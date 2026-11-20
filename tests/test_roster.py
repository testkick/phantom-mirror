import asyncio

import pytest

from app import roster
from app.config import Config
from app.db import DB
from app.engine import Engine

DAY = 86400


def row(addr, av, week=1, month=1, alltime=1, vlm=0, name=None):
    return {"ethAddress": addr, "displayName": name, "accountValue": str(av), "windowPerformances": [
        ["week", {"pnl": str(week), "vlm": str(vlm)}], ["month", {"pnl": str(month), "roi": "0.1"}],
        ["allTime", {"pnl": str(alltime)}]]}


def addr(i):
    return "0x" + f"{i:040x}"


def cfg(**kw):
    c = Config()
    c.roster_size, c.roster_min_account_usd, c.roster_max_weekly_turnover, c.roster_drop_days = 3, 100_000, 50, 3
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_select_filters_and_ranks():
    rows = [
        row(addr(1), 50_000, month=9e9),                    # too small
        row(addr(2), 1e6, month=-5),                        # losing month
        row(addr(3), 1e6, month=5e5, alltime=-1),           # losing all time
        row(addr(4), 1e6, month=9e5, vlm=1e9),              # market maker: 1000x turnover
        row(addr(5), 1e6, month=1e5, name="five"),
        row(addr(6), 2e6, month=3e5),
        row(addr(7), 5e5, month=2e5),
        row(addr(8), 5e5, month=1e3),
    ]
    picks = roster.select(rows, cfg())
    assert [p["address"] for p in picks] == [addr(6), addr(7), addr(5)]


def test_apply_adds_voters_leaves_manual_and_drops_after_grace():
    db, c = DB(":memory:"), cfg()
    db.x("INSERT INTO traders(address,label,source,chain,added_at) VALUES(?, 'mine', 'manual', 'hl', 0)", (addr(9),))
    picks = [{"address": a, "name": ""} for a in (addr(1), addr(2), addr(9))]
    s = roster.apply(db, picks, c, now=1 * DAY)
    assert s["added"] == 2 and s["already_manual"] == 1
    t = {r["address"]: r for r in db.q("SELECT * FROM traders")}
    assert t[addr(1)]["role"] == "voter" and t[addr(1)]["auto"] == 1
    assert t[addr(9)]["role"] == "copy" and t[addr(9)]["auto"] == 0          # untouched
    # addr(2) falls out: keeps voting through the grace period, then goes
    picks2 = [{"address": addr(1), "name": ""}]
    assert roster.apply(db, picks2, c, now=2 * DAY)["leaving"] == 1
    assert roster.apply(db, picks2, c, now=4 * DAY)["leaving"] == 1          # 2 days out
    assert roster.apply(db, picks2, c, now=5 * DAY)["dropped"] == 1          # 3 days out
    assert db.one("SELECT active FROM traders WHERE address=?", (addr(2),))["active"] == 0
    assert db.one("SELECT active FROM traders WHERE address=?", (addr(9),))["active"] == 1   # manual never dropped
    # comes back: rejoins and re-baselines
    s = roster.apply(db, [{"address": addr(2), "name": ""}], c, now=6 * DAY)
    r = db.one("SELECT * FROM traders WHERE address=?", (addr(2),))
    assert r["active"] == 1 and r["baselined"] == 0 and r["out_since"] is None


def test_back_in_selection_cancels_leaving():
    db, c = DB(":memory:"), cfg()
    roster.apply(db, [{"address": addr(1), "name": ""}], c, now=0)
    roster.apply(db, [], c, now=1 * DAY)
    roster.apply(db, [{"address": addr(1), "name": ""}], c, now=2 * DAY)
    roster.apply(db, [], c, now=4 * DAY)                                     # out again: clock restarts
    assert db.one("SELECT active FROM traders")["active"] == 1


def test_voters_vote_but_are_not_copied_and_thresholds_scale():
    db, c = DB(":memory:"), cfg()
    c.fee_bps = c.slippage_bps = 0.0
    e = Engine(db, c)
    for i in range(20):
        db.x("INSERT INTO traders(address,chain,role,auto,added_at) VALUES(?, 'hl', 'voter', 1, 0)", (addr(i),))
    th = e.thresholds()
    assert th == {"members": 20, "entry": 3, "exit": 2}
    for i in range(20, 40):
        db.x("INSERT INTO traders(address,chain,role,auto,added_at) VALUES(?, 'hl', 'voter', 1, 0)", (addr(i),))
    assert e.thresholds() == {"members": 40, "entry": 6, "exit": 4}
    mids = {"BTC": 100.0}
    st = lambda **p: {"account_value": 1e6, "positions": {k: {"szi": v, "entry_px": 1, "leverage": 1} for k, v in p.items()}}
    for i in range(6):
        e.ingest_trader(addr(i), st(), mids, 1)
        e.ingest_trader(addr(i), st(BTC=-1), mids, 2)
    e.run_copy(mids, 2)
    assert db.q("SELECT * FROM paper_positions WHERE strategy='copy'") == []   # voters never copied
    assert e.consensus_board()["BTC"]["signal"] == -1                         # 6 of 6 holders, 6 >= entry


def test_refresh_end_to_end_with_injected_fetch():
    db, c = DB(":memory:"), cfg()

    async def fetch():
        return [row(addr(1), 1e6, month=5), row(addr(2), 10, month=5)]
    s = asyncio.run(roster.refresh(db, c, now=123, fetch=fetch))
    assert s["added"] == 1 and roster.last(db)["ts"] == 123
