import asyncio

import httpx
import pytest

from app.config import Config
from app.db import DB
from app.discover import Discovery
from app.dryrun import DryRun
from app.sol import SOL_MINT

M = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def patch_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))


def test_perps_suggestions_filter_and_rank(monkeypatch):
    rows = [
        {"ethAddress": "0xAAA" + "0" * 37, "displayName": "big", "accountValue": "1000000",
         "windowPerformances": [["week", {"pnl": "50000", "roi": "0.05", "vlm": "1e7"}], ["day", {"pnl": "-10", "roi": "0", "vlm": "0"}]]},
        {"ethAddress": "0xBBB" + "0" * 37, "displayName": None, "accountValue": "60000",
         "windowPerformances": [["week", {"pnl": "90000", "roi": "1.5", "vlm": "1e6"}]]},
        {"ethAddress": "0xCCC" + "0" * 37, "accountValue": "500",  # too small
         "windowPerformances": [["week", {"pnl": "999999", "roi": "9", "vlm": "1"}]]},
    ]
    patch_http(monkeypatch, lambda req: httpx.Response(200, json={"leaderboardRows": rows}))
    d = Discovery(Config())
    week = asyncio.run(d.get("perps", "week"))
    assert [r["address"][:5] for r in week] == ["0xbbb", "0xaaa"]
    assert asyncio.run(d.get("perps", "day")) == []            # losers dropped


def test_spot_suggestions_need_key_and_parse(monkeypatch):
    cfg = Config(); cfg.solanatracker_api_key = ""
    with pytest.raises(RuntimeError):
        asyncio.run(Discovery(cfg).get("spot", "week"))
    seen = {}

    def handler(req):
        seen.update(dict(req.url.params), key=req.headers.get("x-api-key"))
        return httpx.Response(200, json={"traders": [
            {"wallet": "W1", "winRate": 61.5, "period": {"realized": 12345, "roi": 250, "volume": 1e5},
             "counts": {"trades": 80}, "identity": {"name": "alice", "twitter": "alice_x"}},
            {"wallet": "W2", "period": {"realized": 9}, "identity": {"type": "bot"}},
            {"wallet": "HFT", "winRate": 40, "period": {"realized": 9e6, "roi": 30}, "counts": {"trades": 168760}},
            {"wallet": "SNIPER", "winRate": 100, "period": {"realized": 2e6, "roi": 600000}, "counts": {"trades": 200}},
            {"wallet": "MOON", "winRate": 60, "period": {"realized": 1e6, "roi": 9000}, "counts": {"trades": 100}}]})
    patch_http(monkeypatch, handler)
    cfg.solanatracker_api_key = "k"
    out = asyncio.run(Discovery(cfg).get("spot", "day"))
    assert seen["key"] == "k" and seen["days"] == "1" and seen["minDays"] == "1" and seen["excludeArbitrage"] == "true"
    assert [r["address"] for r in out] == ["W1"] and out[0]["roi"] == 2.5 and out[0]["name"] == "alice"


class FakeDry(DryRun):
    def __init__(self, db, cfg, impact):
        super().__init__(db, cfg)
        self.impact = impact

    async def quote(self, i, o, amt):
        if i == SOL_MINT:   # buy: SOL lamports -> token (6 dp) at $1 token, $100 SOL
            return {"outAmount": int(amt / 1e9 * 100 * (1 - self.impact) * 1e6)}
        return {"outAmount": int(amt / 1e6 * (1 - self.impact) / 100 * 1e9)}


PX = {SOL_MINT: {"price": 100.0, "liquidity": 1e9, "decimals": 9}, M: {"price": 1.0, "liquidity": 1e6, "decimals": 6}}


def fill(db, delta, ts=1.0, realized=0.0):
    db.x("INSERT INTO paper_fills(ts,strategy,source,coin,delta,px,fee,realized_pnl,closing) VALUES(?, 'spot_copy','W',?,?,1.0,0,?,0)",
         (ts, M, delta, realized))


def test_dryrun_ok_capped_and_slippage_block():
    db = DB(":memory:"); cfg = Config()
    dr = FakeDry(db, cfg, impact=0.01)
    fill(db, 500)                                                # $500 paper buy -> capped at $100
    out = asyncio.run(dr.run(PX, None, 1))
    assert out[0]["status"] == "ok" and out[0]["usd"] == 100 and "capped" in out[0]["detail"]
    assert out[0]["impact_bps"] == pytest.approx(101, abs=1)
    assert asyncio.run(dr.run(PX, None, 2)) == []                # nothing new
    dr.impact = 0.10
    fill(db, -50)
    out = asyncio.run(dr.run(PX, None, 3))
    assert out[0]["side"] == "sell" and out[0]["status"] == "blocked" and "worse than mid" in out[0]["detail"]


def test_dryrun_daily_loss_and_balance_blocks(monkeypatch):
    import time
    db = DB(":memory:"); cfg = Config()
    dr = FakeDry(db, cfg, impact=0.0)
    fill(db, 50)
    out = asyncio.run(dr.run(PX, 0.3, 1))                        # $50 needs 0.5 SOL; wallet has 0.3
    assert out[0]["status"] == "blocked" and "insufficient SOL" in out[0]["detail"]
    fill(db, -10, ts=time.time(), realized=-150)                 # big loss today
    fill(db, 20, ts=time.time())
    out = asyncio.run(dr.run(PX, 10, 2))
    assert out[0]["side"] == "sell" and out[0]["status"] == "ok"    # sells are never blocked by the loss cap
    assert out[1]["status"] == "blocked" and "daily loss" in out[1]["detail"]
