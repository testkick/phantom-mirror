"""All tunables live here and are read from environment variables."""
import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # --- infrastructure ---
    data_dir: str = field(default_factory=lambda: os.getenv("DATA_DIR", "./data"))
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", ""))
    demo_mode: bool = field(default_factory=lambda: _b("DEMO_MODE", False))
    hl_info_url: str = field(default_factory=lambda: os.getenv("HL_INFO_URL", "https://api.hyperliquid.xyz/info"))
    hl_leaderboard_url: str = field(
        default_factory=lambda: os.getenv("HL_LEADERBOARD_URL", "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
    )
    # "" is the main Hyperliquid perp dex. Add HIP-3 dex names (comma separated) to
    # also track stock/commodity perps, e.g. HL_DEXS=",xyz"
    hl_dexs: list = field(default_factory=lambda: os.getenv("HL_DEXS", "").split(","))
    poll_seconds: float = field(default_factory=lambda: _f("POLL_SECONDS", 20))
    equity_snapshot_seconds: float = field(default_factory=lambda: _f("EQUITY_SNAPSHOT_SECONDS", 60))

    # --- paper execution model ---
    starting_equity: float = field(default_factory=lambda: _f("STARTING_EQUITY", 10_000))
    # Hyperliquid taker is ~4.5 bps; trading through Phantom adds 5 bps (9.5 total).
    fee_bps: float = field(default_factory=lambda: _f("FEE_BPS", 9.5))
    slippage_bps: float = field(default_factory=lambda: _f("SLIPPAGE_BPS", 2.0))
    min_order_usd: float = field(default_factory=lambda: _f("MIN_ORDER_USD", 10))

    # --- strategy A: 1:1 copy ---
    copy_alloc_usd: float = field(default_factory=lambda: _f("COPY_ALLOC_USD", 1_000))  # equity assigned per trader
    copy_max_leverage: float = field(default_factory=lambda: _f("COPY_MAX_LEVERAGE", 5))  # cap per mirrored position
    copy_rebalance_threshold: float = field(default_factory=lambda: _f("COPY_REBALANCE_THRESHOLD", 0.10))
    copy_existing: bool = field(default_factory=lambda: _b("COPY_EXISTING", False))  # mirror positions open before tracking began
    min_account_value: float = field(default_factory=lambda: _f("MIN_ACCOUNT_VALUE", 1_000))

    # --- strategy B: consensus ---
    consensus_min_traders: int = field(default_factory=lambda: _i("CONSENSUS_MIN_TRADERS", 3))
    consensus_agreement: float = field(default_factory=lambda: _f("CONSENSUS_AGREEMENT", 0.70))
    consensus_exit_min_traders: int = field(default_factory=lambda: _i("CONSENSUS_EXIT_MIN_TRADERS", 2))
    consensus_exit_agreement: float = field(default_factory=lambda: _f("CONSENSUS_EXIT_AGREEMENT", 0.50))
    consensus_notional_usd: float = field(default_factory=lambda: _f("CONSENSUS_NOTIONAL_USD", 1_000))
    consensus_max_positions: int = field(default_factory=lambda: _i("CONSENSUS_MAX_POSITIONS", 10))
    consensus_include_existing: bool = field(default_factory=lambda: _b("CONSENSUS_INCLUDE_EXISTING", True))

    # --- strategy C: Solana spot copy ---
    solana_rpc_url: str = field(default_factory=lambda: os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"))
    jup_api_base: str = field(default_factory=lambda: os.getenv("JUP_API_BASE", "https://lite-api.jup.ag"))
    jup_api_key: str = field(default_factory=lambda: os.getenv("JUP_API_KEY", ""))
    spot_alloc_usd: float = field(default_factory=lambda: _f("SPOT_ALLOC_USD", 1_000))  # paper equity per spot trader
    spot_fee_bps: float = field(default_factory=lambda: _f("SPOT_FEE_BPS", 85))  # approximates an in-wallet swap fee; adjust
    spot_slippage_bps: float = field(default_factory=lambda: _f("SPOT_SLIPPAGE_BPS", 100))
    spot_max_position_pct: float = field(default_factory=lambda: _f("SPOT_MAX_POSITION_PCT", 0.25))  # of SPOT_ALLOC_USD
    spot_min_liquidity_usd: float = field(default_factory=lambda: _f("SPOT_MIN_LIQUIDITY_USD", 50_000))  # entry filter
    spot_price_refresh_seconds: float = field(default_factory=lambda: _f("SPOT_PRICE_REFRESH_SECONDS", 300))

    # --- trader suggestions ---
    solanatracker_api_key: str = field(default_factory=lambda: os.getenv("SOLANATRACKER_API_KEY", ""))
    solanatracker_base: str = field(default_factory=lambda: os.getenv("SOLANATRACKER_BASE", "https://data.solanatracker.io"))
    discover_limit: int = field(default_factory=lambda: _i("DISCOVER_LIMIT", 15))
    discover_min_account_usd: float = field(default_factory=lambda: _f("DISCOVER_MIN_ACCOUNT_USD", 25_000))
    # spot copyability filters: drop HFT bots, launch snipers and likely insiders
    discover_spot_max_trades_per_day: float = field(default_factory=lambda: _f("DISCOVER_SPOT_MAX_TRADES_PER_DAY", 300))
    discover_spot_max_win_rate: float = field(default_factory=lambda: _f("DISCOVER_SPOT_MAX_WIN_RATE", 95))
    discover_spot_max_roi: float = field(default_factory=lambda: _f("DISCOVER_SPOT_MAX_ROI", 50))  # 50 = 5,000%
    discover_cache_seconds: float = field(default_factory=lambda: _f("DISCOVER_CACHE_SECONDS", 1800))

    # --- dry run: live-execution check for spot copy (nothing is signed or sent) ---
    dryrun_enabled: bool = field(default_factory=lambda: _b("DRYRUN_ENABLED", True))
    dryrun_sol_wallet: str = field(default_factory=lambda: os.getenv("DRYRUN_SOL_WALLET", ""))  # PUBLIC address only
    live_max_trade_usd: float = field(default_factory=lambda: _f("LIVE_MAX_TRADE_USD", 100))
    live_max_slippage_bps: float = field(default_factory=lambda: _f("LIVE_MAX_SLIPPAGE_BPS", 300))
    live_daily_loss_usd: float = field(default_factory=lambda: _f("LIVE_DAILY_LOSS_USD", 100))
    live_sol_reserve: float = field(default_factory=lambda: _f("LIVE_SOL_RESERVE", 0.05))  # keep for fees

    def public(self) -> dict:
        d = dict(self.__dict__)
        d.pop("admin_token", None)
        d.pop("jup_api_key", None)
        d.pop("solanatracker_api_key", None)
        d["spot_suggestions_enabled"] = bool(self.solanatracker_api_key) or self.demo_mode
        d.pop("solana_rpc_url", None)  # may embed an API key
        d["admin_required"] = bool(self.admin_token)
        return d
