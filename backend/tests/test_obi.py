"""Unit tests for OBI strategy helpers."""
from strategies.obi import Direction, check_obi_exit, evaluate_obi


def test_evaluate_obi_long_when_consecutive_above_threshold():
    history = [0.7, 0.72]
    assert (
        evaluate_obi(history, total_book_volume=5000, atr_regime="MEDIUM", has_position=False)
        == Direction.LONG
    )


def test_evaluate_obi_short_when_consecutive_below_threshold():
    history = [0.3, 0.28]
    assert (
        evaluate_obi(history, total_book_volume=5000, atr_regime="MEDIUM", has_position=False)
        == Direction.SHORT
    )


def test_evaluate_obi_neutral_in_high_regime():
    history = [0.7, 0.72]
    assert (
        evaluate_obi(history, total_book_volume=5000, atr_regime="HIGH", has_position=False)
        == Direction.NEUTRAL
    )


def test_evaluate_obi_neutral_insufficient_volume():
    history = [0.7, 0.72]
    assert (
        evaluate_obi(history, total_book_volume=500, atr_regime="MEDIUM", has_position=False)
        == Direction.NEUTRAL
    )


def test_evaluate_obi_neutral_when_has_position():
    history = [0.7, 0.72]
    assert (
        evaluate_obi(history, total_book_volume=5000, atr_regime="MEDIUM", has_position=True)
        == Direction.NEUTRAL
    )


def test_check_obi_exit_stop_loss():
    assert check_obi_exit("long", current_obi=0.9, pnl_pct=-0.03, candles_held=0, atr_regime="MEDIUM") == "STOP_LOSS"


def test_check_obi_exit_take_profit():
    assert check_obi_exit("long", current_obi=0.9, pnl_pct=0.04, candles_held=0, atr_regime="MEDIUM") == "TAKE_PROFIT"


def test_check_obi_exit_signal_decay_long():
    assert (
        check_obi_exit("long", current_obi=0.5, pnl_pct=0.0, candles_held=0, atr_regime="MEDIUM")
        == "SIGNAL_DECAY"
    )


def test_check_obi_exit_signal_decay_short():
    assert (
        check_obi_exit("short", current_obi=0.5, pnl_pct=0.0, candles_held=0, atr_regime="MEDIUM")
        == "SIGNAL_DECAY"
    )


def test_check_obi_exit_time_exit():
    assert (
        check_obi_exit("long", current_obi=0.7, pnl_pct=0.0, candles_held=10, atr_regime="MEDIUM")
        == "TIME_EXIT"
    )


def test_evaluate_obi_overrides_consecutive_and_thresholds():
    history = [0.5, 0.51]
    overrides = {"consecutive_readings": 2, "long_threshold": 0.5}
    assert (
        evaluate_obi(
            history,
            total_book_volume=5000,
            atr_regime="MEDIUM",
            has_position=False,
            overrides=overrides,
        )
        == Direction.LONG
    )


def test_check_obi_exit_overrides_stop_and_max_candles():
    assert (
        check_obi_exit(
            "long",
            current_obi=0.7,
            pnl_pct=-0.05,
            candles_held=0,
            atr_regime="MEDIUM",
            overrides={"stop_loss_pct": 0.04},
        )
        == "STOP_LOSS"
    )
    assert (
        check_obi_exit(
            "long",
            current_obi=0.7,
            pnl_pct=0.0,
            candles_held=2,
            atr_regime="MEDIUM",
            overrides={"max_candles_in_trade": 2},
        )
        == "TIME_EXIT"
    )
