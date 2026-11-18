"""Dry run: what would live execution of the spot copy strategy actually have done?

For every new spot_copy paper fill, ask Jupiter for a real swap quote at the size a live
account would trade, apply the live risk limits, and record the outcome. Nothing is signed
or sent, and no private key is involved; DRYRUN_SOL_WALLET is a public address used only
to check the SOL balance.
"""
import time

import httpx

from .sol import SOL_MINT

LAMPORTS = 1e9


class DryRun:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        self.http = httpx.AsyncClient(timeout=20, headers={"x-api-key": cfg.jup_api_key} if cfg.jup_api_key else {})
        row = db.one("SELECT COALESCE(MAX(id),0) m FROM paper_fills")
        self.last_fill_id = row["m"]  # don't back-fill history on restart

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int) -> dict:
        r = await self.http.get(f"{self.cfg.jup_api_base.rstrip('/')}/swap/v1/quote", params={
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw),
            "slippageBps": int(self.cfg.live_max_slippage_bps)})
        r.raise_for_status()
        return r.json()

    def _daily_net(self) -> float:
        """Today's (UTC) realized spot_copy result net of fees, as the stand-in for live P&L."""
        start = time.time() - (time.time() % 86400)
        row = self.db.one("SELECT COALESCE(SUM(realized_pnl)-SUM(fee),0) n FROM paper_fills "
                          "WHERE strategy='spot_copy' AND ts>=?", (start,))
        return row["n"]

    async def run(self, spot_px: dict, sol_balance: float | None, now: float) -> list[dict]:
        cfg = self.cfg
        fills = self.db.q("SELECT * FROM paper_fills WHERE strategy='spot_copy' AND id>? ORDER BY id", (self.last_fill_id,))
        if not fills:
            return []
        self.last_fill_id = fills[-1]["id"]
        sol_px = (spot_px.get(SOL_MINT) or {}).get("price")
        out = []
        for f in fills:
            mint, buy = f["coin"], f["delta"] > 0
            info = spot_px.get(mint) or {}
            mid, dec = info.get("price"), info.get("decimals")
            paper_usd = abs(f["delta"]) * f["px"]
            usd = min(paper_usd, cfg.live_max_trade_usd)
            rec = {"fill_id": f["id"], "ts": now, "source": f["source"], "mint": mint, "side": "buy" if buy else "sell",
                   "usd": usd, "paper_px": f["px"], "exec_px": None, "impact_bps": None, "status": "ok", "detail": ""}
            if usd < paper_usd:
                rec["detail"] = f"capped from ${paper_usd:,.0f}"
            try:
                if not (mid and sol_px and dec is not None):
                    raise RuntimeError("no price/decimals for quote")
                if buy and self._daily_net() <= -cfg.live_daily_loss_usd:
                    rec["status"] = "blocked"
                    rec["detail"] = f"daily loss cap ${cfg.live_daily_loss_usd:,.0f} hit"
                elif buy and sol_balance is not None and usd / sol_px > sol_balance - cfg.live_sol_reserve:
                    rec["status"] = "blocked"
                    rec["detail"] = f"insufficient SOL ({sol_balance:.3f} in wallet)"
                else:
                    qty = usd / mid
                    if buy:
                        q = await self.quote(SOL_MINT, mint, int(usd / sol_px * LAMPORTS))
                        got = int(q["outAmount"]) / 10 ** dec
                        exec_px = usd / got
                        adverse = (exec_px / mid - 1) * 1e4
                    else:
                        q = await self.quote(mint, SOL_MINT, int(qty * 10 ** dec))
                        exec_px = int(q["outAmount"]) / LAMPORTS * sol_px / qty
                        adverse = (1 - exec_px / mid) * 1e4
                    rec["exec_px"], rec["impact_bps"] = exec_px, adverse
                    if adverse > cfg.live_max_slippage_bps:
                        rec["status"] = "blocked"
                        rec["detail"] = (rec["detail"] + "; " if rec["detail"] else "") + \
                            f"{adverse:.0f} bps worse than mid (limit {cfg.live_max_slippage_bps:.0f})"
            except Exception as e:
                rec["status"] = "error"
                rec["detail"] = f"{type(e).__name__}: {e}"[:200]
            self.db.x("INSERT INTO dryrun(fill_id,ts,source,mint,side,usd,paper_px,exec_px,impact_bps,status,detail) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(rec.values()))
            out.append(rec)
        return out

    async def close(self):
        await self.http.aclose()


class DemoDryRun(DryRun):
    """Offline quotes: price impact grows with size relative to liquidity."""

    def __init__(self, db, cfg, spot_px_ref: dict):
        super().__init__(db, cfg)
        self.px = spot_px_ref

    async def quote(self, input_mint, output_mint, amount_raw):
        buy = input_mint == SOL_MINT
        mint = output_mint if buy else input_mint
        p, sol = self.px[mint], self.px[SOL_MINT]
        dec = p["decimals"]
        usd = amount_raw / LAMPORTS * sol["price"] if buy else amount_raw / 10 ** dec * p["price"]
        impact = min(0.5, usd / max(p["liquidity"], 1) * 20)
        if buy:
            return {"outAmount": int(usd * (1 - impact) / p["price"] * 10 ** dec)}
        return {"outAmount": int(usd * (1 - impact) / sol["price"] * LAMPORTS)}
