"""Unit tests for signal conflict resolver."""
from risk.circuit_breaker import CircuitBreaker
from risk.position_sizer import PositionSizer
from strategies.obi import Direction
from strategies.resolver import COORDINATION_TABLE, Conviction, SignalConflictResolver


def test_coordination_table_all_nine_entries():
    assert len(COORDINATION_TABLE) == 9
    assert COORDINATION_TABLE[(Direction.LONG, Direction.LONG)] == (Direction.LONG, Conviction.HIGH)
    assert COORDINATION_TABLE[(Direction.SHORT, Direction.SHORT)] == (Direction.SHORT, Conviction.HIGH)
    assert COORDINATION_TABLE[(Direction.LONG, Direction.NEUTRAL)] == (Direction.LONG, Conviction.NORMAL)
    assert COORDINATION_TABLE[(Direction.SHORT, Direction.NEUTRAL)] == (Direction.SHORT, Conviction.NORMAL)
    assert COORDINATION_TABLE[(Direction.NEUTRAL, Direction.LONG)] == (Direction.LONG, Conviction.LOW)
    assert COORDINATION_TABLE[(Direction.NEUTRAL, Direction.SHORT)] == (Direction.SHORT, Conviction.LOW)
    assert COORDINATION_TABLE[(Direction.LONG, Direction.SHORT)] == (None, Conviction.NONE)
    assert COORDINATION_TABLE[(Direction.SHORT, Direction.LONG)] == (None, Conviction.NONE)
    assert COORDINATION_TABLE[(Direction.NEUTRAL, Direction.NEUTRAL)] == (None, Conviction.NONE)


def test_resolver_applies_each_table_row():
    resolver = SignalConflictResolver()
    for (obi_d, roc_d), (exp_dir, exp_conv) in COORDINATION_TABLE.items():
        decision = resolver.resolve(obi_d, roc_d, atr_regime="MEDIUM", can_trade=True)
        assert decision.direction == exp_dir
        assert decision.conviction == exp_conv
        assert decision.obi_dir == obi_d
        assert decision.roc_dir == roc_d


def test_resolver_atr_high_gate():
    resolver = SignalConflictResolver()
    decision = resolver.resolve(Direction.LONG, Direction.LONG, atr_regime="HIGH", can_trade=True)
    assert decision.direction is None
    assert decision.conviction == Conviction.NONE
    assert decision.skip_reason == "ATR_REGIME_HIGH"
    assert decision.should_trade is False


def test_resolver_circuit_breaker_gate():
    resolver = SignalConflictResolver()
    decision = resolver.resolve(Direction.LONG, Direction.LONG, atr_regime="MEDIUM", can_trade=False)
    assert decision.direction is None
    assert decision.conviction == Conviction.NONE
    assert decision.skip_reason == "CIRCUIT_BREAKER_ACTIVE"
    assert decision.should_trade is False


def test_circuit_breaker_integrates_with_resolver_state():
    sizer = PositionSizer(10_000.0)
    breaker = CircuitBreaker(sizer)
    resolver = SignalConflictResolver()
    can, _ = breaker.can_trade()
    decision = resolver.resolve(Direction.LONG, Direction.LONG, atr_regime="MEDIUM", can_trade=can)
    assert can is True
    assert decision.should_trade is True
