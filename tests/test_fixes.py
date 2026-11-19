import asyncio
import json

import httpx
import pytest

from app.config import Config
from app.db import DB
from app.engine import Engine
from app.hl import HLClient
from app.sol import SOL_MINT

A = "0x" + "a" * 40
W = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
M = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def mk():
    cfg = Config()
    cfg.fee_bps = cfg.slippage_bps = cfg.spot_fee_bps = cfg.spot_slippage_bps = 0.0
    db = DB(":memory:")
    db.x("INSERT INTO traders(address,chain,added_at) VALUES(?, 'hl', 0)", (A,))
    db.x("INSERT INTO traders(address,chain,added_at) VALUES(?, 'sol', 0)", (W,))
    return Engine(db, cfg), db


def st(**pos):
    return {"account_value": 100_000, "positions": {c: {"szi": s, "entry_px": 1.0, "leverage": 1} for c, s in pos.items()}}


def paper(db, strategy):
    return {r["coin"]: r["size"] for r in db.q("SELECT * FROM paper_positions WHERE strategy=?", (strategy,))}


def test_perps_additions_to_old_position_are_copied_old_part_never():
    e, db = mk()
    mids = {"ETH": 10.0}
    e.ingest_trader(A, st(ETH=10_000), mids, 1)          # pre-tracking: 10k ETH
    e.run_copy(mids, 1)
    assert paper(db, "copy") == {}
    e.ingest_trader(A, st(ETH=15_000), mids, 2)          # adds 5k -> copy 5k scaled (x 1000/100000)
    e.run_copy(mids, 2)
    assert paper(db, "copy")["ETH"] == pytest.approx(50)
    e.ingest_trader(A, st(ETH=8_000), mids, 3)           # sells below the old size: nothing new left
    e.run_copy(mids, 3)
    assert paper(db, "copy") == {}
    e.ingest_trader(A, st(ETH=12_000), mids, 4)          # base shrank to 8k, so 4k is new
    e.run_copy(mids, 4)
    assert paper(db, "copy")["ETH"] == pytest.approx(40)


def test_newly_discovered_market_positions_are_pre_tracking():
    e, db = mk()
    mids = {"xyz:TSLA": 400.0}
    e.ingest_trader(A, st(), mids, 1)
    evs = e.ingest_trader(A, {**st(**{"xyz:TSLA": 100}), "baseline_coins": {"xyz:TSLA"}}, mids, 2)
    assert evs == []
    e.run_copy(mids, 2)
    assert paper(db, "copy") == {}
    evs = e.ingest_trader(A, st(**{"xyz:TSLA": 150}), mids, 3)
    assert [x["kind"] for x in evs] == ["increase"]


def test_spot_buy_more_on_old_bag_is_copied():
    e, db = mk()
    P = {SOL_MINT: {"price": 100.0, "liquidity": 1e9}, M: {"price": 1.0, "liquidity": 1e6}}
    e.ingest_spot(W, {"sol": 900, "tokens": {M: 5_000}}, P, 1)     # $95k portfolio, old bag
    e.ingest_spot(W, {"sol": 850, "tokens": {M: 10_000}}, P, 2)    # bought 5k more
    e.run_spot_copy(P, 2)
    assert paper(db, "spot_copy")[M] == pytest.approx(5_000 * 1000 / 95_000)
    e.ingest_spot(W, {"sol": 900, "tokens": {M: 4_000}}, P, 3)     # sold into the old bag
    e.run_spot_copy(P, 3)
    assert paper(db, "spot_copy") == {}


def test_unified_account_value_and_hip3_discovery(monkeypatch):
    calls = []

    def handler(req):
        b = json.loads(req.content)
        calls.append((b["type"], b.get("dex", "")))
        t, dex = b["type"], b.get("dex", "")
        if t == "perpDexs":
            return httpx.Response(200, json=[None, {"name": "xyz"}, {"name": "flx"}])
        if t == "userAbstraction":
            return httpx.Response(200, json="unifiedAccount")
        if t == "clearinghouseState":
            if dex == "xyz":
                return httpx.Response(200, json={"marginSummary": {"accountValue": "1500"},
                                                 "assetPositions": [{"position": {"coin": "xyz:TSLA", "szi": "2", "entryPx": "400"}}]})
            return httpx.Response(200, json={"marginSummary": {"accountValue": "0"}, "assetPositions": []})
        if t == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": [{"coin": "USDC", "total": "10000", "hold": "1400"},
                                                          {"coin": "HYPE", "total": "5", "hold": "0"}]})
        if t == "allMids":
            return httpx.Response(200, json={"BTC": "1"} if not dex else {f"{dex}:TSLA": "400"})
        raise AssertionError(t)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    hl = HLClient("https://x/info", "https://x/lb")

    async def run():
        await hl.all_mids()
        s1 = await hl.state(A)
        mids = await hl.all_mids()
        calls.clear()
        s2 = await hl.state(A)
        return s1, mids, s2
    s1, mids, s2 = asyncio.run(run())
    assert s1["positions"].keys() == {"xyz:TSLA"} and s1["baseline_coins"] == {"xyz:TSLA"}
    assert s1["account_value"] == pytest.approx(10_000 + (1500 - 1400))   # stables + uPnL
    assert "xyz:TSLA" in mids
    assert ("perpDexs", "") not in calls and ("clearinghouseState", "flx") not in calls  # no re-sweep
    assert s2["positions"].keys() == {"xyz:TSLA"} and s2["baseline_coins"] == set()


def test_spot_round_robin_backs_off_on_429(tmp_path):
    from app import main
    cfg = Config(); cfg.spot_wallets_per_poll = 2
    main.init_state(cfg, str(tmp_path / "t.db"))
    wallets = [f"{c}" * 43 + "1" for c in "ABCDE"]
    for w in wallets:
        main.S.db.x("INSERT INTO traders(address,chain,added_at) VALUES(?, 'sol', 0)", (w,))
    seen = []

    class Sol:
        fail = set()
        async def holdings(self, a):
            seen.append(a)
            if a in self.fail:
                raise httpx.HTTPStatusError("429", request=httpx.Request("POST", "https://x"), response=httpx.Response(429))
            return {"sol": 1.0, "tokens": {}}
        async def prices(self, mints):
            return {SOL_MINT: {"price": 100.0, "liquidity": 1e9, "decimals": 9}}
        async def token_info(self, mints):
            return {}
        async def sol_balance(self, a):
            return 0.0
    main.S.sol = Sol()
    asyncio.run(main.poll_spot(1)); asyncio.run(main.poll_spot(2))
    assert seen == wallets[:4]                                      # 2 per poll, continuing in order
    main.S.sol.fail = {wallets[4]}
    seen.clear()
    asyncio.run(main.poll_spot(3))
    assert seen == [wallets[4]]                                     # stopped at the rate limit
    main.S.sol.fail = set(); seen.clear()
    asyncio.run(main.poll_spot(4))
    assert seen == [wallets[4], wallets[0]]                         # retried first, then wraps


def test_migration_adds_base_from_baseline(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE spot_holdings (address TEXT, mint TEXT, amount REAL, baseline INTEGER, updated_at REAL, PRIMARY KEY(address,mint))")
    c.execute("INSERT INTO spot_holdings VALUES('w','m',42,1,0)")
    c.execute("INSERT INTO spot_holdings VALUES('w','n',7,0,0)")
    c.commit(); c.close()
    db = DB(str(path))
    assert {r["mint"]: r["base"] for r in db.q("SELECT * FROM spot_holdings")} == {"m": 42, "n": 0}
