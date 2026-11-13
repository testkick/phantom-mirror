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

    def public(self) -> dict:
        d = dict(self.__dict__)
        d.pop("admin_token", None)
        d["admin_required"] = bool(self.admin_token)
        return d
