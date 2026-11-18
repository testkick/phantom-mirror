import asyncio
import time

import pytest

from app.config import Config
from app.db import DB
from app.engine import Engine
from app.hl import parse_state

A, B, C, D = ("0x" + ch * 40 for ch in "abcd")


def mk(**over):
    cfg = Config()
    cfg.fee_bps, cfg.slippage_bps = 0.0, 0.0
    for k, v in over.items():
        setattr(cfg, k, v)
    db = DB(":memory:")
    for addr in (A, B, C, D):
        db.x("INSERT INTO traders(address,added_at) VALUES(?,0)", (addr,))
    return Engine(db, cfg), db


def st(av=100_000, **pos):
    return {"account_value": av, "positions": {c: {"szi": s, "entry_px": 1.0, "leverage": 5} for c, s in pos.items()}}


def paper(db, strategy):
    return {(r["source"], r["coin"]): r for r in db.q("SELECT * FROM paper_positions WHERE strategy=?", (strategy,))}


def test_parse_state_real_shape():
    raw = {"assetPositions": [{"type": "oneWay", "position": {"coin": "BTC", "szi": "-0.5", "entryPx": "90000.0",
            "leverage": {"type": "cross", "value": 20}}}, {"position": {"coin": "ETH", "szi": "0.0"}}],
           "marginSummary": {"accountValue": "12345.6"}}
    s = parse_state(raw)
    assert s["account_value"] == 12345.6
    assert s["positions"] == {"BTC": {"szi": -0.5, "entry_px": 90000.0, "leverage": 20.0}}


def test_baseline_positions_are_not_copied_and_events_classified():
    e, db = mk()
    mids = {"BTC": 100.0, "ETH": 10.0}
    assert e.ingest_trader(A, st(BTC=10), mids, 1) == []          # first snapshot: baseline, no events
    e.run_copy(mids, 1)
    assert paper(db, "copy") == {}                                 # pre-existing position not mirrored
    kinds = lambda evs: sorted((x["coin"], x["kind"]) for x in evs)
    assert kinds(e.ingest_trader(A, st(BTC=20, ETH=100), mids, 2)) == [("BTC", "increase"), ("ETH", "open")]
    assert kinds(e.ingest_trader(A, st(BTC=5, ETH=-100), mids, 3)) == [("BTC", "reduce"), ("ETH", "flip")]
    assert kinds(e.ingest_trader(A, st(), mids, 4)) == [("BTC", "close"), ("ETH", "close")]
    assert e.ingest_trader(A, st(), mids, 5) == []


def test_copy_scales_rebalances_and_realizes_pnl():
    e, db = mk()
    mids = {"ETH": 10.0}
    e.ingest_trader(A, st(), mids, 1)
    e.ingest_trader(A, st(ETH=5000), mids, 2)                      # 50k notional on 100k = 0.5x
    e.run_copy(mids, 2)
    p = paper(db, "copy")[(A, "ETH")]
    assert p["size"] == pytest.approx(50.0)                        # 0.5x of $1,000 alloc = $500 = 50 ETH
    e.ingest_trader(A, st(ETH=5200), mids, 3)                      # +4% : below 10% threshold
    e.run_copy(mids, 3)
    assert paper(db, "copy")[(A, "ETH")]["size"] == pytest.approx(50.0)
    mids = {"ETH": 12.0}
    e.ingest_trader(A, st(ETH=2500), mids, 4)                      # halves
    e.run_copy(mids, 4)
    assert paper(db, "copy")[(A, "ETH")]["size"] == pytest.approx(25.0)
    e.ingest_trader(A, st(), mids, 5)
    e.run_copy(mids, 5)
    assert paper(db, "copy") == {}
    s = e.stats("copy", mids)
    assert s["realized"] == pytest.approx(50 * 2.0)                # 50 ETH * $2
    assert s["equity"] == pytest.approx(10_100)
    assert s["win_rate"] == 1.0


def test_copy_short_flip_and_leverage_cap():
    e, db = mk()
    mids = {"SOL": 100.0}
    e.ingest_trader(A, st(av=10_000), mids, 1)
    e.ingest_trader(A, st(av=10_000, SOL=-2000), mids, 2)          # 20x leverage short
    e.run_copy(mids, 2)
    assert paper(db, "copy")[(A, "SOL")]["size"] == pytest.approx(-50.0)   # capped at 5x of $1,000
    mids = {"SOL": 90.0}
    e.ingest_trader(A, st(av=10_000, SOL=100), mids, 3)            # flips long 1x
    e.run_copy(mids, 3)
    p = paper(db, "copy")[(A, "SOL")]
    assert p["size"] == pytest.approx(10.0)             # 100 SOL * ($1,000 alloc / $10,000 account)
    assert p["entry_px"] == 90.0
    assert e.stats("copy", mids)["realized"] == pytest.approx(500.0)       # short 50 from 100 -> 90


def test_fees_and_slippage_reduce_equity():
    e, db = mk(fee_bps=10.0, slippage_bps=10.0)
    e.fill("copy", A, "BTC", 1.0, 100.0, 1, "t")
    e.fill("copy", A, "BTC", -1.0, 100.0, 2, "t")
    s = e.stats("copy", {"BTC": 100.0})
    assert s["realized"] == pytest.approx(99.9 - 100.1)
    assert s["fees"] == pytest.approx((100.1 + 99.9) * 0.001)
    assert s["equity"] < 10_000


def test_consensus_enter_hold_exit():
    e, db = mk()
    mids = {"BTC": 100.0}
    for t in (A, B, C, D):
        e.ingest_trader(t, st(), mids, 1)
    e.ingest_trader(A, st(BTC=1), mids, 2)
    e.ingest_trader(B, st(BTC=1), mids, 2)
    e.run_consensus(mids, 2)
    assert paper(db, "consensus") == {}                            # 2 < min 3
    e.ingest_trader(C, st(BTC=3), mids, 3)
    e.run_consensus(mids, 3)
    p = paper(db, "consensus")[("consensus", "BTC")]
    assert p["size"] == pytest.approx(10.0)                        # $1,000 / $100
    e.ingest_trader(D, st(BTC=-1), mids, 4)                        # 3L/1S = 75%: hold
    e.ingest_trader(A, st(), mids, 4)                              # 2L/1S = 67% >= exit 50%: still hold
    e.run_consensus(mids, 4)
    assert ("consensus", "BTC") in paper(db, "consensus")
    mids = {"BTC": 110.0}
    e.ingest_trader(B, st(), mids, 5)                              # 1L/1S: below exit min
    e.run_consensus(mids, 5)
    assert paper(db, "consensus") == {}
    assert e.stats("consensus", mids)["realized"] == pytest.approx(100.0)


def test_consensus_blocked_by_disagreement_and_max_positions():
    e, db = mk(consensus_max_positions=1)
    mids = {"BTC": 100.0, "ETH": 10.0}
    for t in (A, B, C, D):
        e.ingest_trader(t, st(), mids, 1)
    e.ingest_trader(A, st(BTC=1, ETH=1), mids, 2)
    e.ingest_trader(B, st(BTC=1, ETH=1), mids, 2)
    e.ingest_trader(C, st(BTC=1, ETH=1), mids, 2)
    e.ingest_trader(D, st(BTC=-1, ETH=-1), mids, 2)                # 75% >= 70%: signal on both
    e.run_consensus(mids, 2)
    assert len(paper(db, "consensus")) == 1                        # capped
    e2, db2 = mk()
    for t in (A, B, C, D):
        e2.ingest_trader(t, st(), mids, 1)
    e2.ingest_trader(A, st(BTC=1), mids, 2); e2.ingest_trader(B, st(BTC=1), mids, 2)
    e2.ingest_trader(C, st(BTC=1), mids, 2); e2.ingest_trader(D, st(BTC=-1), mids, 2)
    e2.cfg.consensus_agreement = 0.8
    e2.run_consensus(mids, 2)
    assert paper(db2, "consensus") == {}                           # 75% < 80%


def test_missing_price_never_closes_position():
    e, db = mk()
    e.ingest_trader(A, st(), {"ETH": 10.0}, 1)
    e.ingest_trader(A, st(ETH=5000), {"ETH": 10.0}, 2)
    e.run_strategies({"ETH": 10.0}, 2)
    e.run_strategies({}, 3)                                        # price feed gap
    assert (A, "ETH") in paper(db, "copy")


def test_demo_end_to_end_and_api(tmp_path):
    from fastapi.testclient import TestClient
    from app import main

    cfg = Config()
    cfg.demo_mode, cfg.admin_token, cfg.equity_snapshot_seconds = True, "secret", 0
    main.init_state(cfg, str(tmp_path / "t.db"))

    async def run():
        for i in range(400):
            await main.poll_once(now=1_700_000_000 + i * 20)
    asyncio.run(run())

    c = TestClient(main.app)                                       # no lifespan: state already initialised
    s = c.get("/api/summary").json()
    by = {x["strategy"]: x for x in s["strategies"]}
    assert by["copy"]["fills"] > 0 and by["consensus"]["fills"] > 0 and by["spot_copy"]["fills"] > 0
    for x in by.values():                                          # accounting identity
        assert x["equity"] == pytest.approx(10_000 + x["realized"] - x["fees"] + x["unrealized"])
    assert len(c.get("/api/traders").json()) == 7
    assert c.get("/api/events").json()
    assert len(c.get("/api/equity").json()["copy"]) > 10
    dr = c.get("/api/dryrun").json()
    assert dr["summary"]["n"] > 0 and dr["summary"]["ok"] > 0
    sug = c.get("/api/discover?category=spot&window=week").json()
    assert len(sug) == 8 and not sug[0]["tracked"]
    assert c.get("/api/discover?category=spot&window=all").status_code == 400
    assert c.post("/api/traders", json={"address": "0x" + "9" * 40}).status_code == 401
    assert c.post("/api/traders", json={"address": "@scotty78"}, headers={"x-admin-token": "secret"}).status_code == 400
    assert c.post("/api/traders", json={"address": "not-an-address"}, headers={"x-admin-token": "secret"}).status_code == 400
    assert c.post("/api/traders", json={"address": "0x" + "9" * 40, "label": "x"}, headers={"x-admin-token": "secret"}).json()["added"]
    assert c.delete("/api/traders/" + "0x" + "1" * 40, headers={"x-admin-token": "secret"}).json()["ok"]
    asyncio.run(main.poll_once(now=1_700_100_000))
    assert not [p for p in c.get("/api/positions").json() if p["source"] == "0x" + "1" * 40]
