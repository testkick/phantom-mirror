"""Hyperliquid data access.

Phantom perps settle on Hyperliquid under each user's 0x (Ethereum) address, so a
Phantom top trader's positions are readable from Hyperliquid's public info API.

HLClient  - real API (no key needed).
DemoClient - deterministic simulator so the whole system can run offline.
"""
import math
import random
import time

import httpx


def parse_state(raw: dict) -> dict:
    """Normalise a clearinghouseState response to {account_value, positions{coin: {...}}}."""
    positions = {}
    for ap in raw.get("assetPositions", []) or []:
        p = ap.get("position", ap)
        szi = float(p.get("szi", 0) or 0)
        if szi == 0:
            continue
        lev = p.get("leverage") or {}
        positions[p["coin"]] = {
            "szi": szi,
            "entry_px": float(p["entryPx"]) if p.get("entryPx") not in (None, "") else None,
            "leverage": float(lev.get("value", 0) or 0) if isinstance(lev, dict) else float(lev or 0),
        }
    av = float((raw.get("marginSummary") or {}).get("accountValue", 0) or 0)
    return {"account_value": av, "positions": positions}


STABLES = {"USDC", "USDT0", "USDH", "USDE"}
UNIFIED = {"unifiedAccount", "portfolioMargin"}


class HLClient:
    """Reads every perp market a trader uses:
    - HIP-3 markets (stocks, commodities: coins like "xyz:TSLA") live on separate perp dexes.
      Each trader's dexes are found by sweeping all dexes every `discover_seconds`, a few
      traders per poll so the sweep never bursts past Hyperliquid's rate limit.
    - Unified / portfolio-margin accounts keep collateral as spot USDC; per-dex accountValue
      is only margin in use. Their equity = spot stables (free + held) + unrealized P&L.
    """

    def __init__(self, info_url: str, leaderboard_url: str, dexs: list[str] | None = None,
                 discover_seconds: float = 1800, sweeps_per_poll: int = 3):
        self.info_url = info_url
        self.leaderboard_url = leaderboard_url
        self.forced = {d for d in (dexs or []) if d}  # HL_DEXS extras, always read
        self.discover_seconds = discover_seconds
        self.sweeps_per_poll = sweeps_per_poll
        self.meta: dict[str, dict] = {}   # address -> {ts, dexs:set, abstraction}
        self.all_dexs: list[str] = []
        self._dexs_ts = 0.0
        self._sweeps_left = sweeps_per_poll
        self.http = httpx.AsyncClient(timeout=15, headers={"Content-Type": "application/json"})

    async def _info(self, body: dict):
        r = await self.http.post(self.info_url, json=body)
        r.raise_for_status()
        return r.json()

    async def _perp_dexs(self) -> list[str]:
        if time.time() - self._dexs_ts > 3600:
            self.all_dexs = [d["name"] for d in await self._info({"type": "perpDexs"}) if d and d.get("name")]
            self._dexs_ts = time.time()
        return self.all_dexs

    def dexs_in_use(self) -> set[str]:
        used = set(self.forced)
        for m in self.meta.values():
            used |= m["dexs"]
        return used

    async def all_mids(self) -> dict[str, float]:
        self._sweeps_left = self.sweeps_per_poll  # all_mids is called once at the start of each poll
        mids: dict[str, float] = {}
        for dex in [""] + sorted(self.dexs_in_use()):
            body = {"type": "allMids"}
            if dex:
                body["dex"] = dex
            data = await self._info(body)
            for k, v in data.items():
                if k.startswith("@"):  # spot pair ids
                    continue
                try:
                    mids[k] = float(v)
                except (TypeError, ValueError):
                    pass
        return mids

    async def _dex_state(self, address: str, dex: str) -> dict:
        body = {"type": "clearinghouseState", "user": address}
        if dex:
            body["dex"] = dex
        return parse_state(await self._info(body))

    async def state(self, address: str) -> dict:
        m = self.meta.get(address)
        sweep = (m is None or time.time() - m["ts"] > self.discover_seconds) and self._sweeps_left > 0
        known = set(m["dexs"]) if m else set()
        per_dex: dict[str, dict] = {"": await self._dex_state(address, "")}
        if sweep:
            self._sweeps_left -= 1
            abstraction = await self._info({"type": "userAbstraction", "user": address})
            found = set()
            for dex in await self._perp_dexs():
                st = await self._dex_state(address, dex)
                per_dex[dex] = st
                if st["positions"] or st["account_value"] > 0:
                    found.add(dex)
            # keep dexes we already knew about until a sweep shows them empty
            m = {"ts": time.time(), "dexs": found, "abstraction": abstraction if isinstance(abstraction, str) else ""}
            self.meta[address] = m
        for dex in sorted((m["dexs"] if m else set()) | self.forced):
            if dex not in per_dex:
                per_dex[dex] = await self._dex_state(address, dex)
        if m and per_dex.keys() - {""}:
            m["dexs"] |= {d for d, st in per_dex.items() if d and st["positions"]}

        positions: dict = {}
        perp_av = 0.0
        # Positions on a market we're seeing for the first time existed before we looked:
        # treat them as pre-tracking, never as fresh opens to copy at today's price.
        baseline_coins: set[str] = set()
        for dex, st in per_dex.items():
            positions.update(st["positions"])
            perp_av += st["account_value"]
            if dex and dex not in known and dex not in self.forced:
                baseline_coins |= set(st["positions"])
        av = perp_av
        if m and m["abstraction"] in UNIFIED:
            spot = await self._info({"type": "spotClearinghouseState", "user": address})
            total = hold = 0.0
            for b in spot.get("balances", []) or []:
                if b.get("coin") in STABLES:
                    total += float(b.get("total") or 0)
                    hold += float(b.get("hold") or 0)
            # per-dex accountValue = margin held + unrealized P&L, and the held margin is
            # already inside the spot total, so subtract it once.
            av = total + (perp_av - hold)
        return {"account_value": av, "positions": positions, "baseline_coins": baseline_coins}

    async def leaderboard(self, window: str = "week", top: int = 10, min_account_value: float = 0) -> list[dict]:
        """Best-effort: Hyperliquid's leaderboard file is undocumented and large.
        This is the Hyperliquid-wide leaderboard, NOT Phantom's own Top Traders list."""
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.get(self.leaderboard_url)
            r.raise_for_status()
            rows = r.json().get("leaderboardRows", [])
        out = []
        for row in rows:
            perf = dict(row.get("windowPerformances", []))
            w = perf.get(window) or {}
            av = float(row.get("accountValue", 0) or 0)
            if av < min_account_value:
                continue
            out.append({
                "address": row["ethAddress"].lower(),
                "label": row.get("displayName") or "",
                "account_value": av,
                "pnl": float(w.get("pnl", 0) or 0),
                "roi": float(w.get("roi", 0) or 0),
            })
        out.sort(key=lambda r: r["pnl"], reverse=True)
        return out[:top]

    async def close(self):
        await self.http.aclose()


class DemoClient:
    """Simulated market + traders. Prices random-walk; traders open/close with a shared bias
    so consensus signals actually occur."""

    COINS = {"BTC": 95_000.0, "ETH": 3_400.0, "SOL": 180.0, "HYPE": 38.0, "DOGE": 0.24}
    TRADERS = {
        "0x" + f"{i:x}" * 40: name
        for i, name in enumerate(["demo-alpha", "demo-bravo", "demo-charlie", "demo-delta", "demo-echo"], start=1)
    }

    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.px = dict(self.COINS)
        self.bias = {c: self.rng.choice([-1, 1]) for c in self.COINS}
        self.acct = {a: {"account_value": self.rng.uniform(50_000, 500_000), "positions": {}} for a in self.TRADERS}

    def step(self):
        for c in self.px:
            drift = 0.0004 * self.bias[c]
            self.px[c] *= math.exp(drift + self.rng.gauss(0, 0.002))
            if self.rng.random() < 0.02:
                self.bias[c] *= -1
        for a, acct in self.acct.items():
            for c in self.px:
                pos = acct["positions"].get(c)
                r = self.rng.random()
                if pos is None and r < 0.06:
                    side = self.bias[c] if self.rng.random() < 0.8 else -self.bias[c]
                    notional = acct["account_value"] * self.rng.uniform(0.2, 1.5)
                    acct["positions"][c] = {"szi": side * notional / self.px[c], "entry_px": self.px[c], "leverage": 10.0}
                elif pos is not None and r < 0.04:
                    acct["account_value"] += pos["szi"] * (self.px[c] - pos["entry_px"])
                    del acct["positions"][c]
                elif pos is not None and r < 0.06:
                    pos["szi"] *= self.rng.choice([0.5, 1.5])

    async def all_mids(self) -> dict[str, float]:
        self.step()
        return dict(self.px)

    async def state(self, address: str) -> dict:
        a = self.acct.get(address)
        if a is None:
            return {"account_value": 0.0, "positions": {}}
        return {"account_value": a["account_value"], "positions": {c: dict(p) for c, p in a["positions"].items()}}

    async def leaderboard(self, window="week", top=10, min_account_value=0):
        return [{"address": a, "label": n, "account_value": self.acct[a]["account_value"], "pnl": 0.0, "roi": 0.0}
                for a, n in list(self.TRADERS.items())[:top]]

    async def close(self):
        pass
