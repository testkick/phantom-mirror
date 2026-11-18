"""PhantomMirror - monitor Phantom top traders and paper-mirror them.

Two sources:
  hl  - perps traders, read from Hyperliquid by 0x address
  sol - spot traders, read from Solana by wallet address (holdings diffed between polls)
"""
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
from .discover import PERP_WINDOWS, SPOT_WINDOWS, DemoDiscovery, Discovery
from .dryrun import DemoDryRun, DryRun
from .engine import STRATEGIES, Engine
from .hl import DemoClient, HLClient
from .sol import CASH_MINTS, SOL_ADDR_RE, SOL_MINT, DemoSolClient, SolClient

log = logging.getLogger("phantom-mirror")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
STATIC = os.path.join(os.path.dirname(__file__), "static")


class State:
    cfg: Config
    db: DB
    engine: Engine
    hl: object
    sol: object
    discovery: object
    dryrun: object
    mids: dict = {}                    # perp coin -> mid
    spot_px: dict = {}                 # mint -> {price, liquidity, ts}
    symbols: dict = {}                 # mint -> symbol
    price_miss: dict = {}              # mint -> ts Jupiter last returned no price
    last_poll: float | None = None
    last_poll_error: str | None = None
    last_equity_snapshot: float = 0.0


S = State()


def marks() -> dict:
    """Perp mids and spot prices in one coin -> price map (mints never collide with tickers)."""
    return {**S.mids, **{m: v["price"] for m, v in S.spot_px.items()}}


def sym(coin: str) -> str:
    if coin in S.symbols:
        return S.symbols[coin]
    return coin if len(coin) <= 12 else f"{coin[:4]}…{coin[-4:]}"


def _err(address: str, e: Exception) -> None:
    S.db.x("UPDATE traders SET last_error=? WHERE address=?", (f"{type(e).__name__}: {e}"[:300], address))
    log.warning("fetch failed for %s: %s", address, e)


async def poll_perps(now: float) -> None:
    traders = S.db.q("SELECT address FROM traders WHERE active=1 AND chain='hl'")
    mids = await S.hl.all_mids()
    S.mids = mids
    for t in traders:
        try:
            state = await S.hl.state(t["address"])
            for ev in S.engine.ingest_trader(t["address"], state, mids, now):
                log.info("EVENT %s %s %s %.6g -> %.6g", ev["address"][:10], ev["kind"], ev["coin"], ev["prev_szi"], ev["new_szi"])
        except Exception as e:  # a failed fetch must never be read as "trader went flat"
            _err(t["address"], e)
    S.engine.run_strategies(mids, now)


async def poll_spot(now: float) -> None:
    traders = S.db.q("SELECT address FROM traders WHERE active=1 AND chain='sol'")
    held_paper = {r["coin"] for r in S.db.q("SELECT coin FROM paper_positions WHERE strategy='spot_copy'")}
    if not traders and not held_paper:
        return
    snaps: dict[str, dict] = {}
    for t in traders:
        try:
            snaps[t["address"]] = await S.sol.holdings(t["address"])
        except Exception as e:
            _err(t["address"], e)

    # Price what can trade this round on every poll; everything else (dust, pre-existing bags)
    # only every SPOT_PRICE_REFRESH_SECONDS, for portfolio value.
    stored = {(r["address"], r["mint"]): r for r in S.db.q("SELECT * FROM spot_holdings")}
    hot, cold = {SOL_MINT} | held_paper, set()
    for addr, snap in snaps.items():
        for mint, amt in snap["tokens"].items():
            if mint in CASH_MINTS:
                continue
            r = stored.get((addr, mint))
            fresh = r is None or not r["baseline"] or r["amount"] != amt
            # unpriceable tokens back off even when "hot", or junk airdrops would be re-queried every poll
            if fresh and now - S.price_miss.get(mint, 0) >= S.cfg.spot_price_refresh_seconds:
                hot.add(mint)
            else:
                cold.add(mint)
    last = lambda m: S.spot_px.get(m, {}).get("ts") or S.price_miss.get(m, 0)
    stale = [m for m in cold - hot if now - last(m) >= S.cfg.spot_price_refresh_seconds]
    want = list(hot) + stale
    if want:
        got = await S.sol.prices(want)
        for m in want:
            if m in got:
                S.spot_px[m] = {**got[m], "ts": now}
                S.price_miss.pop(m, None)
            else:  # Jupiter has no reliable price: back off like a cold token
                S.price_miss[m] = now

    for addr, snap in snaps.items():
        for ev in S.engine.ingest_spot(addr, snap, S.spot_px, now):
            log.info("SPOT %s %s %s %.6g -> %.6g", addr[:8], ev["kind"], sym(ev["coin"]), ev["prev_szi"], ev["new_szi"])

    unknown = [m for m in S.spot_px if m not in S.symbols]
    if unknown:
        try:
            info = await S.sol.token_info(unknown)
            for m in unknown:  # cache misses too, so we don't ask again every poll
                s = info.get(m, {}).get("symbol") or sym(m)
                S.symbols[m] = s
                S.db.x("INSERT OR REPLACE INTO tokens(mint,symbol,name) VALUES(?,?,?)", (m, s, info.get(m, {}).get("name", "")))
        except Exception as e:
            log.warning("token info failed: %s", e)

    S.engine.run_spot_copy(S.spot_px, now)

    if S.cfg.dryrun_enabled:
        bal = None
        if S.cfg.dryrun_sol_wallet:
            try:
                bal = await S.sol.sol_balance(S.cfg.dryrun_sol_wallet)
            except Exception as e:
                log.warning("dry-run wallet balance failed: %s", e)
        for d in await S.dryrun.run(S.spot_px, bal, now):
            log.info("DRYRUN %s %s $%.0f %s %s", d["side"], sym(d["mint"]), d["usd"], d["status"], d["detail"])


async def poll_once(now: float | None = None) -> None:
    now = now or time.time()
    errors = []
    for name, fn in (("perps", poll_perps), ("spot", poll_spot)):
        try:
            await fn(now)
        except Exception as e:  # one source failing must not stop the other
            errors.append(f"{name}: {type(e).__name__}: {e}")
            log.exception("%s poll failed", name)
    if now - S.last_equity_snapshot >= S.cfg.equity_snapshot_seconds:
        S.engine.snapshot_equity(marks(), now)
        S.last_equity_snapshot = now
    S.last_poll, S.last_poll_error = now, ("; ".join(errors) or None)


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
    S.mids, S.spot_px, S.price_miss = {}, {}, {}
    S.last_poll, S.last_poll_error, S.last_equity_snapshot = None, None, 0.0
    S.symbols = {r["mint"]: r["symbol"] for r in S.db.q("SELECT * FROM tokens")}
    if S.cfg.demo_mode:
        S.hl, S.sol = DemoClient(), DemoSolClient()
        S.discovery, S.dryrun = DemoDiscovery(S.cfg), DemoDryRun(S.db, S.cfg, S.spot_px)
        for addr, name in DemoClient.TRADERS.items():
            S.db.x("INSERT OR IGNORE INTO traders(address,label,source,chain,added_at) VALUES(?,?,?,?,?)",
                   (addr, name, "demo", "hl", time.time()))
        for addr, name in DemoSolClient.WALLETS.items():
            S.db.x("INSERT OR IGNORE INTO traders(address,label,source,chain,added_at) VALUES(?,?,?,?,?)",
                   (addr, name, "demo", "sol", time.time()))
    else:
        S.hl = HLClient(S.cfg.hl_info_url, S.cfg.hl_leaderboard_url, S.cfg.hl_dexs)
        S.sol = SolClient(S.cfg.solana_rpc_url, S.cfg.jup_api_base, S.cfg.jup_api_key)
        S.discovery, S.dryrun = Discovery(S.cfg), DryRun(S.db, S.cfg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_state()
    task = asyncio.create_task(poll_loop())
    log.info("started (demo=%s, poll=%ss)", S.cfg.demo_mode, S.cfg.poll_seconds)
    yield
    task.cancel()
    await S.hl.close()
    await S.sol.close()
    await S.dryrun.close()


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
    m = marks()
    return {
        "now": time.time(), "last_poll": S.last_poll, "last_poll_error": S.last_poll_error,
        "strategies": [S.engine.stats(s, m) for s in STRATEGIES],
        "config": S.cfg.public(),
    }


@app.get("/api/traders")
def traders():
    m = marks()
    rows = S.db.q("SELECT * FROM traders ORDER BY active DESC, added_at")
    perp_pos = S.db.q("SELECT * FROM trader_positions")
    spot_pos = S.db.q("SELECT * FROM spot_holdings")
    pnl = {(r["strategy"], r["source"]): r["net"] for r in S.db.q(
        "SELECT strategy, source, SUM(realized_pnl)-SUM(fee) net FROM paper_fills GROUP BY strategy, source")}
    for r in rows:
        strat = "spot_copy" if r["chain"] == "sol" else "copy"
        if r["chain"] == "sol":
            pos = [dict(coin=p["mint"], symbol=sym(p["mint"]), szi=p["amount"], baseline=p["baseline"],
                        notional=p["amount"] * m[p["mint"]] if p["mint"] in m else None)
                   for p in spot_pos if p["address"] == r["address"]]
            pos = sorted((p for p in pos if (p["notional"] or 0) >= 1 or not p["baseline"]),
                         key=lambda p: -(p["notional"] or 0))
            r["more_positions"] = max(0, len(pos) - 8)
            pos = pos[:8]
        else:
            pos = [dict(p, symbol=p["coin"], notional=abs(p["szi"]) * m.get(p["coin"], p["entry_px"] or 0))
                   for p in perp_pos if p["address"] == r["address"]]
            r["more_positions"] = 0
        r["positions"] = pos
        unreal = sum(p["size"] * (m.get(p["coin"], p["entry_px"]) - p["entry_px"]) for p in S.db.q(
            "SELECT * FROM paper_positions WHERE strategy=? AND source=?", (strat, r["address"])))
        r["copy_pnl"] = pnl.get((strat, r["address"]), 0.0) + unreal
    return rows


@app.get("/api/positions")
def positions():
    m = marks()
    rows = S.db.q("SELECT * FROM paper_positions ORDER BY strategy, opened_at DESC")
    for r in rows:
        mid = m.get(r["coin"])
        r["symbol"] = sym(r["coin"])
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
    rows = S.db.q("SELECT e.*, t.label, t.chain FROM events e LEFT JOIN traders t ON t.address=e.address "
                  "ORDER BY e.id DESC LIMIT ?", (min(limit, 1000),))
    for r in rows:
        r["symbol"] = sym(r["coin"])
    return rows


@app.get("/api/fills")
def fills(limit: int = 100):
    rows = S.db.q("SELECT * FROM paper_fills ORDER BY id DESC LIMIT ?", (min(limit, 1000),))
    for r in rows:
        r["symbol"] = sym(r["coin"])
    return rows


@app.get("/api/equity")
def equity(max_points: int = 600):
    out = {}
    for s in STRATEGIES:
        rows = S.db.q("SELECT ts, equity FROM equity WHERE strategy=? ORDER BY ts", (s,))
        step = max(1, len(rows) // max_points)
        out[s] = rows[::step] + (rows[-1:] if rows and (len(rows) - 1) % step else [])
    return out


@app.get("/api/discover")
async def discover(category: str = "perps", window: str = "week"):
    windows = PERP_WINDOWS if category == "perps" else SPOT_WINDOWS if category == "spot" else None
    if windows is None or window not in windows:
        raise HTTPException(400, "category must be perps or spot, with a window from " + ", ".join(windows or PERP_WINDOWS))
    try:
        rows = await S.discovery.get(category, window)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"leaderboard fetch failed: {type(e).__name__}: {e}")
    tracked = {r["address"] for r in S.db.q("SELECT address FROM traders WHERE active=1")}
    return [dict(r, tracked=r["address"] in tracked) for r in rows]


@app.get("/api/dryrun")
def dryrun(limit: int = 60):
    rows = S.db.q("SELECT d.*, t.label FROM dryrun d LEFT JOIN traders t ON t.address=d.source "
                  "ORDER BY d.id DESC LIMIT ?", (min(limit, 500),))
    for r in rows:
        r["symbol"] = sym(r["mint"])
    agg = S.db.one("SELECT COUNT(*) n, SUM(status='ok') ok, SUM(status='blocked') blocked, SUM(status='error') errors, "
                   "AVG(CASE WHEN status='ok' THEN impact_bps END) avg_impact FROM dryrun")
    c = S.cfg
    return {"summary": agg, "rows": rows, "enabled": c.dryrun_enabled, "wallet_set": bool(c.dryrun_sol_wallet),
            "limits": {"max_trade_usd": c.live_max_trade_usd, "max_slippage_bps": c.live_max_slippage_bps,
                       "daily_loss_usd": c.live_daily_loss_usd}}


# ---------------------------------------------------------------------- admin API
class TraderIn(BaseModel):
    address: str
    label: str = ""


class SeedIn(BaseModel):
    window: str = "week"  # day | week | month | allTime
    top: int = 10
    min_account_value: float = 50_000


def normalize(address: str) -> tuple[str, str]:
    """-> (address, chain). 0x addresses are perps (Hyperliquid); base58 are Solana spot wallets."""
    a = address.strip()
    if ADDR_RE.match(a):
        return a.lower(), "hl"
    if SOL_ADDR_RE.match(a):
        return a, "sol"  # base58 is case-sensitive: never lowercase
    if a.startswith("@"):
        raise HTTPException(400, "Phantom usernames can't be looked up automatically. Open the profile in Phantom and "
                                 "paste their address instead: 0x… for perps traders, the Solana address for spot traders.")
    raise HTTPException(400, "Enter a 0x address (perps trader) or a Solana wallet address (spot trader).")


def _add(address: str, label: str, source: str) -> bool:
    address, chain = normalize(address)
    n = S.db.x("INSERT OR IGNORE INTO traders(address,label,source,chain,added_at) VALUES(?,?,?,?,?)",
               (address, label.strip()[:60], source, chain, time.time()))
    if not n:  # existed: reactivate, re-baseline so stale positions aren't mirrored
        S.db.x("UPDATE traders SET active=1, baselined=CASE WHEN active=1 THEN baselined ELSE 0 END, "
               "label=CASE WHEN ?<>'' THEN ? ELSE label END WHERE address=?", (label, label.strip()[:60], address))
    return bool(n)


@app.post("/api/traders", dependencies=[Depends(admin)])
def add_trader(body: TraderIn):
    return {"added": _add(body.address, body.label.lstrip("@"), "manual")}


@app.delete("/api/traders/{address}", dependencies=[Depends(admin)])
def remove_trader(address: str):
    """Deactivate: this trader's copy positions close on the next poll; history is kept."""
    if ADDR_RE.match(address):
        address = address.lower()
    S.db.x("UPDATE traders SET active=0 WHERE address=?", (address,))
    S.db.x("DELETE FROM trader_positions WHERE address=?", (address,))
    S.db.x("DELETE FROM spot_holdings WHERE address=?", (address,))
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
