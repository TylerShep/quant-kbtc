"""
Capture a feature snapshot at trade entry for ML training data.
Stage 1: passive data collection only -- no model inference.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import structlog

from strategies.roc import calculate_roc

logger = structlog.get_logger()


def extract_features(
    *,
    features,
    candle_aggregator,
    atr_filter,
    state,
    roc_lookback: int = 3,
    historical_sync=None,
) -> dict:
    """Build a feature dict from current market state.

    All inputs come from objects already available in the coordinator at
    entry time. Returns a flat dict ready for DB insertion.
    """
    # `calculate_roc(closes, lookback)` requires `len(closes) >= lookback + 1`
    # (it takes a return between `closes[-1]` and `closes[-(lookback+1)]`).
    # We need to request at least 11 candles so the longest lookback (10)
    # can populate; otherwise `roc_10` is silently `None` on every entry.
    recent_candles = candle_aggregator.recent(11)
    closes = [c.close for c in recent_candles]

    roc_3 = calculate_roc(closes, 3)
    roc_5 = calculate_roc(closes, 5)
    roc_10 = calculate_roc(closes, 10)

    # Green candle count (last 3 completed candles).
    last3 = recent_candles[-3:] if len(recent_candles) >= 3 else recent_candles
    green_candles_3 = sum(1 for c in last3 if c.close > c.open)

    # Candle body as pct of high-low range.
    candle_body_pct = None
    if recent_candles:
        c = recent_candles[-1]
        hl_range = c.high - c.low
        if hl_range > 0:
            candle_body_pct = abs(c.close - c.open) / hl_range

    # Activity ratio: tick count on the latest completed candle vs the
    # average of the prior 5. We use tick count rather than spot volume
    # because the spot WS only exposes a 24h cumulative volume snapshot
    # (not per-tick increments), which can decrease between ticks as old
    # volume rolls off the back. Tick rate is a standard market-micro
    # proxy for trade-arrival intensity. The DB column stays
    # `volume_ratio` to avoid a schema migration / breaking historical
    # rows; the semantic is now "activity ratio". See ml-quant.mdc.
    volume_ratio = None
    if len(recent_candles) >= 2:
        latest_ticks = getattr(recent_candles[-1], "tick_count", 0) or 0
        prior = recent_candles[-6:-1] if len(recent_candles) >= 6 else recent_candles[:-1]
        prior_ticks = [getattr(c, "tick_count", 0) or 0 for c in prior]
        avg_prior = sum(prior_ticks) / len(prior_ticks) if prior_ticks else 0
        if latest_ticks > 0 and avg_prior > 0:
            volume_ratio = latest_ticks / avg_prior

    # ATR as pct
    atr_pct = None
    if atr_filter.atr_pct_history:
        atr_pct = atr_filter.atr_pct_history[-1]

    # Spread as pct of mid price
    spread_pct = None
    if features.spread_cents is not None and features.mid_price:
        spread_pct = features.spread_cents / features.mid_price

    now = datetime.now(timezone.utc)

    tfi = None
    taker_buy_vol = None
    taker_sell_vol = None
    if historical_sync and hasattr(state, "kalshi_ticker") and state.kalshi_ticker:
        tfi = historical_sync.get_tfi(state.kalshi_ticker)
        taker_buy_vol, taker_sell_vol = historical_sync.get_tfi_volumes(state.kalshi_ticker)

    return {
        "obi": round(getattr(features, "obi_raw", features.obi), 4) if features.obi is not None else None,
        "roc_3": round(roc_3, 4) if roc_3 is not None else None,
        "roc_5": round(roc_5, 4) if roc_5 is not None else None,
        "roc_10": round(roc_10, 4) if roc_10 is not None else None,
        "atr_pct": round(atr_pct, 6) if atr_pct is not None else None,
        "spread_pct": round(spread_pct, 6) if spread_pct is not None else None,
        "bid_depth": features.total_bid_vol,
        "ask_depth": features.total_ask_vol,
        "green_candles_3": green_candles_3,
        "candle_body_pct": round(candle_body_pct, 4) if candle_body_pct is not None else None,
        "volume_ratio": round(volume_ratio, 4) if volume_ratio is not None else None,
        "time_remaining_sec": state.time_remaining_sec,
        "hour_of_day": now.hour,
        "day_of_week": now.weekday(),
        "tfi": round(tfi, 4) if tfi is not None else None,
        "taker_buy_vol": taker_buy_vol,
        "taker_sell_vol": taker_sell_vol,
    }


async def save_features(
    db_pool,
    *,
    trade_id: int,
    trading_mode: str,
    ticker: str,
    feature_dict: dict,
) -> Optional[int]:
    """Persist a feature snapshot to trade_features. Returns the row id."""
    try:
        cols = [
            "trade_id", "trading_mode", "ticker",
            "obi", "roc_3", "roc_5", "roc_10",
            "atr_pct", "spread_pct", "bid_depth", "ask_depth",
            "green_candles_3", "candle_body_pct", "volume_ratio",
            "time_remaining_sec", "hour_of_day", "day_of_week",
            "taker_buy_vol", "taker_sell_vol",
        ]
        vals = [
            trade_id, trading_mode, ticker,
            feature_dict.get("obi"),
            feature_dict.get("roc_3"),
            feature_dict.get("roc_5"),
            feature_dict.get("roc_10"),
            feature_dict.get("atr_pct"),
            feature_dict.get("spread_pct"),
            feature_dict.get("bid_depth"),
            feature_dict.get("ask_depth"),
            feature_dict.get("green_candles_3"),
            feature_dict.get("candle_body_pct"),
            feature_dict.get("volume_ratio"),
            feature_dict.get("time_remaining_sec"),
            feature_dict.get("hour_of_day"),
            feature_dict.get("day_of_week"),
            feature_dict.get("taker_buy_vol"),
            feature_dict.get("taker_sell_vol"),
        ]
        placeholders = ", ".join(f"%s" for _ in cols)
        col_str = ", ".join(cols)
        sql = f"INSERT INTO trade_features ({col_str}) VALUES ({placeholders}) RETURNING id"

        async with db_pool.connection() as conn:
            row = await conn.execute(sql, vals)
            result = await row.fetchone()
            return result[0] if result else None
    except Exception as e:
        logger.warning("ml.save_features_failed", error=str(e))
        return None


async def label_trade(
    db_pool,
    trade_id: int,
    pnl: float,
    mfe: float = 0.0,
    mae: float = 0.0,
) -> None:
    """Update the feature row with outcome label, PnL, MFE, and MAE at exit."""
    try:
        if pnl > 0.001:
            label = 1
        elif pnl < -0.001:
            label = -1
        else:
            label = 0

        async with db_pool.connection() as conn:
            await conn.execute(
                """UPDATE trade_features
                   SET label = %s, pnl = %s,
                       max_favorable_excursion = %s,
                       max_adverse_excursion = %s
                   WHERE trade_id = %s""",
                (label, round(pnl, 4), round(mfe, 4), round(mae, 4), trade_id),
            )
    except Exception as e:
        logger.warning("ml.label_trade_failed", trade_id=trade_id, error=str(e))
