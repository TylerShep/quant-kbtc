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
            return "https://trading-api.kalshi.com/trade-api/v2"
        return "https://demo-api.kalshi.co/trade-api/v2"

    @property
    def ws_url(self) -> str:
        if self.env == "prod":
            return "wss://trading-api.kalshi.com/trade-api/ws/v2"
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
    consecutive_readings: int = field(default_factory=lambda: _env_int("OBI_CONSECUTIVE_READINGS", 2))
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


@dataclass(frozen=True)
class ATRConfig:
    period: int = field(default_factory=lambda: _env_int("ATR_PERIOD", 14))
    low_threshold: float = field(default_factory=lambda: _env_float("ATR_LOW_THRESHOLD", 0.15))
    high_threshold: float = field(default_factory=lambda: _env_float("ATR_HIGH_THRESHOLD", 0.50))
    smooth_period: int = field(default_factory=lambda: _env_int("ATR_SMOOTH_PERIOD", 3))
    regime_confirm_bars: int = field(default_factory=lambda: _env_int("ATR_REGIME_CONFIRM_BARS", 2))


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


@dataclass(frozen=True)
class BotConfig:
    env: str = field(default_factory=lambda: _env("BOT_ENV", "development"))
    market: str = field(default_factory=lambda: _env("MARKET", "BTC"))
    trading_mode: str = field(default_factory=lambda: _env("TRADING_MODE", "paper"))
    initial_bankroll: float = field(default_factory=lambda: _env_float("INITIAL_BANKROLL", 1000.0))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    alert_webhook_url: str = field(default_factory=lambda: _env("ALERT_WEBHOOK_URL"))
    cors_origins: str = field(
        default_factory=lambda: _env("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173")
    )

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@dataclass(frozen=True)
class Settings:
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    spot: SpotConfig = field(default_factory=SpotConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    obi: OBIConfig = field(default_factory=OBIConfig)
    roc: ROCConfig = field(default_factory=ROCConfig)
    atr: ATRConfig = field(default_factory=ATRConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    bot: BotConfig = field(default_factory=BotConfig)


settings = Settings()
