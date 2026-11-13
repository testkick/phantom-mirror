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


class HLClient:
    def __init__(self, info_url: str, leaderboard_url: str, dexs: list[str] | None = None):
        self.info_url = info_url
        self.leaderboard_url = leaderboard_url
        self.dexs = dexs or [""]
        self.http = httpx.AsyncClient(timeout=15, headers={"Content-Type": "application/json"})

    async def _info(self, body: dict):
        r = await self.http.post(self.info_url, json=body)
        r.raise_for_status()
        return r.json()

    async def all_mids(self) -> dict[str, float]:
        mids: dict[str, float] = {}
        for dex in self.dexs:
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

    async def state(self, address: str) -> dict:
        merged = {"account_value": 0.0, "positions": {}}
        for dex in self.dexs:
            body = {"type": "clearinghouseState", "user": address}
            if dex:
                body["dex"] = dex
            st = parse_state(await self._info(body))
            merged["account_value"] += st["account_value"]
            merged["positions"].update(st["positions"])
        return merged

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
