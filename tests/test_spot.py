import pytest

from app.config import Config
from app.db import DB
from app.engine import Engine
from app.sol import SOL_MINT, USDC_MINT

W = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"   # case-sensitive base58
M = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"   # some token
P = {SOL_MINT: {"price": 100.0, "liquidity": 1e9}, M: {"price": 1.0, "liquidity": 1e6}}


def mk(**over):
    cfg = Config()
    cfg.spot_fee_bps = cfg.spot_slippage_bps = 0.0
    for k, v in over.items():
        setattr(cfg, k, v)
    db = DB(":memory:")
    db.x("INSERT INTO traders(address,chain,added_at) VALUES(?, 'sol', 0)", (W,))
    return Engine(db, cfg), db


def h(sol=900.0, **tok):
    return {"sol": sol, "tokens": {({"tok": M, "usdc": USDC_MINT}[k]): v for k, v in tok.items()}}


def pos(db):
    return {r["coin"]: r for r in db.q("SELECT * FROM paper_positions WHERE strategy='spot_copy'")}


def test_preexisting_bags_not_copied_cash_ignored_value_tracked():
    e, db = mk()
    assert e.ingest_spot(W, h(tok=10_000, usdc=500), P, 1) == []
    e.run_spot_copy(P, 1)
    assert pos(db) == {}
    assert db.one("SELECT account_value FROM traders")["account_value"] == pytest.approx(90_000 + 10_000 + 0)  # USDC unpriced here
    assert db.q("SELECT mint FROM spot_holdings") == [{"mint": M}]  # USDC never stored


def test_buy_mirrored_at_portfolio_weight_then_sold_with_pnl():
    e, db = mk()
    e.ingest_spot(W, h(sol=1000), P, 1)
    evs = e.ingest_spot(W, h(sol=900, tok=10_000), P, 2)          # 10% of a $100k portfolio
    assert [x["kind"] for x in evs] == ["buy"]
    e.run_spot_copy(P, 2)
    assert pos(db)[M]["size"] == pytest.approx(100.0)              # 10% of $1,000 alloc at $1
    p2 = {**P, M: {"price": 2.0, "liquidity": 1e6}}
    assert [x["kind"] for x in e.ingest_spot(W, h(sol=1100), p2, 3)] == ["sell all"]
    e.run_spot_copy(p2, 3)
    assert pos(db) == {}
    assert e.stats("spot_copy", {})["realized"] == pytest.approx(100.0)


def test_cap_and_liquidity_filter():
    e, db = mk(spot_max_position_pct=0.25)
    e.ingest_spot(W, h(sol=1000), P, 1)
    e.ingest_spot(W, h(sol=100, tok=90_000), P, 2)                 # 90% weight
    e.run_spot_copy(P, 2)
    assert pos(db)[M]["size"] == pytest.approx(250.0)              # capped at 25% of $1,000
    e2, db2 = mk()
    thin = {**P, M: {"price": 1.0, "liquidity": 5_000}}
    e2.ingest_spot(W, h(sol=1000), thin, 1)
    e2.ingest_spot(W, h(sol=900, tok=10_000), thin, 2)
    e2.run_spot_copy(thin, 2)
    assert pos(db2) == {}                                          # too thin to enter


def test_missing_price_never_sells_and_partial_sell_resizes():
    e, db = mk()
    e.ingest_spot(W, h(sol=1000), P, 1)
    e.ingest_spot(W, h(sol=900, tok=10_000), P, 2)
    e.run_spot_copy(P, 2)
    e.run_spot_copy({SOL_MINT: P[SOL_MINT]}, 3)                    # token price gap
    assert M in pos(db)
    assert [x["kind"] for x in e.ingest_spot(W, h(sol=950, tok=5_000), P, 4)] == ["sell part"]
    e.run_spot_copy(P, 4)
    assert pos(db)[M]["size"] == pytest.approx(50.0)


def test_solana_address_case_preserved_and_chain_detected():
    from app.main import normalize
    assert normalize(W) == (W, "sol")
    assert normalize("0x" + "A" * 40) == ("0x" + "a" * 40, "hl")


def test_migration_adds_chain_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE traders (address TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual', "
              "active INTEGER NOT NULL DEFAULT 1, baselined INTEGER NOT NULL DEFAULT 0, account_value REAL, added_at REAL NOT NULL, "
              "last_seen REAL, last_error TEXT)")
    c.execute("INSERT INTO traders(address, added_at) VALUES('0xabc', 0)")
    c.commit(); c.close()
    db = DB(str(path))
    assert db.one("SELECT chain FROM traders")["chain"] == "hl"
