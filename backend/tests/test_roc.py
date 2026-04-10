"""Unit tests for ROC strategy helpers."""
from strategies.obi import Direction
from strategies.roc import calculate_roc, check_roc_exit, evaluate_roc


def _up_candles(n: int = 3):
    return [{"open": 100.0, "close": 101.0} for _ in range(n)]


def _down_candles(n: int = 3):
    return [{"open": 101.0, "close": 100.0} for _ in range(n)]


def test_evaluate_roc_long_positive_roc_and_up_candles():
    closes = [100.0, 100.0, 100.0, 101.5]
    candles = _up_candles(3)
    assert (
        evaluate_roc(
            closes,
            candles,
            atr_regime="MEDIUM",
            obi_direction=Direction.NEUTRAL,
            has_position=False,
        )
        == Direction.LONG
    )


def test_evaluate_roc_short_negative_roc_and_down_candles():
    closes = [100.0, 100.0, 100.0, 98.5]
    candles = _down_candles(3)
    assert (
        evaluate_roc(
            closes,
            candles,
            atr_regime="MEDIUM",
            obi_direction=Direction.NEUTRAL,
            has_position=False,
        )
        == Direction.SHORT
    )


def test_evaluate_roc_neutral_in_low_regime():
    closes = [100.0, 100.0, 100.0, 101.5]
    candles = _up_candles(3)
    assert (
        evaluate_roc(
            closes,
            candles,
            atr_regime="LOW",
            obi_direction=Direction.NEUTRAL,
            has_position=False,
        )
        == Direction.NEUTRAL
    )


def test_calculate_roc_math():
    closes = [100.0, 100.0, 100.0, 102.0]
    assert calculate_roc(closes, lookback=3) == 2.0
    assert calculate_roc([100.0], lookback=3) is None
    assert calculate_roc([100.0, 0.0, 100.0, 100.0], lookback=2) is None


def test_check_roc_exit_stop_loss():
    assert check_roc_exit("long", pnl_pct=-0.03, entry_roc=1.0, current_roc=1.0, latest_candle=None, candles_held=0) == "STOP_LOSS"


def test_check_roc_exit_take_profit():
    assert check_roc_exit("long", pnl_pct=0.04, entry_roc=1.0, current_roc=1.0, latest_candle=None, candles_held=0) == "TAKE_PROFIT"


def test_check_roc_exit_blowoff_take_profit():
    candle = {"open": 100.0, "close": 102.0}
    assert (
        check_roc_exit("long", pnl_pct=0.01, entry_roc=1.0, current_roc=1.0, latest_candle=candle, candles_held=0)
        == "BLOWOFF_TAKE_PROFIT"
    )


def test_check_roc_exit_momentum_stall():
    assert (
        check_roc_exit(
            "long",
            pnl_pct=0.0,
            entry_roc=2.0,
            current_roc=0.5,
            latest_candle={"open": 100.0, "close": 100.5},
            candles_held=0,
        )
        == "MOMENTUM_STALL"
    )


def test_check_roc_exit_candle_reversal_long():
    assert (
        check_roc_exit(
            "long",
            pnl_pct=0.0,
            entry_roc=1.0,
            current_roc=1.0,
            latest_candle={"open": 100.0, "close": 99.0},
            candles_held=0,
        )
        == "CANDLE_REVERSAL"
    )


def test_check_roc_exit_candle_reversal_short():
    assert (
        check_roc_exit(
            "short",
            pnl_pct=0.0,
            entry_roc=-1.0,
            current_roc=-1.0,
            latest_candle={"open": 100.0, "close": 101.0},
            candles_held=0,
        )
        == "CANDLE_REVERSAL"
    )


def test_check_roc_exit_time_exit():
    assert (
        check_roc_exit(
            "long",
            pnl_pct=0.0,
            entry_roc=1.0,
            current_roc=1.0,
            latest_candle={"open": 100.0, "close": 100.0},
            candles_held=10,
        )
        == "TIME_EXIT"
    )


def test_evaluate_roc_overrides_respected():
    closes = [100.0, 100.0, 100.0, 100.2]
    candles = [
        {"open": 100.0, "close": 100.05},
        {"open": 100.0, "close": 100.05},
        {"open": 100.0, "close": 100.05},
    ]
    overrides = {
        "roc_long_threshold": 0.1,
        "roc_max_cap": 5.0,
        "roc_candle_confirm_min": 1,
    }
    assert (
        evaluate_roc(
            closes,
            candles,
            atr_regime="MEDIUM",
            obi_direction=Direction.NEUTRAL,
            has_position=False,
            overrides=overrides,
        )
        == Direction.LONG
    )
