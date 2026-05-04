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
    pool_min_size: int = field(default_factory=lambda: _env_int("DB_POOL_MIN_SIZE", 5))
    pool_max_size: int = field(default_factory=lambda: _env_int("DB_POOL_MAX_SIZE", 20))
    pool_timeout_sec: float = field(default_factory=lambda: _env_float("DB_POOL_TIMEOUT_SEC", 10.0))
    pool_max_idle_sec: float = field(default_factory=lambda: _env_float("DB_POOL_MAX_IDLE_SEC", 600.0))
    pool_max_lifetime_sec: float = field(default_factory=lambda: _env_float("DB_POOL_MAX_LIFETIME_SEC", 3600.0))
    write_gate_size: int = field(default_factory=lambda: _env_int("DB_WRITE_GATE_SIZE", 8))

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
    # Supervised live-trade cap. After this many completed live round-trips,
    # PositionManager.can_enter returns False until the operator calls the
    # reset endpoint (or the counter is bumped down manually). 0 = unlimited.
    # Default 5 keeps live trading on a short leash while we are still
    # validating the post-OOM stability and the market-status fix.
    live_trade_limit: int = field(
        default_factory=lambda: _env_int("LIVE_TRADE_LIMIT", 5)
    )
    # Supervised auto-pause: when True, the coordinator flips
    # ``trading_paused = "paused"`` after every live exit so the operator
    # must manually unpause via /api/trading-pause before the next live
    # entry can fire. When False, live trading runs continuously and the
    # operator pauses manually via the dashboard. Default False — the
    # ``live_trade_limit`` cap is the primary safety rail; an additional
    # per-trade pause is only useful during very early validation.
    supervised_auto_pause: bool = field(
        default_factory=lambda: _env_bool("SUPERVISED_AUTO_PAUSE", False)
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
    # BUG-032 (2026-05-04): the EXPIRY_GUARD exit was triggered at T-60s,
    # but a single Kalshi exit-order round-trip during the pre-close
    # volatility window can take 18+ seconds (15s FILL_POLL_TIMEOUT +
    # 1.5s ledger lag + jitter). The coordinator retries up to 3 times
    # with 2s + 4s backoff, so the *worst-case* exit takes ~70 seconds —
    # which guarantees we cross the contract close. Three orphans on
    # 2026-05-04 (B79950, B80350, B79150) followed exactly this pattern:
    # exit fired at T-50s, first attempt sat in flight for 22s, retry,
    # 409 Conflict from Kalshi (contract already closed), orphan adopted.
    # Move the EXPIRY_GUARD trigger to T-180s so the worst-case retry
    # sequence still completes before the close, and so a single failed
    # round-trip doesn't burn the entire window.
    expiry_guard_trigger_sec: int = field(
        default_factory=lambda: _env_int("EXPIRY_GUARD_TRIGGER_SEC", 180)
    )
    # BUG-032 layer-2: the inner FILL_POLL_TIMEOUT for an EXPIRY_GUARD
    # exit. The default 15s polling window is the right balance for
    # normal exits but is way too long when the contract is about to
    # close. Override it for EXPIRY_GUARD/SHORT_SETTLEMENT_GUARD only.
    expiry_guard_fill_poll_timeout_sec: float = field(
        default_factory=lambda: _env_float("EXPIRY_GUARD_FILL_POLL_TIMEOUT_SEC", 5.0)
    )

    # ── Phase 2 (Expiry Exit Reliability, 2026-05-04): live retry widening
    # for EXPIRY_GUARD / SHORT_SETTLEMENT_GUARD only. The existing live
    # exit path sends market sells with ``yes_price=1`` (or ``no_price=1``)
    # which means "I'll accept any price ≥ 1 cent" -- maximally aggressive
    # but also maximally exposed to a thin pre-close book. The widening
    # config lets attempt 0 try a tighter floor (closer to mid) and step
    # down toward the existing max-aggressive behavior on the final attempt.
    #
    # Defaults preserve the legacy behavior exactly: attempt 0 already
    # uses the 1-cent floor so retries never change order prices unless
    # the operator opts in. To enable: set the first-attempt floor below
    # 99 (long) / above 1 (short) and pick a positive widen step.
    #
    # Mechanics for a long exit (sell YES) at attempt N:
    #   floor_cents = max(1, first_attempt_yes_floor_cents
    #                        - widen_step_cents * N)
    # On the FINAL attempt we always force the 1-cent floor regardless
    # of computed value when ``final_attempt_max_aggressive`` is True.
    # Short exits mirror with NO floors.
    expiry_retry_max_attempts: int = field(
        default_factory=lambda: _env_int("EXPIRY_RETRY_MAX_ATTEMPTS", 3)
    )
    expiry_retry_backoff_base_sec: float = field(
        default_factory=lambda: _env_float("EXPIRY_RETRY_BACKOFF_BASE_SEC", 2.0)
    )
    expiry_retry_max_backoff_sec: float = field(
        default_factory=lambda: _env_float("EXPIRY_RETRY_MAX_BACKOFF_SEC", 8.0)
    )
    # Per-attempt floor for the YES leg of a long exit (cents). Default 1
    # = current behavior (no widening). Operators wanting to harvest
    # better fills on attempt 0 set this to e.g. 30 and the bot will
    # only fill if the book has a YES bid at ≥30c on the first try.
    expiry_retry_first_attempt_yes_floor_cents: int = field(
        default_factory=lambda: _env_int(
            "EXPIRY_RETRY_FIRST_ATTEMPT_YES_FLOOR_CENTS", 1)
    )
    # Per-attempt floor for the NO leg of a short exit (cents). Default 1
    # = current behavior. Mirror of the YES floor above.
    expiry_retry_first_attempt_no_floor_cents: int = field(
        default_factory=lambda: _env_int(
            "EXPIRY_RETRY_FIRST_ATTEMPT_NO_FLOOR_CENTS", 1)
    )
    # Cents to widen (lower for long, raise for short) per retry attempt.
    # 0 = no widening, all attempts use the same floor.
    expiry_retry_widen_step_cents: int = field(
        default_factory=lambda: _env_int("EXPIRY_RETRY_WIDEN_STEP_CENTS", 0)
    )
    # When True, the FINAL attempt always uses the 1-cent floor (max
    # aggressive) regardless of the computed widen schedule. This is the
    # explicit safety rail required by BUG-032 -- we MUST be able to fall
    # through to current behavior if widening fails to harvest.
    expiry_retry_final_attempt_max_aggressive: bool = field(
        default_factory=lambda: _env_bool(
            "EXPIRY_RETRY_FINAL_ATTEMPT_MAX_AGGRESSIVE", True)
    )

    # ── Phase 3 (Expiry Exit Reliability, 2026-05-04): pre-expiry passive
    # limit ladder. OPT-IN, default OFF. When enabled, just before the
    # EXPIRY_GUARD trigger fires the coordinator places a passive limit
    # exit order at a tighter price than the EXPIRY_GUARD aggressive
    # exit, polls for fills briefly, cancels and steps to a more
    # aggressive rung if no fill, and unconditionally falls back to the
    # standard EXPIRY_GUARD path when the time budget runs out.
    #
    # The ladder is a quality-of-fill optimization. It MUST NEVER extend
    # the orphan window: ``ladder_total_budget_sec`` is bounded by
    # ``expiry_guard_trigger_sec`` and the fallback path runs after the
    # budget regardless of fill state.
    #
    # Rollout discipline: enable in paper first (LADDER_ENABLED_PAPER),
    # validate diagnostics for at least one full week, then enable the
    # live flag (LADDER_ENABLED_LIVE) under a tight live_trade_limit.
    ladder_enabled_paper: bool = field(
        default_factory=lambda: _env_bool("LADDER_ENABLED_PAPER", False)
    )
    ladder_enabled_live: bool = field(
        default_factory=lambda: _env_bool("LADDER_ENABLED_LIVE", False)
    )
    # When the contract has this many seconds to expiry, the ladder
    # is allowed to start. Must be greater than expiry_guard_trigger_sec
    # by at least ``ladder_total_budget_sec`` so the ladder runs
    # completely before EXPIRY_GUARD fires. Default 240s = 60s of
    # headroom over the 180s EXPIRY_GUARD trigger.
    ladder_start_trigger_sec: int = field(
        default_factory=lambda: _env_int("LADDER_START_TRIGGER_SEC", 240)
    )
    # Total time budget across all rungs. After this many seconds, the
    # ladder ALWAYS yields to the EXPIRY_GUARD path even if no fills
    # occurred. Bounded above to prevent the ladder from racing the close.
    ladder_total_budget_sec: int = field(
        default_factory=lambda: _env_int("LADDER_TOTAL_BUDGET_SEC", 50)
    )
    # Number of progressively-aggressive rungs. Each rung is a passive
    # limit order placed N cents off the current best ask (long) /
    # best bid (short). Rung 0 is the tightest (most operator-favorable),
    # subsequent rungs widen by ``ladder_rung_step_cents``.
    ladder_rung_count: int = field(
        default_factory=lambda: _env_int("LADDER_RUNG_COUNT", 3)
    )
    # Initial rung offset off the executable side, in cents. For a long
    # exit we sell YES; rung 0 places yes_price = best_yes_bid +
    # rung_first_offset_cents (i.e. inside the spread, asking for a
    # higher price). For a short exit it mirrors with the NO side.
    ladder_rung_first_offset_cents: int = field(
        default_factory=lambda: _env_int("LADDER_RUNG_FIRST_OFFSET_CENTS", 5)
    )
    # Cents to widen per rung. After ladder_rung_count rungs the ladder
    # is at offset = first_offset + step * (count - 1) cents wide.
    ladder_rung_step_cents: int = field(
        default_factory=lambda: _env_int("LADDER_RUNG_STEP_CENTS", 3)
    )
    # Per-rung poll/cancel timeout. After this many seconds with no fill,
    # cancel the rung and step to the next.
    ladder_rung_timeout_sec: float = field(
        default_factory=lambda: _env_float("LADDER_RUNG_TIMEOUT_SEC", 8.0)
    )
    # If True, on bot restart while a ladder order is in flight the
    # PositionManager attempts to cancel any resting ladder-tagged
    # orders and falls back to the regular guard path. False = trust
    # the existing reconciliation logic to surface stale orders. Only
    # set to True when the operator has verified the cancel-on-restart
    # path against demo. Default False = fail-closed to standard
    # reconciliation flow.
    ladder_cancel_on_restart: bool = field(
        default_factory=lambda: _env_bool("LADDER_CANCEL_ON_RESTART", False)
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

    Re-calibrated 2026-04-29 against 14-day paper window (2026-04-15 →
    2026-04-29, 522 clean trades). Findings that the new defaults encode:

      * Long side: 309 trades, +$114,197 net, 53% WR. Edge is robust across
        every UTC hour. The original 0-7 UTC block (calibrated on the
        2026-04-13 → 04-19 window) was costing +$36,313 of long PnL —
        hours 03/05/06 alone produced +$28,513. Block list cleared.
      * Short side: 213 trades, -$6,021 net overall, BUT cleanly bimodal:
          - Cheap (<$40) NORMAL conviction: 89 trades @ $30 bucket lost
            -$12,893 (37% WR). This is the entire short anti-edge.
          - HIGH conviction shorts: 12 trades, +$1,799 net, 75% WR.
          - Shorts at $50+: 56 trades, +$6,400 net, 70%+ WR.
        Replace ``EDGE_LIVE_LONG_ONLY=true`` blanket block with two
        targeted gates: ``short_min_price`` and ``short_min_conviction``.
      * Cheap entries (<$25) carry positive long expectancy still; the
        ``max_entry_price`` cap stays in place.
      * OBI+ROC agreement: 92.3% WR for longs, 72.7% WR for HIGH shorts.
        Stays exempt from the price cap.

    Paper lane is NEVER affected by this profile — it continues to take
    the full strategy for ongoing data collection and ML training.

    Default OFF — flip ``EDGE_LIVE_PROFILE_ENABLED=true`` to activate.
    """
    enabled: bool = field(default_factory=lambda: _env_bool("EDGE_LIVE_PROFILE_ENABLED", False))
    # Hard kill switch for shorts. Default False after 2026-04-29 re-calibration
    # that showed HIGH-conviction shorts and $50+ shorts have positive
    # expectancy. Set to True to revert to the pre-2026-04-29 behavior of
    # blocking every short.
    long_only: bool = field(default_factory=lambda: _env_bool("EDGE_LIVE_LONG_ONLY", False))
    # Targeted short-side gates (only consulted when long_only=False).
    # A short trade is allowed if entry_price >= short_min_price OR
    # conviction >= short_min_conviction. Either gate alone is sufficient.
    short_min_price: float = field(default_factory=lambda: _env_float("EDGE_LIVE_SHORT_MIN_PRICE", 40.0))
    short_min_conviction: str = field(default_factory=lambda: _env("EDGE_LIVE_SHORT_MIN_CONVICTION", "HIGH"))
    # ROC-contradiction veto for NORMAL-conviction shorts.
    # 14-day paper attribution (2026-04-19 → 2026-05-02, 189 NORMAL shorts post-ML)
    # showed a single clean disaster cohort: shorts with raw 5-bar ROC <= -0.05
    # (price already falling sharply) lost -$120/trade across 103 trades at 40%
    # WR. Every other ROC bucket was breakeven-to-profitable. Pattern is mean
    # reversion: short OBI on top of an extended down-move gets steamrolled by
    # the snap-back. Counterfactual application would have lifted 14-day paper
    # short PnL from -$10,613 to +$244. Long-side mirror cohort is profitable
    # in every roc_5 bucket so the gate is short-only.
    #
    # Filter is short-only and gated on conviction == NORMAL (HIGH-conviction
    # shorts override). Set to 0.0 (or any non-negative value) to disable.
    short_block_negative_roc_threshold: float = field(
        default_factory=lambda: _env_float(
            "EDGE_LIVE_SHORT_BLOCK_NEGATIVE_ROC_THRESHOLD", -0.05))
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
    # Hour list cleared 2026-04-29: 14-day window showed hours 03/05/06
    # were among the strongest long-PnL hours of the day. See class docstring.
    blocked_hours_utc: str = field(
        default_factory=lambda: _env("EDGE_LIVE_BLOCKED_HOURS_UTC", "")
    )
    # Phase 2.5 master kill switch. Default OFF; the operator opts in
    # after observing 3+ weekly review cycles and confirming the
    # AUTO_APPLY recommendations match what they would have done by
    # hand. The bot itself does NOT read this — scripts/edge_profile_apply.py
    # reads the env file directly. Defining it here ensures it shows up
    # in /api/diagnostics' edge_profile_health block so the dashboard
    # can render the kill-switch state.
    auto_apply_enabled: bool = field(
        default_factory=lambda: _env_bool("EDGE_LIVE_AUTO_APPLY_ENABLED", False)
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
