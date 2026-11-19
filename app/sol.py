"""Solana spot data access.

Holdings come from a Solana RPC (getBalance + getTokenAccountsByOwner for both token
programs). Prices, liquidity and symbols come from Jupiter's public Price V3 and Tokens V2
APIs. Holdings are diffed between polls, the same way perps positions are, so this works
for any wallet without parsing individual DEX transactions.
"""
import random
import re

import httpx

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY4XjDDgnEnJbxXh"
# Treated as cash: counted in portfolio value, never mirrored.
CASH_MINTS = {SOL_MINT, USDC_MINT, USDT_MINT}
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
)
SOL_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class SolClient:
    def __init__(self, rpc_url: str, jup_base: str, jup_key: str = ""):
        self.rpc_url = rpc_url
        self.jup_base = jup_base.rstrip("/")
        headers = {"x-api-key": jup_key} if jup_key else {}
        self.http = httpx.AsyncClient(timeout=20)
        self.jup = httpx.AsyncClient(timeout=20, headers=headers)
        self._id = 0

    async def _rpc(self, method: str, params: list):
        self._id += 1
        r = await self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"RPC {method}: {body['error']}")
        return body["result"]

    async def holdings(self, owner: str) -> dict:
        """{'sol': float, 'tokens': {mint: ui_amount}} (zero balances dropped)."""
        sol = (await self._rpc("getBalance", [owner, {"commitment": "confirmed"}]))["value"] / 1e9
        tokens: dict[str, float] = {}
        for prog in TOKEN_PROGRAMS:
            res = await self._rpc("getTokenAccountsByOwner",
                                  [owner, {"programId": prog}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
            for acc in res["value"]:
                info = acc["account"]["data"]["parsed"]["info"]
                amt = float(info["tokenAmount"].get("uiAmountString") or info["tokenAmount"].get("uiAmount") or 0)
                if amt > 0:
                    tokens[info["mint"]] = tokens.get(info["mint"], 0.0) + amt
        # wrapped SOL in a token account is still SOL
        sol += tokens.pop(SOL_MINT, 0.0)
        return {"sol": sol, "tokens": tokens}

    async def sol_balance(self, owner: str) -> float:
        return (await self._rpc("getBalance", [owner, {"commitment": "confirmed"}]))["value"] / 1e9

    async def prices(self, mints: list[str], max_calls: int = 1_000_000) -> dict[str, dict]:
        """mint -> {'price', 'liquidity', 'decimals'}; tokens Jupiter can't price are omitted.
        Asks in batches of 50, in the order given, for at most `max_calls` batches. Stops early on a
        rate limit and returns what it has; `self.priced` lists the mints actually asked about."""
        out: dict[str, dict] = {}
        mints = list(dict.fromkeys(mints))
        self.priced: list[str] = []
        for i in range(0, min(len(mints), max_calls * 50), 50):
            batch = mints[i:i + 50]
            r = await self.jup.get(f"{self.jup_base}/price/v3", params={"ids": ",".join(batch)})
            if r.status_code == 429:
                break
            r.raise_for_status()
            self.priced += batch
            for mint, d in (r.json() or {}).items():
                if d and d.get("usdPrice") is not None:
                    out[mint] = {"price": float(d["usdPrice"]), "liquidity": float(d.get("liquidity") or 0),
                                 "decimals": d.get("decimals")}
        return out

    async def token_info(self, mints: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(mints), 100):
            r = await self.jup.get(f"{self.jup_base}/tokens/v2/search", params={"query": ",".join(mints[i:i + 100])})
            r.raise_for_status()
            for t in r.json() or []:
                out[t["id"]] = {"symbol": t.get("symbol") or t["id"][:6], "name": t.get("name") or ""}
        return out

    async def close(self):
        await self.http.aclose()
        await self.jup.aclose()


class DemoSolClient:
    """Simulated spot wallets for offline runs."""

    TOKENS = {
        "DemoMint1111111111111111111111111111111111": ("DCTY", 0.0012, 400_000),
        "DemoMint2222222222222222222222222222222222": ("ROCCO", 0.00045, 150_000),
        "DemoMint3333333333333333333333333333333333": ("JUP", 0.85, 50_000_000),
        "DemoMint4444444444444444444444444444444444": ("BONK", 0.000021, 20_000_000),
    }
    WALLETS = {"DemoSpotWa11et1111111111111111111111111111": "demo-spot-scotty",
               "DemoSpotWa11et2222222222222222222222222222": "demo-spot-whale"}

    def __init__(self, seed: int = 11):
        self.rng = random.Random(seed)
        self.px = {m: p for m, (_, p, _) in self.TOKENS.items()}
        self.px[SOL_MINT] = 180.0
        self.w = {a: {"sol": self.rng.uniform(200, 800), "tokens": {}} for a in self.WALLETS}

    def step(self):
        for m in self.px:
            vol = 0.002 if m == SOL_MINT else 0.02
            self.px[m] *= max(0.2, 1 + self.rng.gauss(0.001, vol))
        for w in self.w.values():
            for m in self.TOKENS:
                r = self.rng.random()
                held = w["tokens"].get(m, 0)
                if not held and r < 0.05 and w["sol"] > 5:
                    spend = w["sol"] * self.rng.uniform(0.05, 0.25)
                    w["sol"] -= spend
                    w["tokens"][m] = spend * self.px[SOL_MINT] / self.px[m]
                elif held and r < 0.04:
                    frac = self.rng.choice([0.5, 1.0])
                    w["sol"] += held * frac * self.px[m] / self.px[SOL_MINT]
                    w["tokens"][m] = held * (1 - frac)
                    if not w["tokens"][m]:
                        del w["tokens"][m]

    async def holdings(self, owner):
        if owner == next(iter(self.WALLETS)):  # one call per poll advances the sim
            self.step()
        w = self.w.get(owner, {"sol": 0.0, "tokens": {}})
        return {"sol": w["sol"], "tokens": dict(w["tokens"])}

    async def sol_balance(self, owner):
        return 1.5

    async def prices(self, mints, max_calls=1_000_000):
        self.priced = list(mints)
        return {m: {"price": self.px[m], "liquidity": self.TOKENS.get(m, ("", 0, 1e9))[2], "decimals": 9 if m == SOL_MINT else 6}
                for m in mints if m in self.px}

    async def token_info(self, mints):
        return {m: {"symbol": self.TOKENS[m][0], "name": ""} for m in mints if m in self.TOKENS}

    async def close(self):
        pass
