"""Unit tests for CandleAggregator."""
from data.candle_aggregator import CandleAggregator


def test_on_tick_first_tick_returns_none():
    agg = CandleAggregator(interval_sec=900, max_candles=500)
    assert agg.on_tick(100.0, 50.0) is None
    assert agg.current is not None
    assert agg.current.open == 50.0


def test_on_tick_returns_completed_candle_on_boundary_cross():
    agg = CandleAggregator(interval_sec=900, max_candles=500)
    boundary0 = 0.0
    agg.on_tick(boundary0 + 10.0, 50.0)
    completed = agg.on_tick(boundary0 + 900.0, 60.0)
    assert completed is not None
    assert completed.timestamp == boundary0
    assert completed.close == 50.0
    assert agg.current.timestamp == boundary0 + 900.0


def test_ohlc_values_correct_within_interval():
    agg = CandleAggregator(interval_sec=100, max_candles=50)
    base = 1000.0
    agg.on_tick(base + 0.0, 10.0)
    agg.on_tick(base + 1.0, 15.0)
    agg.on_tick(base + 2.0, 8.0)
    agg.on_tick(base + 3.0, 12.0, volume=2.0)
    c = agg.current
    assert c.open == 10.0
    assert c.high == 15.0
    assert c.low == 8.0
    assert c.close == 12.0
    assert c.volume == 2.0


def test_deque_max_size_enforced():
    max_candles = 5
    agg = CandleAggregator(interval_sec=10, max_candles=max_candles)
    for i in range(max_candles + 3):
        t = float(i * 10)
        agg.on_tick(t, 1.0)
        agg.on_tick(t + 9.9, 1.0)
    assert len(agg.candles) == max_candles
