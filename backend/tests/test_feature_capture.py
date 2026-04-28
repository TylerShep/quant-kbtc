"""Value-level regression tests for ml.feature_capture.extract_features.

Companion to test_train_serve_features.py, which only verifies the *names*
of the features extract_features() returns. This file pins the *values*:
specifically, that on healthy fixture inputs every one of the 14
ENTRY_FEATURES used by the XGBoost trainer comes back non-null.

Why this exists
---------------
Two silent feature-population bugs were producing 100% NULL values for
`roc_10` and `volume_ratio` in `trade_features` across 754 paper trades
on both the main bot and the canary:

  1. roc_10 lookback off-by-one: extract_features() requested
     `candle_aggregator.recent(10)`, returning at most 10 closes, but
     `strategies.roc.calculate_roc(closes, 10)` requires `len(closes) >= 11`
     (it computes a return between closes[-1] and closes[-(lookback+1)]).
     The function therefore silently returned None on every call.

  2. volume_ratio source had no signal: the spot WS exposes only a
     rolling 24h cumulative volume figure (volume_24h), not per-tick
     increments, and the coordinator never passed any volume to
     candle_aggregator.on_tick() anyway. Every candle therefore had
     volume == 0.0, so `latest_vol > 0 and avg_prior > 0` was always
     False and volume_ratio defaulted to None.

The fix in feature_capture.py + candle_aggregator.py:
  - Request recent(11) so roc_10's lookback can satisfy.
  - Add a per-candle `tick_count` field on Candle, increment on every
    tick, and compute volume_ratio from tick counts (a standard
    market-microstructure proxy for trade-arrival intensity). The DB
    column name stays `volume_ratio` to avoid a schema migration; the
    semantic is now "activity ratio" -- documented in feature_capture
    and in the ml-quant rule.

These tests pin both bugs so they can't silently regress: they would
have failed on the pre-fix code and pass on the post-fix code.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# The ENTRY_FEATURES list lives in scripts/train_xgb.py. Hard-code it
# here rather than re-parsing via AST: this test cares about the bot's
# extract_features() value contract, and a divergence between the two
# lists is precisely what test_train_serve_features.py already catches.
ENTRY_FEATURES = [
    "obi", "roc_3", "roc_5", "roc_10",
    "atr_pct", "spread_pct", "bid_depth", "ask_depth",
    "green_candles_3", "candle_body_pct", "volume_ratio",
    "time_remaining_sec", "hour_of_day", "day_of_week",
    # v2 execution-quality features (Tier 1.b, 2026-04-28). Keep this
    # list synced with scripts/train_xgb.py::ENTRY_FEATURES; the AST-based
    # contract test in test_train_serve_features.py guards the canonical
    # source-of-truth relationship.
    "minutes_to_contract_close", "quoted_spread_at_entry_bps",
    "book_thickness_at_offer", "recent_trade_count_60s",
]


def _make_candle(close: float, tick_count: int = 5, *, open_=None, high=None, low=None) -> types.SimpleNamespace:
    """Build a single fake candle. Defaults pick a green body with a
    nonzero high-low range so candle_body_pct is non-None."""
    o = open_ if open_ is not None else close - 0.5
    return types.SimpleNamespace(
        open=o,
        high=high if high is not None else max(o, close) + 0.5,
        low=low if low is not None else min(o, close) - 0.5,
        close=close,
        volume=0.0,
        tick_count=tick_count,
    )


def _make_aggregator(candles: list) -> MagicMock:
    """Mock candle_aggregator that returns up to N most-recent candles
    from `candles` when .recent(N) is called -- mirrors the real
    deque-backed behaviour: recent(N) yields min(len(candles), N) entries.
    """
    agg = MagicMock()

    def _recent(n: int) -> list:
        return candles[-n:]

    agg.recent.side_effect = _recent
    return agg


class _StubOrderBook:
    """Minimal in-memory book that satisfies the bits of OrderBookState the
    extractor needs (``book_thickness_within``). Tests can override the
    levels via the constructor."""

    def __init__(self, bid_levels=None, ask_levels=None):
        self.bids = dict(bid_levels or {})
        self.asks = dict(ask_levels or {})

    def book_thickness_within(self, center_price: float, half_width_cents: float) -> float:
        lo = center_price - half_width_cents
        hi = center_price + half_width_cents
        total = 0.0
        for price, size in self.bids.items():
            if lo <= price <= hi:
                total += size
        for price, size in self.asks.items():
            if lo <= price <= hi:
                total += size
        return total


def _default_book() -> _StubOrderBook:
    """Symmetric book around mid=50: 5 bid levels at 48,49,50,51,52 with
    sizes 100 each, plus a far-away level outside the ±5c window. Total
    in-window thickness: 5 levels * 100 = 500 (note: the constructor
    here covers both sides as one combined dict; in OrderBookState the
    levels are split into bids and asks but the helper sums both)."""
    bids = {48: 100, 49: 100, 50: 100, 30: 999}  # 30 is outside ±5
    asks = {51: 100, 52: 100, 60: 999}  # 60 is outside ±5
    return _StubOrderBook(bid_levels=bids, ask_levels=asks)


def _make_state(**overrides) -> types.SimpleNamespace:
    base = dict(
        time_remaining_sec=420,
        kalshi_ticker="KXBTC-25APR2400-T100000",
        order_book=_default_book(),
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _make_features() -> MagicMock:
    f = MagicMock()
    f.obi = 0.42
    f.obi_raw = 0.42
    f.spread_cents = 2
    f.mid_price = 50
    f.total_bid_vol = 250
    f.total_ask_vol = 220
    return f


def _make_atr(pct: float = 0.0125) -> types.SimpleNamespace:
    return types.SimpleNamespace(atr_pct_history=[pct])


def _extract(*, candles, **overrides):
    """Convenience wrapper: drop any cached import of feature_capture
    (so we exercise the latest source) then call extract_features."""
    sys.modules.pop("ml.feature_capture", None)
    from ml.feature_capture import extract_features  # noqa: WPS433

    kwargs = dict(
        features=_make_features(),
        candle_aggregator=_make_aggregator(candles),
        atr_filter=_make_atr(),
        state=_make_state(),
        historical_sync=None,
    )
    kwargs.update(overrides)
    return extract_features(**kwargs)


def _make_healthy_candles(n: int = 11) -> list:
    """11 monotonically-rising candles with varying tick_counts. Enough
    history to populate every entry feature including roc_10."""
    candles = []
    base_price = 100.0
    for i in range(n):
        candles.append(_make_candle(close=base_price + i * 0.25, tick_count=5 + i))
    return candles


# ─── BUG-class regression tests: every entry feature is non-null on
#     healthy inputs. Either of the original bugs would fail these. ───

def test_all_entry_features_non_null_on_healthy_inputs():
    """The full happy-path contract: with 11 well-formed candles,
    nonzero spread, nonzero ATR, and a kalshi_ticker, every one of the
    14 ENTRY_FEATURES used by the XGBoost trainer must be non-None.

    Pre-fix this would have failed on roc_10 and volume_ratio.
    """
    out = _extract(candles=_make_healthy_candles(11))
    nulls = [k for k in ENTRY_FEATURES if out.get(k) is None]
    assert not nulls, (
        f"extract_features returned NULL for entry features {nulls} on "
        f"healthy inputs. This is the bug class that produced 100% NULL "
        f"roc_10 and volume_ratio across 754 trade_features rows -- see "
        f"the docstring on this test file. Full output: {out}"
    )


def test_roc_10_populated_when_eleven_candles_available():
    """roc_10 needs len(closes) >= 11. With exactly 11 candles, it must
    populate. This is the direct off-by-one regression test."""
    out = _extract(candles=_make_healthy_candles(11))
    assert out["roc_10"] is not None, (
        "roc_10 was None with 11 candles -- the lookback off-by-one bug "
        "is back. extract_features must request recent(11) (or more)."
    )


def test_roc_10_returns_none_when_only_ten_candles():
    """Honest behaviour: with only 10 completed candles, roc_10 cannot
    be computed (lookback 10 needs 11 closes). Confirms we don't paper
    over insufficient history with a 0 / NaN value."""
    out = _extract(candles=_make_healthy_candles(10))
    assert out["roc_10"] is None
    assert out["roc_5"] is not None
    assert out["roc_3"] is not None


def test_volume_ratio_populated_from_tick_count():
    """volume_ratio uses per-candle tick_count as an activity proxy.
    With 11 candles whose tick_counts vary (5..15), the latest candle
    (15 ticks) divided by avg of prior 5 (10..14 -> avg 12) must give
    1.25, non-None and > 0.

    Pre-fix this would have been None because every candle had volume=0.
    """
    out = _extract(candles=_make_healthy_candles(11))
    assert out["volume_ratio"] is not None, (
        "volume_ratio was None on healthy inputs -- the activity-proxy "
        "wiring regressed."
    )
    assert out["volume_ratio"] > 0
    # Sanity-check the actual ratio: latest tick_count is 5 + 10 = 15;
    # prior 5 are tick_counts at indices 5..9 -> 10,11,12,13,14, avg=12.
    # 15 / 12 = 1.25.
    assert abs(out["volume_ratio"] - 1.25) < 1e-9


def test_volume_ratio_is_none_when_only_one_candle():
    """With <2 candles there is no prior window, so volume_ratio is
    correctly None rather than divide-by-zero or 1.0 fallback."""
    out = _extract(candles=_make_healthy_candles(1))
    assert out["volume_ratio"] is None


def test_volume_ratio_is_none_when_all_tick_counts_are_zero():
    """If somehow every candle reports tick_count=0 (e.g. an aggregator
    not yet wired to a tick stream), volume_ratio must remain None
    rather than silently emit 0 -- 0 is a valid feature value the
    model would learn from. Distinguishing 'no signal' from 'true 0' is
    the whole point of nullable features here."""
    candles = [_make_candle(close=100.0 + i, tick_count=0) for i in range(11)]
    out = _extract(candles=candles)
    assert out["volume_ratio"] is None


# ─── Field-level sanity: each feature is the right shape, in the right
#     range, on healthy inputs. Catches the "feature is non-null but
#     completely wrong" failure mode. ───

def test_obi_passthrough_uses_raw_when_available():
    """obi_raw should be preferred over obi (smoothed) per the existing
    code path; verify the value flows through unmodified to 4 decimals."""
    out = _extract(candles=_make_healthy_candles(11))
    assert out["obi"] == 0.42


def test_green_candles_3_counts_last_three_completed():
    """Only the last 3 completed candles count, even if more are
    available. Verify with a deliberately mixed sequence."""
    candles = (
        [_make_candle(close=100.0, tick_count=5)] * 8
        + [_make_candle(close=99.0, open_=100.0, tick_count=5)]   # red
        + [_make_candle(close=101.0, open_=100.0, tick_count=5)]  # green
        + [_make_candle(close=102.0, open_=101.0, tick_count=5)]  # green
    )
    out = _extract(candles=candles)
    assert out["green_candles_3"] == 2


def test_candle_body_pct_uses_latest_candle():
    """candle_body_pct = |close - open| / (high - low). Verify on a
    deterministic last candle."""
    # Build 10 boring candles + 1 with a known body ratio.
    candles = [_make_candle(close=100.0, tick_count=5) for _ in range(10)]
    candles.append(types.SimpleNamespace(
        open=100.0, high=104.0, low=96.0, close=102.0,
        volume=0.0, tick_count=5,
    ))
    out = _extract(candles=candles)
    # |102 - 100| / (104 - 96) = 2/8 = 0.25
    assert abs(out["candle_body_pct"] - 0.25) < 1e-9


def test_spread_pct_zero_mid_price_does_not_crash():
    """Defensive: if mid_price is 0/None (markets not yet open or
    one-sided book), spread_pct must be None, not raise."""
    candles = _make_healthy_candles(11)
    sys.modules.pop("ml.feature_capture", None)
    from ml.feature_capture import extract_features  # noqa: WPS433

    feats = _make_features()
    feats.mid_price = 0
    out = extract_features(
        features=feats,
        candle_aggregator=_make_aggregator(candles),
        atr_filter=_make_atr(),
        state=_make_state(),
        historical_sync=None,
    )
    assert out["spread_pct"] is None


def test_extract_features_no_historical_sync_omits_kalshi_volumes():
    """taker_buy_vol / taker_sell_vol / tfi all come from
    historical_sync. When it's None (e.g. unit tests, cold start), the
    keys are still present in the dict but their values are None."""
    out = _extract(candles=_make_healthy_candles(11))
    assert out["tfi"] is None
    assert out["taker_buy_vol"] is None
    assert out["taker_sell_vol"] is None


# ─── v2 execution-quality features (Tier 1.b, 2026-04-28) ─────────────
# These four were added because the original 14 features describe market
# state but don't capture how WELL the bot will fill at the current price.
# After the SHORT_SETTLEMENT_GUARD analysis showed that ~$6.6k of paper
# losses came from shorts entered with <13 min to close, capturing the
# distance-to-close as a learnable feature became a clear win.
# See backend/migrations/008_execution_quality_features.sql.

def test_minutes_to_contract_close_computed_from_time_remaining_sec():
    """state.time_remaining_sec=420 -> 7.0 minutes."""
    out = _extract(candles=_make_healthy_candles(11))
    assert out["minutes_to_contract_close"] == 7.0


def test_minutes_to_contract_close_none_when_state_unknown():
    """Right after a contract rotation the bot may have time_remaining_sec=None.
    The feature must be None in that case so the model can distinguish it
    from 'we know the answer is 0'."""
    out = _extract(
        candles=_make_healthy_candles(11),
        state=_make_state(time_remaining_sec=None),
    )
    assert out["minutes_to_contract_close"] is None


def test_quoted_spread_at_entry_bps_computed_correctly():
    """Default fixture: spread_cents=2, mid_price=50 -> (2/50)*10000 = 400 bps."""
    out = _extract(candles=_make_healthy_candles(11))
    assert out["quoted_spread_at_entry_bps"] == 400


def test_quoted_spread_at_entry_bps_none_when_inputs_missing():
    """Defensive: if either spread_cents or mid_price is None / 0, the
    feature must be None rather than divide-by-zero or inf."""
    sys.modules.pop("ml.feature_capture", None)
    from ml.feature_capture import extract_features  # noqa: WPS433

    feats = _make_features()
    feats.spread_cents = None
    out = extract_features(
        features=feats,
        candle_aggregator=_make_aggregator(_make_healthy_candles(11)),
        atr_filter=_make_atr(),
        state=_make_state(),
        historical_sync=None,
    )
    assert out["quoted_spread_at_entry_bps"] is None

    feats.spread_cents = 2
    feats.mid_price = 0
    out = extract_features(
        features=feats,
        candle_aggregator=_make_aggregator(_make_healthy_candles(11)),
        atr_filter=_make_atr(),
        state=_make_state(),
        historical_sync=None,
    )
    assert out["quoted_spread_at_entry_bps"] is None


def test_book_thickness_at_offer_sums_within_5c_window():
    """Default fixture book: bids at 48,49,50 (size 100 each) + asks at
    51,52 (size 100 each). The far-away 30/60 levels are excluded by the
    ±5c window (50 ± 5 = [45, 55]). Expected: 500 total."""
    out = _extract(candles=_make_healthy_candles(11))
    assert out["book_thickness_at_offer"] == 500


def test_book_thickness_at_offer_none_when_no_order_book():
    """If state.order_book is missing (defensive: simulator stubs), the
    feature must be None rather than raise."""
    out = _extract(
        candles=_make_healthy_candles(11),
        state=_make_state(order_book=None),
    )
    assert out["book_thickness_at_offer"] is None


def test_book_thickness_at_offer_none_when_mid_price_missing():
    """Without a mid_price we have no center for the window -- can't
    compute thickness. Feature must be None."""
    sys.modules.pop("ml.feature_capture", None)
    from ml.feature_capture import extract_features  # noqa: WPS433

    feats = _make_features()
    feats.mid_price = None
    out = extract_features(
        features=feats,
        candle_aggregator=_make_aggregator(_make_healthy_candles(11)),
        atr_filter=_make_atr(),
        state=_make_state(),
        historical_sync=None,
    )
    assert out["book_thickness_at_offer"] is None


def test_recent_trade_count_60s_uses_latest_candle_tick_count():
    """The healthy fixture has 11 candles with tick_counts 5..15. The
    latest is 15, so recent_trade_count_60s must be 15."""
    out = _extract(candles=_make_healthy_candles(11))
    assert out["recent_trade_count_60s"] == 15


def test_recent_trade_count_60s_none_when_no_candles():
    """No candles -> no signal -> None (not 0)."""
    out = _extract(candles=[])
    assert out["recent_trade_count_60s"] is None


def test_all_v2_features_present_in_output_dict_keys():
    """Schema check: every v2 feature appears in the extractor output
    dict, even when its value happens to be None. This is what
    save_features() depends on -- it does feature_dict.get(name) and a
    missing KEY would silently insert NULL while a present-but-None VALUE
    is the explicit signal we want."""
    out = _extract(candles=_make_healthy_candles(11))
    for name in (
        "minutes_to_contract_close",
        "quoted_spread_at_entry_bps",
        "book_thickness_at_offer",
        "recent_trade_count_60s",
    ):
        assert name in out, f"v2 feature {name!r} missing from extract_features output"
