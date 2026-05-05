"""FeatureEngine momentum-window regression tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from data.manager import OrderBookState
from features.engine import FeatureEngine


def _market_state(spot_price: float):
    book = OrderBookState()
    book.apply_level("yes", 48, 1200)
    book.apply_level("no", 47, 1000)  # yes ask = 53
    return SimpleNamespace(order_book=book, spot_price=spot_price)


def test_feature_engine_populates_intra_candle_spot_momentum():
    engine = FeatureEngine()
    state = _market_state(100.0)

    # FeatureEngine reads wall-clock for both OBI smoothing and momentum
    # windows, so provide a pair of timestamps per update call.
    with patch(
        "features.engine.time.time",
        side_effect=[0.0, 0.0, 30.0, 30.0, 60.0, 60.0],
    ):
        engine.update("BTC", state)
        state.spot_price = 102.0
        engine.update("BTC", state)
        state.spot_price = 103.0
        snap = engine.update("BTC", state)

    assert snap is not None
    assert snap.spot_roc_30s == pytest.approx(((103.0 - 102.0) / 102.0) * 100.0)
    assert snap.spot_roc_60s == pytest.approx(3.0)
    assert snap.spot_momentum_decay is not None
    assert snap.spot_momentum_decay < 1.0

