"""Tiny SQLite wrapper. One file on a Railway volume; WAL mode; single process."""
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS traders (
    address       TEXT PRIMARY KEY,
    label         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'manual',   -- manual | hl_leaderboard | demo
    active        INTEGER NOT NULL DEFAULT 1,
    baselined     INTEGER NOT NULL DEFAULT 0,       -- first snapshot taken?
    account_value REAL,
    added_at      REAL NOT NULL,
    last_seen     REAL,
    last_error    TEXT
);
CREATE TABLE IF NOT EXISTS trader_positions (
    address    TEXT NOT NULL,
    coin       TEXT NOT NULL,
    szi        REAL NOT NULL,                       -- signed size, + long / - short
    entry_px   REAL,
    leverage   REAL,
    baseline   INTEGER NOT NULL DEFAULT 0,          -- 1 = was already open when tracking began
    updated_at REAL NOT NULL,
    PRIMARY KEY (address, coin)
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    address  TEXT NOT NULL,
    coin     TEXT NOT NULL,
    kind     TEXT NOT NULL,                         -- open | increase | reduce | close | flip
    prev_szi REAL NOT NULL,
    new_szi  REAL NOT NULL,
    px       REAL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    strategy  TEXT NOT NULL,                        -- copy | consensus
    source    TEXT NOT NULL,                        -- trader address, or 'consensus'
    coin      TEXT NOT NULL,
    size      REAL NOT NULL,                        -- signed
    entry_px  REAL NOT NULL,
    opened_at REAL NOT NULL,
    PRIMARY KEY (strategy, source, coin)
);
CREATE TABLE IF NOT EXISTS paper_fills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    strategy     TEXT NOT NULL,
    source       TEXT NOT NULL,
    coin         TEXT NOT NULL,
    delta        REAL NOT NULL,                     -- signed size traded
    px           REAL NOT NULL,
    fee          REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,           -- gross, before fee
    closing      INTEGER NOT NULL DEFAULT 0,        -- 1 if this fill reduced/closed a position
    reason       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS equity (
    ts         REAL NOT NULL,
    strategy   TEXT NOT NULL,
    equity     REAL NOT NULL,
    realized   REAL NOT NULL,
    unrealized REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON paper_fills(ts);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(strategy, ts);
"""


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).rowcount
