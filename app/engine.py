"""Monitoring + paper mirroring logic. Pure Python over the DB; no network.

Flow per poll:
  1. ingest_trader()  - diff each trader's live positions against the stored snapshot -> events
  2. run_copy()       - strategy A: mirror each trader proportionally
  3. run_consensus()  - strategy B: trade only when several traders agree on a coin
  4. snapshot_equity()

Not modelled: funding payments, liquidations, order-book depth (fills are mid +/- slippage).
"""
from .config import Config
from .db import DB
from .sol import CASH_MINTS, SOL_MINT

EPS = 1e-12
CONSENSUS_SOURCE = "consensus"
STRATEGIES = ("copy", "consensus", "spot_copy")


def _sign(x: float) -> int:
    return (x > EPS) - (x < -EPS)


class Engine:
    def __init__(self, db: DB, cfg: Config):
        self.db = db
        self.cfg = cfg

    # ------------------------------------------------------------------ monitoring
    def ingest_trader(self, address: str, state: dict, mids: dict, now: float) -> list[dict]:
        """Store the trader's latest positions and return the change events."""
        db = self.db
        trader = db.one("SELECT * FROM traders WHERE address=?", (address,))
        if trader is None:
            return []
        new = state["positions"]
        prev = {r["coin"]: r for r in db.q("SELECT * FROM trader_positions WHERE address=?", (address,))}
        events: list[dict] = []
        first = not trader["baselined"]

        for coin in set(prev) | set(new):
            p = prev.get(coin)
            n = new.get(coin)
            prev_szi = p["szi"] if p else 0.0
            new_szi = n["szi"] if n else 0.0
            if abs(new_szi - prev_szi) <= 1e-9 * max(abs(prev_szi), abs(new_szi), 1e-9):
                continue  # unchanged

            # `base` is the part of the position held before tracking began. Only size above it is
            # copied, so adding to an old position is mirrored but the old size never is.
            if first or (p is None and coin in state.get("baseline_coins", ())):
                base, kind = (0.0 if self.cfg.copy_existing else new_szi), None
            elif _sign(prev_szi) == 0:
                base, kind = 0.0, "open"
            elif _sign(new_szi) == 0:
                base, kind = 0.0, "close"
            elif _sign(prev_szi) != _sign(new_szi):
                base, kind = 0.0, "flip"
            else:
                base = p["base"]
                if abs(new_szi) < abs(base):  # sold into the old part: shrink it
                    base = new_szi
                kind = "increase" if abs(new_szi) > abs(prev_szi) else "reduce"
            baseline = 1 if abs(base) > EPS else 0

            if n is None:
                db.x("DELETE FROM trader_positions WHERE address=? AND coin=?", (address, coin))
            else:
                db.x(
                    "INSERT INTO trader_positions(address,coin,szi,entry_px,leverage,baseline,base,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(address,coin) DO UPDATE SET "
                    "szi=excluded.szi, entry_px=excluded.entry_px, leverage=excluded.leverage, "
                    "baseline=excluded.baseline, base=excluded.base, updated_at=excluded.updated_at",
                    (address, coin, new_szi, n.get("entry_px"), n.get("leverage"), baseline, base, now),
                )
            if kind:
                ev = {"ts": now, "address": address, "coin": coin, "kind": kind,
                      "prev_szi": prev_szi, "new_szi": new_szi, "px": mids.get(coin)}
                db.x("INSERT INTO events(ts,address,coin,kind,prev_szi,new_szi,px) VALUES(?,?,?,?,?,?,?)",
                     tuple(ev.values()))
                events.append(ev)

        db.x("UPDATE traders SET baselined=1, account_value=?, last_seen=?, last_error=NULL WHERE address=?",
             (state["account_value"], now, address))
        return events

    # ------------------------------------------------------------------ paper execution
    def fill(self, strategy: str, source: str, coin: str, delta: float, mid: float, now: float, reason: str,
             fee_bps: float | None = None, slippage_bps: float | None = None) -> None:
        """Apply a simulated market order of signed size `delta` at mid +/- slippage."""
        if abs(delta) <= EPS or not mid or mid <= 0:
            return
        slip = (self.cfg.slippage_bps if slippage_bps is None else slippage_bps) / 1e4
        px = mid * (1 + slip) if delta > 0 else mid * (1 - slip)
        fee = abs(delta) * px * (self.cfg.fee_bps if fee_bps is None else fee_bps) / 1e4
        db = self.db
        cur = db.one("SELECT * FROM paper_positions WHERE strategy=? AND source=? AND coin=?", (strategy, source, coin))
        size = cur["size"] if cur else 0.0
        entry = cur["entry_px"] if cur else 0.0
        realized, closing = 0.0, 0

        if size and _sign(delta) != _sign(size):  # reducing / closing / flipping
            closed_qty = min(abs(delta), abs(size))
            realized = closed_qty * (px - entry) * _sign(size)
            closing = 1

        new_size = size + delta
        key = (strategy, source, coin)
        if abs(new_size) <= 1e-9 * max(abs(size), abs(delta)):
            db.x("DELETE FROM paper_positions WHERE strategy=? AND source=? AND coin=?", key)
        elif size == 0 or _sign(new_size) != _sign(size):  # fresh position (or flipped through zero)
            db.x("INSERT OR REPLACE INTO paper_positions(strategy,source,coin,size,entry_px,opened_at) VALUES(?,?,?,?,?,?)",
                 (*key, new_size, px, now))
        elif abs(new_size) > abs(size):  # adding: weighted-average entry
            new_entry = (abs(size) * entry + abs(delta) * px) / abs(new_size)
            db.x("UPDATE paper_positions SET size=?, entry_px=? WHERE strategy=? AND source=? AND coin=?",
                 (new_size, new_entry, *key))
        else:  # partial reduce: entry unchanged
            db.x("UPDATE paper_positions SET size=? WHERE strategy=? AND source=? AND coin=?", (new_size, *key))

        db.x("INSERT INTO paper_fills(ts,strategy,source,coin,delta,px,fee,realized_pnl,closing,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
             (now, strategy, source, coin, delta, px, fee, realized, closing, reason))

    # ------------------------------------------------------------------ strategy A: 1:1 copy
    def copy_targets(self, mids: dict) -> dict[tuple[str, str], float]:
        """(trader, coin) -> target paper size. Each trader gets COPY_ALLOC_USD of paper equity and
        their positions are scaled by alloc / their account value (same leverage profile)."""
        cfg = self.cfg
        targets: dict[tuple[str, str], float] = {}
        rows = self.db.q(
            "SELECT p.address, p.coin, p.szi - p.base AS szi, t.account_value FROM trader_positions p "
            "JOIN traders t ON t.address=p.address WHERE t.active=1 AND t.role='copy'"
        )
        for r in rows:
            av, mid = r["account_value"] or 0.0, mids.get(r["coin"])
            if av < cfg.min_account_value or not mid:
                continue
            tgt = r["szi"] * cfg.copy_alloc_usd / av
            cap = cfg.copy_alloc_usd * cfg.copy_max_leverage / mid
            tgt = max(-cap, min(cap, tgt))
            targets[(r["address"], r["coin"])] = tgt
        return targets

    def run_copy(self, mids: dict, now: float) -> None:
        cfg = self.cfg
        targets = self.copy_targets(mids)
        current = {(r["source"], r["coin"]): r["size"]
                   for r in self.db.q("SELECT * FROM paper_positions WHERE strategy='copy'")}
        for key in set(targets) | set(current):
            source, coin = key
            mid = mids.get(coin)
            if not mid:
                continue  # no price this round; try again next poll
            cur, tgt = current.get(key, 0.0), targets.get(key, 0.0)
            if abs(tgt) * mid < cfg.min_order_usd:
                tgt = 0.0
            if cur == 0 and tgt == 0:
                continue
            if tgt == 0:
                reason = "trader closed"
            elif cur == 0:
                reason = "trader opened"
            elif _sign(cur) != _sign(tgt):
                reason = "trader flipped"
            elif abs(tgt - cur) / abs(cur) > cfg.copy_rebalance_threshold:
                reason = "trader resized"
            else:
                continue
            self.fill("copy", source, coin, tgt - cur, mid, now, reason)

    # ------------------------------------------------------------------ strategy B: consensus
    def thresholds(self) -> dict:
        """Vote minimums that grow with the group, so a big group needs proportionally more agreement."""
        import math
        cfg = self.cfg
        n = self.db.one("SELECT COUNT(*) n FROM traders WHERE active=1 AND chain='hl'")["n"]
        return {"members": n,
                "entry": max(cfg.consensus_min_traders, math.ceil(cfg.consensus_min_share * n - 1e-9)),
                "exit": max(cfg.consensus_exit_min_traders, math.ceil(cfg.consensus_exit_min_share * n - 1e-9))}

    def consensus_board(self) -> dict[str, dict]:
        """coin -> {long, short, signal}. One vote per active trader holding the coin."""
        cfg = self.cfg
        min_votes = self.thresholds()["entry"]
        cond = "" if cfg.consensus_include_existing else " AND p.baseline=0"
        board: dict[str, dict] = {}
        for r in self.db.q(
            "SELECT p.coin, p.szi FROM trader_positions p JOIN traders t ON t.address=p.address "
            "WHERE t.active=1" + cond
        ):
            b = board.setdefault(r["coin"], {"long": 0, "short": 0})
            b["long" if r["szi"] > 0 else "short"] += 1
        for b in board.values():
            total = b["long"] + b["short"]
            b["signal"] = 0
            for side, n in ((1, b["long"]), (-1, b["short"])):
                if n >= min_votes and n / total >= cfg.consensus_agreement:
                    b["signal"] = side
        return board

    def run_consensus(self, mids: dict, now: float) -> None:
        cfg = self.cfg
        board = self.consensus_board()
        exit_min = self.thresholds()["exit"]
        current = {r["coin"]: r["size"] for r in self.db.q("SELECT * FROM paper_positions WHERE strategy='consensus'")}

        # exits first (frees slots)
        for coin, size in list(current.items()):
            mid = mids.get(coin)
            if not mid:
                continue
            b = board.get(coin, {"long": 0, "short": 0, "signal": 0})
            mine = b["long"] if size > 0 else b["short"]
            total = b["long"] + b["short"]
            hold = mine >= exit_min and total and mine / total >= cfg.consensus_exit_agreement
            if not hold or (b["signal"] and b["signal"] != _sign(size)):
                self.fill("consensus", CONSENSUS_SOURCE, coin, -size, mid, now,
                          f"consensus lost ({b['long']}L/{b['short']}S)")
                del current[coin]

        # position size: equal weight spreads current equity across all slots, so more positions
        # never means more total exposure; "fixed" uses CONSENSUS_NOTIONAL_USD per signal
        if cfg.consensus_sizing == "equal_weight":
            target_usd = max(0.0, self.stats("consensus", mids)["equity"]) / cfg.consensus_max_positions
            # resize held positions that drifted far from the target (also shrinks old $1,000 positions)
            for coin, size in list(current.items()):
                mid = mids.get(coin)
                if not mid or target_usd <= 0:
                    continue
                cur_usd = abs(size) * mid
                if abs(cur_usd - target_usd) / target_usd > cfg.consensus_rebalance_threshold:
                    new_size = _sign(size) * target_usd / mid
                    self.fill("consensus", CONSENSUS_SOURCE, coin, new_size - size, mid, now,
                              f"rebalance to ${target_usd:,.0f}")
                    current[coin] = new_size
        else:
            target_usd = cfg.consensus_notional_usd

        # entries: strongest agreement first
        candidates = sorted(
            ((c, b) for c, b in board.items() if b["signal"] and c not in current and mids.get(c)),
            key=lambda cb: -max(cb[1]["long"], cb[1]["short"]),
        )
        for coin, b in candidates:
            if len(current) >= cfg.consensus_max_positions:
                break
            if target_usd < cfg.min_order_usd:
                break
            size = b["signal"] * target_usd / mids[coin]
            self.fill("consensus", CONSENSUS_SOURCE, coin, size, mids[coin], now,
                      f"consensus {b['long']}L/{b['short']}S")
            current[coin] = size

    def run_strategies(self, mids: dict, now: float) -> None:
        self.run_copy(mids, now)
        self.run_consensus(mids, now)

    # ------------------------------------------------------------------ spot monitoring (Solana)
    def ingest_spot(self, address: str, holdings: dict, prices: dict[str, dict], now: float) -> list[dict]:
        """Diff a Solana wallet's token balances against the stored snapshot.
        SOL/USDC/USDT are cash: they count toward portfolio value but produce no events."""
        db = self.db
        trader = db.one("SELECT * FROM traders WHERE address=?", (address,))
        if trader is None:
            return []
        first = not trader["baselined"]
        new = {m: a for m, a in holdings["tokens"].items() if m not in CASH_MINTS}
        prev = {r["mint"]: r for r in db.q("SELECT * FROM spot_holdings WHERE address=?", (address,))}
        events: list[dict] = []
        for mint in set(prev) | set(new):
            p = prev.get(mint)
            old_amt = p["amount"] if p else 0.0
            new_amt = new.get(mint, 0.0)
            if abs(new_amt - old_amt) <= 1e-9 * max(old_amt, new_amt, 1e-9):
                continue
            if first:
                base, kind = (0.0 if self.cfg.copy_existing else new_amt), None
            elif old_amt <= EPS:
                base, kind = 0.0, "buy"
            elif new_amt <= EPS:
                base, kind = 0.0, "sell all"
            else:
                base = min(p["base"], new_amt)  # selling eats the old bag first
                kind = "buy more" if new_amt > old_amt else "sell part"
            baseline = 1 if base > EPS else 0
            if new_amt <= EPS:
                db.x("DELETE FROM spot_holdings WHERE address=? AND mint=?", (address, mint))
            else:
                db.x("INSERT INTO spot_holdings(address,mint,amount,baseline,base,updated_at) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(address,mint) DO UPDATE SET amount=excluded.amount, baseline=excluded.baseline, "
                     "base=excluded.base, updated_at=excluded.updated_at", (address, mint, new_amt, baseline, base, now))
            if kind:
                px = (prices.get(mint) or {}).get("price")
                ev = {"ts": now, "address": address, "coin": mint, "kind": kind,
                      "prev_szi": old_amt, "new_szi": new_amt, "px": px}
                db.x("INSERT INTO events(ts,address,coin,kind,prev_szi,new_szi,px) VALUES(?,?,?,?,?,?,?)",
                     tuple(ev.values()))
                events.append(ev)

        sol_px = (prices.get(SOL_MINT) or {}).get("price") or 0.0
        value = holdings["sol"] * sol_px
        for m, a in holdings["tokens"].items():
            pr = (prices.get(m) or {}).get("price")
            if pr:
                value += a * pr
        # keep the previous value if SOL couldn't be priced this round
        db.x("UPDATE traders SET baselined=1, account_value=CASE WHEN ?>0 THEN ? ELSE account_value END, "
             "last_seen=?, last_error=NULL WHERE address=?", (sol_px, value, now, address))
        return events

    # ------------------------------------------------------------------ strategy C: spot copy
    def run_spot_copy(self, prices: dict[str, dict], now: float) -> None:
        """Each spot trader gets SPOT_ALLOC_USD; each holding is mirrored at the same share of
        portfolio value, capped at SPOT_MAX_POSITION_PCT. New entries need minimum liquidity."""
        cfg = self.cfg
        targets: dict[tuple[str, str], float] = {}
        for r in self.db.q(
            "SELECT h.address, h.mint, h.amount - h.base AS amount, t.account_value FROM spot_holdings h "
            "JOIN traders t ON t.address=h.address WHERE t.active=1 AND t.chain='sol' AND h.amount - h.base > 0"
        ):
            pr = prices.get(r["mint"])
            av = r["account_value"] or 0.0
            if not pr or av < cfg.min_account_value:
                continue
            tgt = r["amount"] * cfg.spot_alloc_usd / av
            cap = cfg.spot_alloc_usd * cfg.spot_max_position_pct / pr["price"]
            targets[(r["address"], r["mint"])] = min(tgt, cap)
        current = {(r["source"], r["coin"]): r["size"]
                   for r in self.db.q("SELECT * FROM paper_positions WHERE strategy='spot_copy'")}
        for key in set(targets) | set(current):
            source, mint = key
            pr = prices.get(mint)
            if not pr:
                continue  # no price this round: never sell blind
            cur, tgt = current.get(key, 0.0), targets.get(key, 0.0)
            if tgt * pr["price"] < cfg.min_order_usd:
                tgt = 0.0
            if cur == 0 and tgt == 0:
                continue
            if tgt == 0:
                reason = "trader sold"
            elif cur == 0:
                if pr["liquidity"] < cfg.spot_min_liquidity_usd:
                    continue  # too thin to enter
                reason = "trader bought"
            elif abs(tgt - cur) / cur > cfg.copy_rebalance_threshold:
                reason = "trader resized"
            else:
                continue
            self.fill("spot_copy", source, mint, tgt - cur, pr["price"], now, reason,
                      fee_bps=cfg.spot_fee_bps, slippage_bps=cfg.spot_slippage_bps)

    # ------------------------------------------------------------------ accounting
    def stats(self, strategy: str, mids: dict) -> dict:
        db = self.db
        agg = db.one(
            "SELECT COALESCE(SUM(realized_pnl),0) realized, COALESCE(SUM(fee),0) fees, COUNT(*) fills, "
            "COALESCE(SUM(closing),0) closes, "
            "COALESCE(SUM(CASE WHEN closing=1 AND realized_pnl>0 THEN 1 ELSE 0 END),0) wins "
            "FROM paper_fills WHERE strategy=?", (strategy,))
        unreal, exposure, n_open = 0.0, 0.0, 0
        for p in db.q("SELECT * FROM paper_positions WHERE strategy=?", (strategy,)):
            mid = mids.get(p["coin"]) or p["entry_px"]
            unreal += p["size"] * (mid - p["entry_px"])
            exposure += abs(p["size"]) * mid
            n_open += 1
        net_realized = agg["realized"] - agg["fees"]
        equity = self.cfg.starting_equity + net_realized + unreal
        return {
            "strategy": strategy, "equity": equity, "pnl": equity - self.cfg.starting_equity,
            "realized": agg["realized"], "fees": agg["fees"], "unrealized": unreal,
            "exposure": exposure, "open_positions": n_open, "fills": agg["fills"],
            "closed": agg["closes"], "win_rate": (agg["wins"] / agg["closes"]) if agg["closes"] else None,
        }

    def snapshot_equity(self, mids: dict, now: float) -> None:
        for s in STRATEGIES:
            st = self.stats(s, mids)
            self.db.x("INSERT INTO equity(ts,strategy,equity,realized,unrealized) VALUES(?,?,?,?,?)",
                      (now, s, st["equity"], st["realized"] - st["fees"], st["unrealized"]))
