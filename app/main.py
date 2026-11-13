"""PhantomMirror - monitor Phantom perps top traders (via Hyperliquid) and paper-mirror them."""
import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import Config
from .db import DB
from .engine import STRATEGIES, Engine
from .hl import DemoClient, HLClient

log = logging.getLogger("phantom-mirror")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
STATIC = os.path.join(os.path.dirname(__file__), "static")


class State:
    cfg: Config
    db: DB
    engine: Engine
    hl: object
    mids: dict = {}
    last_poll: float | None = None
    last_poll_error: str | None = None
    last_equity_snapshot: float = 0.0


S = State()


async def poll_once(now: float | None = None) -> None:
    now = now or time.time()
    mids = await S.hl.all_mids()
    S.mids = mids
    for t in S.db.q("SELECT address FROM traders WHERE active=1"):
        try:
            state = await S.hl.state(t["address"])
            for ev in S.engine.ingest_trader(t["address"], state, mids, now):
                log.info("EVENT %s %s %s %.6g -> %.6g", ev["address"][:10], ev["kind"], ev["coin"], ev["prev_szi"], ev["new_szi"])
        except Exception as e:  # a failed fetch must never be read as "trader went flat"
            S.db.x("UPDATE traders SET last_error=? WHERE address=?", (f"{type(e).__name__}: {e}"[:300], t["address"]))
            log.warning("fetch failed for %s: %s", t["address"], e)
    S.engine.run_strategies(mids, now)
    if now - S.last_equity_snapshot >= S.cfg.equity_snapshot_seconds:
        S.engine.snapshot_equity(mids, now)
        S.last_equity_snapshot = now
    S.last_poll, S.last_poll_error = now, None


async def poll_loop() -> None:
    while True:
        try:
            await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            S.last_poll_error = f"{type(e).__name__}: {e}"
            log.exception("poll failed")
        await asyncio.sleep(S.cfg.poll_seconds)


def init_state(cfg: Config | None = None, db_path: str | None = None) -> None:
    S.cfg = cfg or Config()
    S.db = DB(db_path or os.path.join(S.cfg.data_dir, "phantom_mirror.db"))
    S.engine = Engine(S.db, S.cfg)
    S.mids, S.last_poll, S.last_poll_error, S.last_equity_snapshot = {}, None, None, 0.0
    if S.cfg.demo_mode:
        S.hl = DemoClient()
        for addr, name in DemoClient.TRADERS.items():
            S.db.x("INSERT OR IGNORE INTO traders(address,label,source,added_at) VALUES(?,?,?,?)",
                   (addr, name, "demo", time.time()))
    else:
        S.hl = HLClient(S.cfg.hl_info_url, S.cfg.hl_leaderboard_url, S.cfg.hl_dexs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_state()
    task = asyncio.create_task(poll_loop())
    log.info("started (demo=%s, poll=%ss)", S.cfg.demo_mode, S.cfg.poll_seconds)
    yield
    task.cancel()
    await S.hl.close()


app = FastAPI(title="PhantomMirror", lifespan=lifespan)


def admin(x_admin_token: str = Header(default="")) -> None:
    if S.cfg.admin_token and x_admin_token != S.cfg.admin_token:
        raise HTTPException(401, "admin token required")


# ---------------------------------------------------------------------- read API
@app.get("/healthz")
def healthz():
    stale = S.last_poll is None or time.time() - S.last_poll > max(120, S.cfg.poll_seconds * 5)
    return {"ok": not stale, "last_poll": S.last_poll, "last_poll_error": S.last_poll_error}


@app.get("/api/summary")
def summary():
    return {
        "now": time.time(), "last_poll": S.last_poll, "last_poll_error": S.last_poll_error,
        "strategies": [S.engine.stats(s, S.mids) for s in STRATEGIES],
        "config": S.cfg.public(),
    }


@app.get("/api/traders")
def traders():
    rows = S.db.q("SELECT * FROM traders ORDER BY active DESC, added_at")
    pos = S.db.q("SELECT * FROM trader_positions")
    copy_pnl = {r["source"]: r for r in S.db.q(
        "SELECT source, SUM(realized_pnl)-SUM(fee) net, SUM(closing) closes FROM paper_fills "
        "WHERE strategy='copy' GROUP BY source")}
    for r in rows:
        r["positions"] = [dict(p, notional=abs(p["szi"]) * S.mids.get(p["coin"], p["entry_px"] or 0))
                          for p in pos if p["address"] == r["address"]]
        unreal = sum(p["size"] * (S.mids.get(p["coin"], p["entry_px"]) - p["entry_px"]) for p in S.db.q(
            "SELECT * FROM paper_positions WHERE strategy='copy' AND source=?", (r["address"],)))
        c = copy_pnl.get(r["address"])
        r["copy_pnl"] = (c["net"] if c else 0.0) + unreal
    return rows


@app.get("/api/positions")
def positions():
    rows = S.db.q("SELECT * FROM paper_positions ORDER BY strategy, opened_at DESC")
    for r in rows:
        mid = S.mids.get(r["coin"])
        r["mid"] = mid
        r["unrealized"] = r["size"] * (mid - r["entry_px"]) if mid else None
        r["notional"] = abs(r["size"]) * (mid or r["entry_px"])
    return rows


@app.get("/api/consensus")
def consensus():
    board = S.engine.consensus_board()
    return sorted(({"coin": c, **b} for c, b in board.items()), key=lambda r: -(r["long"] + r["short"]))


@app.get("/api/events")
def events(limit: int = 100):
    return S.db.q("SELECT e.*, t.label FROM events e LEFT JOIN traders t ON t.address=e.address "
                  "ORDER BY e.id DESC LIMIT ?", (min(limit, 1000),))


@app.get("/api/fills")
def fills(limit: int = 100):
    return S.db.q("SELECT * FROM paper_fills ORDER BY id DESC LIMIT ?", (min(limit, 1000),))


@app.get("/api/equity")
def equity(max_points: int = 600):
    out = {}
    for s in STRATEGIES:
        rows = S.db.q("SELECT ts, equity FROM equity WHERE strategy=? ORDER BY ts", (s,))
        step = max(1, len(rows) // max_points)
        out[s] = rows[::step] + (rows[-1:] if rows and (len(rows) - 1) % step else [])
    return out


# ---------------------------------------------------------------------- admin API
class TraderIn(BaseModel):
    address: str
    label: str = ""


class SeedIn(BaseModel):
    window: str = "week"  # day | week | month | allTime
    top: int = 10
    min_account_value: float = 50_000


def _add(address: str, label: str, source: str) -> bool:
    address = address.strip().lower()
    if not ADDR_RE.match(address):
        raise HTTPException(400, "address must be a 0x Ethereum-format address (Phantom perps accounts are 0x addresses, not Solana addresses)")
    n = S.db.x("INSERT OR IGNORE INTO traders(address,label,source,added_at) VALUES(?,?,?,?)",
               (address, label.strip()[:60], source, time.time()))
    if not n:  # existed: reactivate, re-baseline so stale positions aren't mirrored
        S.db.x("UPDATE traders SET active=1, baselined=CASE WHEN active=1 THEN baselined ELSE 0 END, "
               "label=CASE WHEN ?<>'' THEN ? ELSE label END WHERE address=?", (label, label.strip()[:60], address))
    return bool(n)


@app.post("/api/traders", dependencies=[Depends(admin)])
def add_trader(body: TraderIn):
    return {"added": _add(body.address, body.label, "manual")}


@app.delete("/api/traders/{address}", dependencies=[Depends(admin)])
def remove_trader(address: str):
    """Deactivate: copy positions for this trader close on the next poll; history is kept."""
    address = address.lower()
    S.db.x("UPDATE traders SET active=0 WHERE address=?", (address,))
    S.db.x("DELETE FROM trader_positions WHERE address=?", (address,))
    return {"ok": True}


@app.post("/api/traders/seed", dependencies=[Depends(admin)])
async def seed(body: SeedIn):
    """Seed from the Hyperliquid-wide leaderboard (a proxy - not Phantom's own Top Traders list)."""
    try:
        rows = await S.hl.leaderboard(body.window, min(body.top, 50), body.min_account_value)
    except Exception as e:
        raise HTTPException(502, f"leaderboard fetch failed: {type(e).__name__}: {e}")
    added = [r["address"] for r in rows if _add(r["address"], r["label"] or f"HL {body.window} top", "hl_leaderboard")]
    return {"found": len(rows), "added": added}


@app.post("/api/reset", dependencies=[Depends(admin)])
def reset():
    """Wipe paper trading history (keeps the trader roster)."""
    for t in ("paper_positions", "paper_fills", "equity"):
        S.db.x(f"DELETE FROM {t}")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
