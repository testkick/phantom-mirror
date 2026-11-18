# PhantomMirror

Monitors Phantom top traders and paper-mirrors them with three strategies side by side:
perps 1:1 copy, perps consensus, and Solana spot copy.

Phantom perps run on Hyperliquid, and each Phantom account trades there under its 0x (Ethereum-format)
address. Hyperliquid's info API is public, so once you have a trader's 0x address you can read their
positions without any key.

## How it works

Every `POLL_SECONDS` (default 20):

1. Fetch mid prices (`allMids`) and each tracked trader's positions (`clearinghouseState`).
2. Diff against the last snapshot and log events: open, increase, reduce, close, flip.
3. **1:1 copy**: each trader gets `COPY_ALLOC_USD` of paper equity. Their positions are scaled by
   `alloc / their account value`, so you run the same leverage they do, capped at `COPY_MAX_LEVERAGE`.
   Size changes under `COPY_REBALANCE_THRESHOLD` are ignored to avoid fee churn.
4. **Consensus**: one vote per trader per coin. Enter `CONSENSUS_NOTIONAL_USD` when at least
   `CONSENSUS_MIN_TRADERS` hold the same side and they are at least `CONSENSUS_AGREEMENT` of all holders.
   Exit when support falls below the exit thresholds or the signal reverses.
5. Snapshot equity for both strategies.

Positions a trader already held when you started tracking them are marked `pre` and are not copied
(their entry price is stale). They do count toward consensus unless `CONSENSUS_INCLUDE_EXISTING=false`.

Fills are simulated at mid plus/minus `SLIPPAGE_BPS`, with `FEE_BPS` charged per side.

## Spot traders (Solana)

Paste a Solana wallet address instead of a 0x address and the trader is tracked as spot.
Every poll reads the wallet's SOL balance and token accounts (SPL and Token-2022) from
`SOLANA_RPC_URL`, prices them with Jupiter Price V3, and diffs against the last snapshot:
`buy`, `buy more`, `sell part`, `sell all`. SOL, USDC and USDT count as cash and are never mirrored.

**Spot copy** gives each spot trader `SPOT_ALLOC_USD` and holds each token at the same share
of portfolio value the trader does, capped at `SPOT_MAX_POSITION_PCT`. New entries are skipped
when Jupiter reports liquidity below `SPOT_MIN_LIQUIDITY_USD`. Bags held before tracking started
are tagged `pre` and not copied, same as perps.

Limits: a buy and sell inside one poll interval is invisible; prices are Jupiter mid prices with a
flat slippage assumption, which flatters thin memecoins; the public Solana RPC rate-limits hard, so
set `SOLANA_RPC_URL` to a provider endpoint for more than a couple of wallets.

## Suggested traders

The dashboard lists traders to follow with one click. Perps come from Hyperliquid's leaderboard
(day / week / month / all time, accounts under `DISCOVER_MIN_ACCOUNT_USD` hidden). Spot comes from
Solana Tracker's PnL leaderboard (needs `SOLANATRACKER_API_KEY`) with arbitrage bots excluded,
at least 20 trades, and no single token over 80% of profit. Neither is Phantom's own Top Traders
list, and neither knows Phantom usernames.

## Dry run

Every spot copy fill is re-priced with a real Jupiter quote at the size a live account would trade
(`LIVE_MAX_TRADE_USD`), then checked against the live limits: maximum cost vs mid, a daily loss cap
that stops new buys, and (if `DRYRUN_SOL_WALLET` is set to a public address) the wallet's SOL balance.
The result shows what live execution would have done. Nothing is signed or sent and no key is used.

## Run locally

```bash
pip install -r requirements-dev.txt
pytest                                   # 9 tests
DEMO_MODE=1 POLL_SECONDS=1 uvicorn app.main:app --port 8000   # simulated data, no network
uvicorn app.main:app --port 8000         # live Hyperliquid data
```

Open http://localhost:8000.

## Deploy to Railway

1. Push this folder to a repo and create a Railway service from it (Dockerfile build, `railway.json` included).
2. Add a volume mounted at `/data`.
3. Set `ADMIN_TOKEN` (see `.env.example` for every setting).
4. Keep it at one replica and one worker: the poller runs inside the web process.

## Building the roster

- **Manual (the Phantom list):** in Phantom, open Perps > Top Traders, open a trader, copy their 0x
  address, paste it into the dashboard. Phantom has no public API for this list, so this step is by hand.
- **Seed HL top 10:** pulls the Hyperliquid-wide weekly leaderboard. It is a proxy, not Phantom's list,
  and the endpoint is undocumented and returns a large file.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/summary` | both strategies' equity, P&L, fees, win rate, config |
| GET | `/api/traders` | roster with live positions and per-trader copy P&L |
| GET | `/api/positions` `/api/consensus` `/api/events` `/api/fills` `/api/equity` | |
| POST | `/api/traders` `{address, label}` | admin |
| DELETE | `/api/traders/{address}` | admin; copy positions close on next poll |
| POST | `/api/traders/seed` `{window, top, min_account_value}` | admin |
| POST | `/api/reset` | admin; wipes paper history, keeps roster |
| GET | `/healthz` | unhealthy if polling has stalled |

Admin calls need the `x-admin-token` header when `ADMIN_TOKEN` is set.

## Known limits

- **Polling latency.** You see a change up to `POLL_SECONDS` after it happens and fill at the then-current
  mid. Paper results are therefore somewhat pessimistic versus a websocket feed, and optimistic versus
  real order-book impact on thin coins.
- **Not modelled:** funding payments, liquidations, partial fills, order-book depth.
- **Copy win rate** counts every reducing fill, so a trader who scales out in five steps counts five times.
- **HIP-3 markets** (stocks, commodities) need `HL_DEXS` set; account value is summed across dexs, which
  may double count for unified accounts. Treat as experimental.
- **Rate limits:** each poll makes one request per trader plus one for prices. A few dozen traders at 20s
  is well inside Hyperliquid's public limits; for hundreds, raise `POLL_SECONDS`.

## Layout

```
app/config.py   env-driven settings
app/db.py       SQLite schema + wrapper
app/hl.py       Hyperliquid client + offline simulator
app/engine.py   event diffing, both strategies, paper accounting
app/main.py     FastAPI app, poller, API
app/static/     dashboard
tests/          engine + end-to-end tests
```
