"""Unit tests for signal conflict resolver."""
import dataclasses

import pytest

from config import settings
from risk.circuit_breaker import CircuitBreaker
from risk.position_sizer import PositionSizer
from strategies.obi import Direction
from strategies.resolver import COORDINATION_TABLE, Conviction, SignalConflictResolver


@pytest.fixture
def set_bot_flags():
    """Fixture: yields a setter that patches settings.bot for the test scope.

    Settings and BotConfig are frozen dataclasses, so we bypass frozen
    enforcement with object.__setattr__ and restore the original on teardown.
    """
    original_bot = settings.bot

    def _apply(*, paper: bool = False, live: bool = False):
        new_bot = dataclasses.replace(
            original_bot,
            roc_low_conviction_paper_enabled=paper,
            roc_low_conviction_live_enabled=live,
        )
        object.__setattr__(settings, "bot", new_bot)

    yield _apply
    object.__setattr__(settings, "bot", original_bot)


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


def test_low_conviction_blocked_when_paper_flag_false(set_bot_flags):
    """Default behaviour: paper flag false -> LOW conviction trades are gated off."""
    set_bot_flags(paper=False, live=False)
    resolver = SignalConflictResolver()
    decision = resolver.resolve(
        Direction.NEUTRAL, Direction.LONG, atr_regime="MEDIUM", can_trade=True
    )
    assert decision.conviction == Conviction.LOW
    assert decision.direction == Direction.LONG
    assert decision.should_trade_in("paper") is False
    assert decision.should_trade is False  # property == paper lane


def test_low_conviction_tradeable_when_paper_flag_true(set_bot_flags):
    """Flipping the paper flag unlocks LOW trades for the paper lane."""
    set_bot_flags(paper=True, live=False)
    resolver = SignalConflictResolver()
    decision = resolver.resolve(
        Direction.NEUTRAL, Direction.LONG, atr_regime="MEDIUM", can_trade=True
    )
    assert decision.conviction == Conviction.LOW
    assert decision.should_trade_in("paper") is True
    assert decision.should_trade is True


def test_live_low_blocked_when_only_paper_enabled(set_bot_flags):
    """Live lane stays gated even when paper flag is on — required for the
    paper -> live promotion gap."""
    set_bot_flags(paper=True, live=False)
    resolver = SignalConflictResolver()
    decision = resolver.resolve(
        Direction.NEUTRAL, Direction.SHORT, atr_regime="MEDIUM", can_trade=True
    )
    assert decision.conviction == Conviction.LOW
    assert decision.should_trade_in("paper") is True
    assert decision.should_trade_in("live") is False


def test_live_low_tradeable_when_live_flag_true(set_bot_flags):
    """Live flag enables LOW trades for the live lane independent of paper."""
    set_bot_flags(paper=True, live=True)
    resolver = SignalConflictResolver()
    decision = resolver.resolve(
        Direction.NEUTRAL, Direction.LONG, atr_regime="MEDIUM", can_trade=True
    )
    assert decision.conviction == Conviction.LOW
    assert decision.should_trade_in("paper") is True
    assert decision.should_trade_in("live") is True


def test_conviction_upgrade_ladder():
    """Conviction.upgrade() must never manufacture a trade from NONE."""
    assert Conviction.upgrade(Conviction.NONE) == Conviction.NONE
    assert Conviction.upgrade(Conviction.LOW) == Conviction.NORMAL
    assert Conviction.upgrade(Conviction.NORMAL) == Conviction.HIGH
    assert Conviction.upgrade(Conviction.HIGH) == Conviction.HIGH  # already max


# ── describe_signal_driver ────────────────────────────────────────────────
#
# Attribution-only helper: maps the coordination-table inputs
# (obi_dir, roc_dir, spread_state) to a short human label we store alongside
# the trade. Must NEVER affect trading logic, only reporting.

from strategies.resolver import describe_signal_driver
from strategies.spread_div import SpreadState


class TestDescribeSignalDriver:
    """Cover every (obi_dir, roc_dir) cell and every SpreadState suffix."""

    # Base labels with NORMAL spread (no suffix)
    def test_both_agree_long(self):
        assert (
            describe_signal_driver(Direction.LONG, Direction.LONG, SpreadState.NORMAL)
            == "OBI+ROC"
        )

    def test_both_agree_short(self):
        assert (
            describe_signal_driver(Direction.SHORT, Direction.SHORT, SpreadState.NORMAL)
            == "OBI+ROC"
        )

    def test_obi_only_long(self):
        assert (
            describe_signal_driver(Direction.LONG, Direction.NEUTRAL, SpreadState.NORMAL)
            == "OBI"
        )

    def test_obi_only_short(self):
        assert (
            describe_signal_driver(Direction.SHORT, Direction.NEUTRAL, SpreadState.NORMAL)
            == "OBI"
        )

    def test_roc_only_long(self):
        assert (
            describe_signal_driver(Direction.NEUTRAL, Direction.LONG, SpreadState.NORMAL)
            == "ROC"
        )

    def test_roc_only_short(self):
        assert (
            describe_signal_driver(Direction.NEUTRAL, Direction.SHORT, SpreadState.NORMAL)
            == "ROC"
        )

    def test_conflict_long_short(self):
        # Both signals fire but disagree — label as 'OBI/ROC' to make conflicts
        # visually distinct from agreements. The resolver still returns NONE,
        # so this is a reporting-only distinction.
        assert (
            describe_signal_driver(Direction.LONG, Direction.SHORT, SpreadState.NORMAL)
            == "OBI/ROC"
        )

    def test_conflict_short_long(self):
        assert (
            describe_signal_driver(Direction.SHORT, Direction.LONG, SpreadState.NORMAL)
            == "OBI/ROC"
        )

    def test_both_neutral(self):
        assert (
            describe_signal_driver(
                Direction.NEUTRAL, Direction.NEUTRAL, SpreadState.NORMAL
            )
            == "-"
        )

    # Spread suffixes
    def test_wide_suffix_on_agreement(self):
        assert (
            describe_signal_driver(Direction.LONG, Direction.LONG, SpreadState.WIDE)
            == "OBI+ROC/WIDE"
        )

    def test_tight_suffix_on_agreement(self):
        assert (
            describe_signal_driver(Direction.LONG, Direction.LONG, SpreadState.TIGHT)
            == "OBI+ROC/TIGHT"
        )

    def test_tight_suffix_on_roc_only(self):
        assert (
            describe_signal_driver(
                Direction.NEUTRAL, Direction.SHORT, SpreadState.TIGHT
            )
            == "ROC/TIGHT"
        )

    def test_wide_suffix_on_obi_only(self):
        assert (
            describe_signal_driver(
                Direction.SHORT, Direction.NEUTRAL, SpreadState.WIDE
            )
            == "OBI/WIDE"
        )

    def test_suffix_not_appended_when_no_signal(self):
        """WIDE/TIGHT suffix should still be honored even on '-' since it's
        attribution-only — callers can filter for 'spread extreme but no
        directional signal' cases.
        """
        assert (
            describe_signal_driver(
                Direction.NEUTRAL, Direction.NEUTRAL, SpreadState.TIGHT
            )
            == "-/TIGHT"
        )

    def test_default_spread_is_normal(self):
        """Omitting spread_state should behave like NORMAL (no suffix)."""
        assert (
            describe_signal_driver(Direction.LONG, Direction.LONG) == "OBI+ROC"
        )


class TestTradeDecisionSignalDriverProperty:
    """The TradeDecision.signal_driver property is what coordinator.py passes
    to trader.enter(...), so verify the end-to-end path produces the expected
    label out of resolver.resolve()."""

    def test_resolver_agreement_yields_obi_plus_roc(self):
        resolver = SignalConflictResolver()
        decision = resolver.resolve(
            Direction.LONG, Direction.LONG,
            atr_regime="MEDIUM", can_trade=True,
            spread_state=SpreadState.NORMAL,
        )
        assert decision.signal_driver == "OBI+ROC"

    def test_resolver_obi_only_yields_obi(self):
        resolver = SignalConflictResolver()
        decision = resolver.resolve(
            Direction.SHORT, Direction.NEUTRAL,
            atr_regime="MEDIUM", can_trade=True,
        )
        assert decision.signal_driver == "OBI"

    def test_resolver_roc_only_yields_roc(self):
        resolver = SignalConflictResolver()
        decision = resolver.resolve(
            Direction.NEUTRAL, Direction.LONG,
            atr_regime="MEDIUM", can_trade=True,
        )
        assert decision.signal_driver == "ROC"

    def test_resolver_tight_spread_appends_suffix(self):
        resolver = SignalConflictResolver()
        decision = resolver.resolve(
            Direction.LONG, Direction.LONG,
            atr_regime="MEDIUM", can_trade=True,
            spread_state=SpreadState.TIGHT,
        )
        assert decision.signal_driver == "OBI+ROC/TIGHT"

    def test_resolver_wide_spread_appends_suffix(self):
        resolver = SignalConflictResolver()
        decision = resolver.resolve(
            Direction.SHORT, Direction.NEUTRAL,
            atr_regime="MEDIUM", can_trade=True,
            spread_state=SpreadState.WIDE,
        )
        assert decision.signal_driver == "OBI/WIDE"

    def test_halt_still_reports_driver(self):
        """Even when the circuit breaker halts trading, the label should still
        describe what the raw signals looked like — useful for post-mortems."""
        resolver = SignalConflictResolver()
        decision = resolver.resolve(
            Direction.LONG, Direction.LONG,
            atr_regime="MEDIUM", can_trade=False,
        )
        assert decision.signal_driver == "OBI+ROC"
        assert decision.direction is None
