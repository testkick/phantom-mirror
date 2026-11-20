"""Automatic consensus group.

Once a day (or on demand) the Hyperliquid leaderboard is filtered for traders with a real
track record and the best ROSTER_SIZE become consensus voters:

  - profitable over the month AND all time (not just a lucky week)
  - account >= ROSTER_MIN_ACCOUNT_USD
  - weekly volume < ROSTER_MAX_WEEKLY_TURNOVER x account (excludes market makers, whose
    positions are inventory rather than views)

Auto members are voters only: they count toward consensus but are not copied 1:1.
A member who falls out of the selection keeps voting for ROSTER_DROP_DAYS before being
dropped, so the group doesn't churn daily. Traders added by hand are never touched.
"""
import time

import httpx

AUTO_SOURCE = "auto_roster"


def select(rows: list[dict], cfg) -> list[dict]:
    """Filter and rank raw leaderboard rows. Returns the best ROSTER_SIZE candidates."""
    out = []
    for row in rows:
        perf = dict(row.get("windowPerformances", []))
        wk, mo, at = (perf.get(k) or {} for k in ("week", "month", "allTime"))
        av = float(row.get("accountValue", 0) or 0)
        month_pnl = float(mo.get("pnl", 0) or 0)
        if av < cfg.roster_min_account_usd:
            continue
        if month_pnl <= 0 or float(at.get("pnl", 0) or 0) <= 0:
            continue
        if float(wk.get("vlm", 0) or 0) > cfg.roster_max_weekly_turnover * av:
            continue
        out.append({"address": row["ethAddress"].lower(), "name": row.get("displayName") or "",
                    "account_value": av, "month_pnl": month_pnl, "month_roi": float(mo.get("roi", 0) or 0)})
    out.sort(key=lambda r: -r["month_pnl"])
    return out[:cfg.roster_size]


def apply(db, picks: list[dict], cfg, now: float) -> dict:
    """Bring the auto members in line with `picks`. Returns a summary of what changed."""
    picked = {p["address"]: p for p in picks}
    existing = {r["address"]: r for r in db.q("SELECT * FROM traders")}
    added, kept, dropping, dropped, skipped_manual = [], [], [], [], 0

    for addr, p in picked.items():
        r = existing.get(addr)
        label = (p["name"] or "auto")[:60]
        if r is None:
            db.x("INSERT INTO traders(address,label,source,chain,role,auto,added_at) VALUES(?,?,?,?,?,?,?)",
                 (addr, label, AUTO_SOURCE, "hl", "voter", 1, now))
            added.append(addr)
        elif not r["auto"]:
            skipped_manual += 1  # tracked by hand already: leave it exactly as it is
        else:
            if not r["active"]:  # came back: rejoin, re-baseline its positions
                db.x("UPDATE traders SET active=1, baselined=0, out_since=NULL WHERE address=?", (addr,))
                added.append(addr)
            else:
                db.x("UPDATE traders SET out_since=NULL WHERE address=?", (addr,))
                kept.append(addr)

    grace = cfg.roster_drop_days * 86400
    for addr, r in existing.items():
        if not r["auto"] or not r["active"] or addr in picked:
            continue
        since = r["out_since"] or now
        if r["out_since"] is None:
            db.x("UPDATE traders SET out_since=? WHERE address=?", (now, addr))
        if now - since >= grace:
            db.x("UPDATE traders SET active=0 WHERE address=?", (addr,))
            db.x("DELETE FROM trader_positions WHERE address=?", (addr,))
            dropped.append(addr)
        else:
            dropping.append(addr)

    summary = {"ts": now, "candidates": len(picks), "added": len(added), "kept": len(kept),
               "leaving": len(dropping), "dropped": len(dropped), "already_manual": skipped_manual}
    db.x("INSERT OR REPLACE INTO kv(k,v) VALUES('roster_last', ?)", (repr(summary),))
    return summary


async def refresh(db, cfg, now: float | None = None, fetch=None) -> dict:
    now = now or time.time()
    if fetch is None:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.get(cfg.hl_leaderboard_url)
            r.raise_for_status()
            rows = r.json().get("leaderboardRows", [])
    else:
        rows = await fetch()
    return apply(db, select(rows, cfg), cfg, now)


def last(db) -> dict | None:
    import ast
    row = db.one("SELECT v FROM kv WHERE k='roster_last'")
    return ast.literal_eval(row["v"]) if row else None
