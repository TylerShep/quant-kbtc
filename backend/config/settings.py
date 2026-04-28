"""
Central configuration — loaded from environment variables.
Dataclass-based, per quant-developer skill.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_float(key: str, default: float = 0.0) -> float:
    return float(os.environ.get(key, str(default)))


def _env_int(key: str, default: int = 0) -> int:
    return int(os.environ.get(key, str(default)))


def _env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).lower() in ("true", "1", "yes")


@dataclass(frozen=True)
class KalshiConfig:
    api_key_id: str = field(default_factory=lambda: _env("KALSHI_API_KEY_ID"))
    private_key_path: str = field(
        default_factory=lambda: _env("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
    )
    env: str = field(default_factory=lambda: _env("KALSHI_ENV", "demo"))

    @property
    def base_url(self) -> str:
        if self.env == "prod":
            return "https://api.elections.kalshi.com/trade-api/v2"
        return "https://demo-api.kalshi.co/trade-api/v2"

    @property
    def ws_url(self) -> str:
        if self.env == "prod":
            return "wss://api.elections.kalshi.com/trade-api/ws/v2"
        return "wss://demo-api.kalshi.co/trade-api/ws/v2"


@dataclass(frozen=True)
class SpotConfig:
    coinbase_ws_url: str = field(
        default_factory=lambda: _env(
            "COINBASE_WS_URL", "wss://advanced-trade-ws.coinbase.com"
        )
    )
    binance_rest_url: str = field(
        default_factory=lambda: _env(
            "BINANCE_REST_URL", "https://api.binance.com/api/v3"
        )
    )


@dataclass(frozen=True)
class DatabaseConfig:
    url: str = field(
        default_factory=lambda: _env(
            "DATABASE_URL",
            "postgresql://kalshi:kalshi_secret@localhost:5432/kbtc",
        )
    )

    @property
    def async_url(self) -> str:
        """Convert postgresql:// to postgresql+asyncpg:// for async drivers."""
        if self.url.startswith("postgresql://"):
            return self.url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return self.url


@dataclass(frozen=True)
class OBIConfig:
    depth_levels: int = field(default_factory=lambda: _env_int("OBI_DEPTH_LEVELS", 10))
    long_threshold: float = field(default_factory=lambda: _env_float("OBI_LONG_THRESHOLD", 0.65))
    short_threshold: float = field(default_factory=lambda: _env_float("OBI_SHORT_THRESHOLD", 0.35))
    consecutive_readings: int = field(default_factory=lambda: _env_int("OBI_CONSECUTIVE_READINGS", 3))
    smooth_window_sec: float = field(default_factory=lambda: _env_float("OBI_SMOOTH_WINDOW_SEC", 5.0))
    smooth_min_samples: int = field(default_factory=lambda: _env_int("OBI_SMOOTH_MIN_SAMPLES", 3))
    refresh_seconds: int = field(default_factory=lambda: _env_int("OBI_REFRESH_SECONDS", 30))
    min_book_volume: int = field(default_factory=lambda: _env_int("MIN_BOOK_VOLUME", 1000))
    neutral_exit_long: float = field(default_factory=lambda: _env_float("OBI_NEUTRAL_EXIT_LONG", 0.55))
    neutral_exit_short: float = field(default_factory=lambda: _env_float("OBI_NEUTRAL_EXIT_SHORT", 0.45))
    max_candles_in_trade: int = field(default_factory=lambda: _env_int("OBI_MAX_CANDLES", 3))


@dataclass(frozen=True)
class ROCConfig:
    lookback: int = field(default_factory=lambda: _env_int("ROC_LOOKBACK", 3))
    long_threshold: float = field(default_factory=lambda: _env_float("ROC_LONG_THRESHOLD", 0.4))
    short_threshold: float = field(default_factory=lambda: _env_float("ROC_SHORT_THRESHOLD", -0.4))
    max_cap: float = field(default_factory=lambda: _env_float("ROC_MAX_CAP", 2.5))
    min_cap: float = field(default_factory=lambda: _env_float("ROC_MIN_CAP", -2.5))
    candle_confirm_min: int = field(default_factory=lambda: _env_int("ROC_CANDLE_CONFIRM_MIN", 2))
    momentum_stall_ratio: float = field(default_factory=lambda: _env_float("ROC_MOMENTUM_STALL_RATIO", 0.5))
    blowoff_single_candle: float = field(default_factory=lambda: _env_float("ROC_BLOWOFF_CANDLE", 1.5))
    max_candles_in_trade: int = field(default_factory=lambda: _env_int("ROC_MAX_CANDLES", 2))
    threshold_atr_mult: float = field(default_factory=lambda: _env_float("ROC_THRESHOLD_ATR_MULT", 1.2))
    threshold_floor: float = field(default_factory=lambda: _env_float("ROC_THRESHOLD_FLOOR", 0.10))
    threshold_cap: float = field(default_factory=lambda: _env_float("ROC_THRESHOLD_CAP", 1.0))


@dataclass(frozen=True)
class ATRConfig:
    period: int = field(default_factory=lambda: _env_int("ATR_PERIOD", 14))
    low_threshold: float = field(default_factory=lambda: _env_float("ATR_LOW_THRESHOLD", 0.15))
    high_threshold: float = field(default_factory=lambda: _env_float("ATR_HIGH_THRESHOLD", 0.50))
    smooth_period: int = field(default_factory=lambda: _env_int("ATR_SMOOTH_PERIOD", 3))
    regime_confirm_bars: int = field(default_factory=lambda: _env_int("ATR_REGIME_CONFIRM_BARS", 2))


@dataclass(frozen=True)
class SpreadDivConfig:
    enabled: bool = field(default_factory=lambda: _env_bool("SD_ENABLED", True))
    baseline_window: int = field(default_factory=lambda: _env_int("SD_BASELINE_WINDOW", 60))
    wide_threshold: float = field(default_factory=lambda: _env_float("SD_WIDE_THRESHOLD", 0.40))
    tight_threshold: float = field(default_factory=lambda: _env_float("SD_TIGHT_THRESHOLD", -0.20))
    min_history: int = field(default_factory=lambda: _env_int("SD_MIN_HISTORY", 20))
    staleness_sec: int = field(default_factory=lambda: _env_int("SD_STALENESS_SEC", 90))


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = field(default_factory=lambda: _env_float("RISK_PER_TRADE_PCT", 0.02))
    max_risk_per_trade_pct: float = field(default_factory=lambda: _env_float("MAX_RISK_PER_TRADE_PCT", 0.03))
    min_risk_per_trade_pct: float = field(default_factory=lambda: _env_float("MIN_RISK_PER_TRADE_PCT", 0.005))
    daily_loss_limit_pct: float = field(default_factory=lambda: _env_float("DAILY_LOSS_LIMIT_PCT", 0.06))
    weekly_loss_limit_pct: float = field(default_factory=lambda: _env_float("WEEKLY_LOSS_LIMIT_PCT", 0.15))
    max_drawdown_pct: float = field(default_factory=lambda: _env_float("MAX_DRAWDOWN_PCT", 0.20))
    stop_loss_pct: float = field(default_factory=lambda: _env_float("STOP_LOSS_PCT", 0.02))
    profit_target_mult: float = field(default_factory=lambda: _env_float("PROFIT_TARGET_MULT", 1.5))
    high_conviction_mult: float = field(default_factory=lambda: _env_float("HIGH_CONVICTION_MULT", 1.3))
    normal_conviction_mult: float = field(default_factory=lambda: _env_float("NORMAL_CONVICTION_MULT", 1.0))
    low_conviction_mult: float = field(default_factory=lambda: _env_float("LOW_CONVICTION_MULT", 0.65))
    drawdown_reduction_mult: float = field(default_factory=lambda: _env_float("DRAWDOWN_REDUCTION_MULT", 0.5))
    max_live_contracts: int = field(default_factory=lambda: _env_int("MAX_LIVE_CONTRACTS", 50))
    min_short_entry_price: float = field(default_factory=lambda: _env_float("MIN_SHORT_ENTRY_PRICE", 25.0))
    short_min_entry_price: float = field(default_factory=lambda: _env_float("SHORT_MIN_ENTRY_PRICE", 25.0))
    long_max_entry_price: float = field(default_factory=lambda: _env_float("LONG_MAX_ENTRY_PRICE", 60.0))
    short_size_mult: float = field(default_factory=lambda: _env_float("SHORT_SIZE_MULT", 0.7))
    short_settlement_guard_sec: int = field(default_factory=lambda: _env_int("SHORT_SETTLEMENT_GUARD_SEC", 300))
    # Short entries inside the contract's final ``short_min_seconds_to_expiry``
    # window are blocked at the price-guard layer. Paper-trading attribution on
    # 2026-04-07 -- 2026-04-28 (231 short trades) showed shorts entered with
    # >=13 min remaining were 59% WR / +$1,010 net, while shorts entered with
    # <13 min were 0-30% WR / -$6,619 net across 27 trades. The 5-min cohort
    # exited 13/14 via SHORT_SETTLEMENT_GUARD with 0% win rate. Block on entry
    # rather than panic-exit. See docs/runbooks/live-edge-filters.md.
    short_min_seconds_to_expiry: int = field(default_factory=lambda: _env_int("SHORT_MIN_SECONDS_TO_EXPIRY", 780))
    short_stop_loss_mult: float = field(default_factory=lambda: _env_float("SHORT_STOP_LOSS_MULT", 1.5))
    short_trend_lookback_candles: int = field(default_factory=lambda: _env_int("SHORT_TREND_LOOKBACK_CANDLES", 4))
    short_trend_soften_rise_pct: float = field(default_factory=lambda: _env_float("SHORT_TREND_SOFTEN_RISE_PCT", 0.20))
    short_trend_block_rise_pct: float = field(default_factory=lambda: _env_float("SHORT_TREND_BLOCK_RISE_PCT", 0.35))
    min_candles_before_early_exit: int = field(default_factory=lambda: _env_int("MIN_CANDLES_BEFORE_EARLY_EXIT", 2))


@dataclass(frozen=True)
class BotConfig:
    env: str = field(default_factory=lambda: _env("BOT_ENV", "development"))
    market: str = field(default_factory=lambda: _env("MARKET", "BTC"))
    trading_mode: str = field(default_factory=lambda: _env("TRADING_MODE", "paper"))
    initial_bankroll: float = field(default_factory=lambda: _env_float("INITIAL_BANKROLL", 1000.0))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    discord_trades_webhook: str = field(default_factory=lambda: _env("DISCORD_TRADES_WEBHOOK"))
    discord_risk_webhook: str = field(default_factory=lambda: _env("DISCORD_RISK_WEBHOOK"))
    discord_heartbeat_webhook: str = field(default_factory=lambda: _env("DISCORD_HEARTBEAT_WEBHOOK"))
    discord_errors_webhook: str = field(default_factory=lambda: _env("DISCORD_ERRORS_WEBHOOK"))
    discord_attribution_webhook: str = field(default_factory=lambda: _env("DISCORD_ATTRIBUTION_WEBHOOK"))
    discord_live_trades_webhook: str = field(default_factory=lambda: _env("DISCORD_LIVE_TRADES_WEBHOOK"))
    tuning_interval_hours: int = field(
        default_factory=lambda: _env_int("TUNING_INTERVAL_HOURS", 6)
    )
    cors_origins: str = field(
        default_factory=lambda: _env("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173")
    )
    dashboard_api_token: str = field(
        default_factory=lambda: _env("DASHBOARD_API_TOKEN", "")
    )
    roc_low_conviction_paper_enabled: bool = field(
        default_factory=lambda: _env_bool("ROC_LOW_CONVICTION_PAPER_ENABLED", False)
    )
    roc_low_conviction_live_enabled: bool = field(
        default_factory=lambda: _env_bool("ROC_LOW_CONVICTION_LIVE_ENABLED", False)
    )

    # BUG-028 expiry-race guard. Refuse to evaluate or place an entry when
    # the active contract is within this many seconds of close. Also treats
    # ``time_remaining_sec is None`` as too-close (the case that produced
    # every observed EXPIRY_409_SETTLED -- ticker rotated but no ``ticker``
    # WS event had populated ``state.expiry_time`` yet).
    min_seconds_to_expiry: int = field(
        default_factory=lambda: _env_int("MIN_SECONDS_TO_EXPIRY", 120)
    )
    # BUG-028 layer-2 backstop. Right before ``client.create_order`` in
    # ``position_manager.enter()``, fetch the market and abort if Kalshi
    # reports anything other than ``open``. Catches the residual race where
    # the coordinator's time guard passed (book was open, ticker was the
    # right one, time was >120s away) but Kalshi closed the book in the
    # ~10-50ms between decision and order placement. Default ON; can be
    # disabled via env if the extra REST hop is ever shown to add latency
    # that costs more than the EXPIRY_409 trades it prevents.
    expiry_market_status_check_enabled: bool = field(
        default_factory=lambda: _env_bool("EXPIRY_MARKET_STATUS_CHECK_ENABLED", True)
    )

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@dataclass(frozen=True)
class HistoricalSyncConfig:
    enabled: bool = field(
        default_factory=lambda: _env_bool("HISTORICAL_SYNC_ENABLED", True))
    predexon_api_key: str = field(
        default_factory=lambda: _env("PREDEXON_API_KEY", ""))
    predexon_base_url: str = field(
        default_factory=lambda: _env("PREDEXON_BASE_URL", "https://api.predexon.com/v2"))
    predexon_bootstrap_days: int = field(
        default_factory=lambda: _env_int("PREDEXON_BOOTSTRAP_DAYS", 90))
    predexon_interval_sec: int = field(
        default_factory=lambda: _env_int("PREDEXON_BOOTSTRAP_INTERVAL_SEC", 600))
    settlement_sync_days: int = field(
        default_factory=lambda: _env_int("KALSHI_SETTLEMENT_SYNC_DAYS", 90))
    settlement_interval_sec: int = field(
        default_factory=lambda: _env_int("KALSHI_SETTLEMENT_SYNC_INTERVAL_SEC", 3600))
    trades_sync_days: int = field(
        default_factory=lambda: _env_int("KALSHI_TRADES_SYNC_DAYS", 30))
    trades_interval_sec: int = field(
        default_factory=lambda: _env_int("KALSHI_TRADES_SYNC_INTERVAL_SEC", 300))
    tfi_window_minutes: int = field(
        default_factory=lambda: _env_int("TFI_WINDOW_MINUTES", 15))
    tfi_disagree_threshold: float = field(
        default_factory=lambda: _env_float("TFI_DISAGREE_THRESHOLD", 0.1))
    tfi_conviction_enabled: bool = field(
        default_factory=lambda: _env_bool("TFI_CONVICTION_ENABLED", True))


@dataclass(frozen=True)
class EdgeProfileConfig:
    """Live-lane-only filter profile derived from paper trading attribution.

    Empirical 7-day analysis (2026-04-13 → 2026-04-19, 245 clean trades)
    showed a statistically significant long-side edge (t=3.31), short-side
    anti-edge (t=-2.17), and that cheap entries (<$25) and OBI+ROC agreement
    setups carry the bulk of expectancy. This config restricts the LIVE
    lane to that validated subset while paper continues unfiltered for
    ongoing data collection and ML training.

    Default OFF — flip ``EDGE_LIVE_PROFILE_ENABLED=true`` to activate.
    Paper lane is never affected by this profile.
    """
    enabled: bool = field(default_factory=lambda: _env_bool("EDGE_LIVE_PROFILE_ENABLED", False))
    long_only: bool = field(default_factory=lambda: _env_bool("EDGE_LIVE_LONG_ONLY", True))
    block_low_conviction: bool = field(default_factory=lambda: _env_bool("EDGE_LIVE_BLOCK_LOW_CONVICTION", True))
    max_entry_price: float = field(default_factory=lambda: _env_float("EDGE_LIVE_MAX_ENTRY_PRICE", 25.0))
    agreement_overrides_price_cap: bool = field(
        default_factory=lambda: _env_bool("EDGE_LIVE_AGREEMENT_OVERRIDES_PRICE_CAP", True)
    )
    allowed_drivers: str = field(
        default_factory=lambda: _env(
            "EDGE_LIVE_ALLOWED_DRIVERS",
            # ROC/TIGHT removed 2026-04-21 after 9-day paper counterfactual showed
            # decay: 1d -$618, 3d -$284, 7d +$61, shorts 22% WR (9 trades).
            # Re-add only after a walk-forward backtest clears it.
            "OBI,OBI+ROC,ROC",
        )
    )
    blocked_hours_utc: str = field(
        default_factory=lambda: _env(
            "EDGE_LIVE_BLOCKED_HOURS_UTC",
            "0,1,2,3,4,5,6,7",
        )
    )

    @property
    def allowed_drivers_set(self) -> set[str]:
        """Parse comma-separated driver list into a set of base labels."""
        return {d.strip() for d in self.allowed_drivers.split(",") if d.strip()}

    @property
    def blocked_hours_set(self) -> set[int]:
        """Parse comma-separated hour list into a set of ints (0-23)."""
        out: set[int] = set()
        for h in self.blocked_hours_utc.split(","):
            h = h.strip()
            if not h:
                continue
            try:
                hi = int(h)
                if 0 <= hi <= 23:
                    out.add(hi)
            except ValueError:
                continue
        return out


@dataclass(frozen=True)
class LiveConfig:
    """Live-execution toggles separate from the always-on bot config.

    Currently a single feature flag for the BUG-025 fill-stream wiring.
    Defaults to ON so production picks it up immediately; can be flipped
    via env to revert to the legacy polled-order-response path with no
    redeploy.
    """
    use_fill_stream: bool = field(
        default_factory=lambda: _env_bool("LIVE_USE_FILL_STREAM", True)
    )


@dataclass(frozen=True)
class MLConfig:
    """Production ML inference gate. Stays dormant until a trained model
    artifact is dropped into backend/ml/models/ and ML_GATE_ENABLED=true.

    Design follows fail-open in inference.py: when no model file exists,
    `ml_gate()` returns (True, 1.0) and trades pass through unchanged.
    """
    gate_enabled: bool = field(default_factory=lambda: _env_bool("ML_GATE_ENABLED", False))
    gate_paper: bool = field(default_factory=lambda: _env_bool("ML_GATE_PAPER", True))
    gate_live: bool = field(default_factory=lambda: _env_bool("ML_GATE_LIVE", False))
    min_p_win: float = field(default_factory=lambda: _env_float("ML_MIN_P_WIN", 0.0))


@dataclass(frozen=True)
class Settings:
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    spot: SpotConfig = field(default_factory=SpotConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    obi: OBIConfig = field(default_factory=OBIConfig)
    roc: ROCConfig = field(default_factory=ROCConfig)
    atr: ATRConfig = field(default_factory=ATRConfig)
    spread_div: SpreadDivConfig = field(default_factory=SpreadDivConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    historical_sync: HistoricalSyncConfig = field(default_factory=HistoricalSyncConfig)
    ml: MLConfig = field(default_factory=MLConfig)
    edge_profile: EdgeProfileConfig = field(default_factory=EdgeProfileConfig)


settings = Settings()
