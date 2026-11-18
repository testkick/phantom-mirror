"""Suggested traders to follow.

Phantom's own Top Traders list has no public API, so suggestions come from public leaderboards:
  perps - Hyperliquid's leaderboard (every Phantom perps trader is a Hyperliquid account)
  spot  - Solana Tracker's PnL leaderboard (needs SOLANATRACKER_API_KEY)
Results are cached; the Hyperliquid file is large, so it is fetched at most every DISCOVER_CACHE_SECONDS.
"""
import asyncio
import time

import httpx

PERP_WINDOWS = {"day": "day", "week": "week", "month": "month", "all": "allTime"}
SPOT_WINDOWS = {"day": 1, "week": 7, "month": 30}


class Discovery:
    def __init__(self, cfg):
        self.cfg = cfg
        self._cache: dict[tuple, tuple[float, list]] = {}
        self._lock = asyncio.Lock()

    async def get(self, category: str, window: str) -> list[dict]:
        key = (category, window)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cfg.discover_cache_seconds:
            return hit[1]
        async with self._lock:
            hit = self._cache.get(key)  # another request may have filled it while we waited
            if hit and time.time() - hit[0] < self.cfg.discover_cache_seconds:
                return hit[1]
            if category == "perps":
                await self._load_perps()
            elif category == "spot":
                self._cache[key] = (time.time(), await self._load_spot(window))
            else:
                raise ValueError("unknown category")
        return self._cache[key][1]

    async def _load_perps(self) -> None:
        """One download fills every perps window."""
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.get(self.cfg.hl_leaderboard_url)
            r.raise_for_status()
            rows = r.json().get("leaderboardRows", [])
        now = time.time()
        n, min_av = self.cfg.discover_limit, self.cfg.discover_min_account_usd
        for w, hl_key in PERP_WINDOWS.items():
            out = []
            for row in rows:
                perf = dict(row.get("windowPerformances", []))
                p = perf.get(hl_key) or {}
                av = float(row.get("accountValue", 0) or 0)
                pnl = float(p.get("pnl", 0) or 0)
                if av < min_av or pnl <= 0:
                    continue
                out.append({"address": row["ethAddress"].lower(), "chain": "hl", "name": row.get("displayName") or "",
                            "account_value": av, "pnl": pnl, "roi": float(p.get("roi", 0) or 0),
                            "volume": float(p.get("vlm", 0) or 0)})
            out.sort(key=lambda x: -x["pnl"])
            self._cache[("perps", w)] = (now, out[:n])

    async def _load_spot(self, window: str) -> list[dict]:
        key = self.cfg.solanatracker_api_key
        if not key:
            raise RuntimeError("Set SOLANATRACKER_API_KEY on the service to see spot suggestions.")
        days = SPOT_WINDOWS[window]
        params = {"sort": "realized", "direction": "desc", "limit": 100, "days": days,  # over-fetch; filtered below
                  "excludeArbitrage": "true", "minTrades": 20, "minDays": min(3, days),
                  # skip one-hit wonders whose whole profit came from a single token
                  "maxSingleTokenPct": 80}
        async with httpx.AsyncClient(timeout=30, headers={"x-api-key": key}) as c:
            r = await c.get(f"{self.cfg.solanatracker_base}/v2/pnl/leaderboard/top", params=params)
            r.raise_for_status()
            traders = r.json().get("traders", [])
        cfg = self.cfg
        out = []
        for t in traders:
            period = t.get("period") or {}
            ident = t.get("identity") or {}
            if ident.get("type") == "bot":
                continue
            trades = (t.get("counts") or {}).get("trades") or 0
            wr = t.get("winRate") if t.get("winRate") is not None else (period.get("days") or {}).get("winRate")
            roi = float(period.get("roi") or 0) / 100
            if trades > cfg.discover_spot_max_trades_per_day * days:   # too fast to copy by polling
                continue
            if wr is not None and wr > cfg.discover_spot_max_win_rate:  # snipers / insiders
                continue
            if roi > cfg.discover_spot_max_roi:                          # launch-entry returns nobody can match
                continue
            out.append({"address": t["wallet"], "chain": "sol", "name": ident.get("name") or "",
                        "twitter": ident.get("twitter") or "", "pnl": float(period.get("realized") or 0),
                        "roi": float(period.get("roi") or 0) / 100,  # API gives percent
                        "volume": float(period.get("volume") or 0),
                        "win_rate": (t.get("winRate") if t.get("winRate") is not None else (period.get("days") or {}).get("winRate")),
                        "trades": (t.get("counts") or {}).get("trades")})
        return out[:cfg.discover_limit]


class DemoDiscovery:
    def __init__(self, cfg):
        self.cfg = cfg

    async def get(self, category, window):
        import random
        rng = random.Random(f"{category}{window}")
        if category == "perps":
            return [{"address": "0x" + f"{rng.getrandbits(160):040x}", "chain": "hl", "name": rng.choice(["", "", "whale.eth", "degen"]),
                     "account_value": rng.uniform(5e4, 5e6), "pnl": rng.uniform(1e4, 2e6), "roi": rng.uniform(0.05, 3),
                     "volume": rng.uniform(1e6, 5e8)} for _ in range(8)]
        if category == "spot":
            alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
            return [{"address": "".join(rng.choice(alphabet) for _ in range(44)), "chain": "sol",
                     "name": rng.choice(["", "", "scotty78?", "ansem-alt"]), "twitter": "", "pnl": rng.uniform(1e4, 9e5),
                     "roi": rng.uniform(0.1, 8), "volume": rng.uniform(1e5, 2e7), "win_rate": rng.uniform(30, 75),
                     "trades": rng.randint(20, 900)} for _ in range(8)]
        raise ValueError("unknown category")
