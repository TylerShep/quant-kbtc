"""Unit tests for the live-lane edge profile filter.

Covers each rejection rule independently, plus the OBI+ROC agreement bypass
and the disabled passthrough. Paper lane is never the subject of these tests
because the coordinator short-circuits before calling the filter.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from config import settings
from filters.edge_profile import evaluate as evaluate_edge_profile
from strategies.obi import Direction
from strategies.resolver import Conviction, TradeDecision
from strategies.spread_div import SpreadState


@pytest.fixture
def override_edge():
    """Patch fields on settings.edge_profile (frozen dataclass) and restore."""
    original = settings.edge_profile

    def _apply(**kwargs):
        new_cfg = dataclasses.replace(original, **kwargs)
        object.__setattr__(settings, "edge_profile", new_cfg)

    yield _apply
    object.__setattr__(settings, "edge_profile", original)


def _decision(
    *,
    direction: Direction = Direction.LONG,
    conviction: Conviction = Conviction.NORMAL,
    obi_dir: Direction = Direction.LONG,
    roc_dir: Direction = Direction.NEUTRAL,
    spread_state: SpreadState = SpreadState.NORMAL,
) -> TradeDecision:
    return TradeDecision(
        direction=direction,
        conviction=conviction,
        obi_dir=obi_dir,
        roc_dir=roc_dir,
        spread_state=spread_state,
    )


# ─── disabled / no-op behavior ────────────────────────────────────────────
def test_disabled_passthrough_for_anything(override_edge):
    """When EDGE_LIVE_PROFILE_ENABLED=false, every trade passes."""
    override_edge(enabled=False)
    decision = _decision(direction=Direction.SHORT, conviction=Conviction.LOW)
    allowed, reason = evaluate_edge_profile(decision=decision, entry_price=99.0)
    assert allowed is True
    assert reason is None


def test_no_trade_returns_passthrough(override_edge):
    """If the resolver already produced no trade, filter is a no-op."""
    override_edge(enabled=True)
    decision = _decision(direction=None, conviction=Conviction.NONE)
    allowed, reason = evaluate_edge_profile(decision=decision, entry_price=20.0)
    assert allowed is True
    assert reason is None


# ─── long-only kill switch (legacy hard block) ───────────────────────────
def test_blocks_short_when_long_only(override_edge):
    """When the legacy ``long_only`` kill switch is True, every short is
    rejected regardless of conviction or price."""
    override_edge(enabled=True, long_only=True, blocked_hours_utc="")
    decision = _decision(direction=Direction.SHORT, obi_dir=Direction.SHORT)
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=20.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is False
    assert reason == "EDGE_SHORT_BLOCKED"


def test_allows_short_when_long_only_disabled(override_edge):
    """With long_only=False and short gates set permissively, shorts pass."""
    override_edge(enabled=True, long_only=False, blocked_hours_utc="",
                  block_low_conviction=False, short_min_price=0.0,
                  short_min_conviction="LOW")
    decision = _decision(direction=Direction.SHORT, obi_dir=Direction.SHORT)
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=20.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True
    assert reason is None


# ─── targeted short-side gates (post 2026-04-29 re-calibration) ──────────
def test_blocks_cheap_normal_short(override_edge):
    """NORMAL conviction short below short_min_price must be rejected.

    Calibration: 89 trades in $30 bucket lost -$12,893 at 37% WR.
    """
    override_edge(enabled=True, long_only=False, blocked_hours_utc="",
                  short_min_price=40.0, short_min_conviction="HIGH",
                  block_low_conviction=False)
    decision = _decision(
        direction=Direction.SHORT, obi_dir=Direction.SHORT,
        conviction=Conviction.NORMAL,
    )
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=30.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is False
    assert reason == "EDGE_SHORT_PRICE_LOW_30c<40c"


def test_allows_high_conviction_cheap_short(override_edge):
    """HIGH conviction short bypasses the price gate.

    Calibration: 12 HIGH shorts net +$1,799 at 75% WR, including 8 trades
    in the $30 bucket that gained +$1,349 at 75% WR.
    """
    override_edge(enabled=True, long_only=False, blocked_hours_utc="",
                  short_min_price=40.0, short_min_conviction="HIGH",
                  block_low_conviction=False)
    decision = _decision(
        direction=Direction.SHORT, obi_dir=Direction.SHORT,
        roc_dir=Direction.SHORT,
        conviction=Conviction.HIGH,
    )
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=27.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True
    assert reason is None


def test_allows_expensive_normal_short(override_edge):
    """NORMAL short at or above short_min_price is allowed.

    Calibration: shorts at $50+ net +$6,400 across 56 trades at 70%+ WR.
    """
    override_edge(enabled=True, long_only=False, blocked_hours_utc="",
                  short_min_price=40.0, short_min_conviction="HIGH",
                  block_low_conviction=False)
    decision = _decision(
        direction=Direction.SHORT, obi_dir=Direction.SHORT,
        conviction=Conviction.NORMAL,
    )
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=55.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True
    assert reason is None


def test_short_price_gate_boundary_inclusive(override_edge):
    """Price exactly equal to short_min_price is allowed (filter uses strict <)."""
    override_edge(enabled=True, long_only=False, blocked_hours_utc="",
                  short_min_price=40.0, short_min_conviction="HIGH",
                  block_low_conviction=False)
    decision = _decision(
        direction=Direction.SHORT, obi_dir=Direction.SHORT,
        conviction=Conviction.NORMAL,
    )
    allowed, _ = evaluate_edge_profile(
        decision=decision, entry_price=40.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True


def test_short_filter_deferred_to_priced_call(override_edge):
    """Pre-filter call (entry_price=None) must not reject a sub-threshold-
    conviction short — the coordinator's second call with entry_price will
    make the final decision once the price is known."""
    override_edge(enabled=True, long_only=False, blocked_hours_utc="",
                  short_min_price=40.0, short_min_conviction="HIGH",
                  block_low_conviction=False)
    decision = _decision(
        direction=Direction.SHORT, obi_dir=Direction.SHORT,
        conviction=Conviction.NORMAL,
    )
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=None,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True
    assert reason is None


def test_short_min_conviction_normal_lets_normal_through(override_edge):
    """Operator can loosen short_min_conviction to NORMAL to allow all
    NORMAL shorts (only LOW conviction shorts then need price compensation)."""
    override_edge(enabled=True, long_only=False, blocked_hours_utc="",
                  short_min_price=40.0, short_min_conviction="NORMAL",
                  block_low_conviction=False)
    decision = _decision(
        direction=Direction.SHORT, obi_dir=Direction.SHORT,
        conviction=Conviction.NORMAL,
    )
    allowed, _ = evaluate_edge_profile(
        decision=decision, entry_price=20.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True


def test_unknown_short_min_conviction_fails_closed(override_edge):
    """Operator typo on EDGE_LIVE_SHORT_MIN_CONVICTION must NOT silently
    bypass the gate. Sub-threshold shorts must still hit the price check."""
    override_edge(enabled=True, long_only=False, blocked_hours_utc="",
                  short_min_price=40.0, short_min_conviction="WHOOPS",
                  block_low_conviction=False)
    decision = _decision(
        direction=Direction.SHORT, obi_dir=Direction.SHORT,
        conviction=Conviction.HIGH,
    )
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=20.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is False
    assert reason == "EDGE_SHORT_PRICE_LOW_20c<40c"


def test_short_gate_does_not_affect_longs(override_edge):
    """The short-side gate must only fire on Direction.SHORT — longs at
    any price below short_min_price are still subject to the long-side
    max_entry_price cap, never the short min cap."""
    override_edge(enabled=True, long_only=False, blocked_hours_utc="",
                  short_min_price=40.0, short_min_conviction="HIGH",
                  block_low_conviction=False, max_entry_price=80.0)
    decision = _decision(
        direction=Direction.LONG, obi_dir=Direction.LONG,
        conviction=Conviction.NORMAL,
    )
    allowed, _ = evaluate_edge_profile(
        decision=decision, entry_price=20.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True


def test_long_only_overrides_short_gates(override_edge):
    """When long_only=True, short_min_price / short_min_conviction are
    irrelevant — every short is rejected with EDGE_SHORT_BLOCKED."""
    override_edge(enabled=True, long_only=True, blocked_hours_utc="",
                  short_min_price=10.0, short_min_conviction="LOW")
    decision = _decision(
        direction=Direction.SHORT, obi_dir=Direction.SHORT,
        conviction=Conviction.HIGH,
    )
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=99.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is False
    assert reason == "EDGE_SHORT_BLOCKED"


# ─── conviction rule ──────────────────────────────────────────────────────
def test_blocks_low_conviction_by_default(override_edge):
    override_edge(enabled=True, blocked_hours_utc="")
    decision = _decision(conviction=Conviction.LOW, roc_dir=Direction.LONG,
                         obi_dir=Direction.NEUTRAL)
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=20.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is False
    assert reason == "EDGE_LOW_CONVICTION_BLOCKED"


def test_allows_low_conviction_when_disabled(override_edge):
    override_edge(enabled=True, block_low_conviction=False, blocked_hours_utc="")
    decision = _decision(conviction=Conviction.LOW, roc_dir=Direction.LONG,
                         obi_dir=Direction.NEUTRAL)
    allowed, _ = evaluate_edge_profile(
        decision=decision, entry_price=20.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True


# ─── driver allowlist ─────────────────────────────────────────────────────
def test_blocks_obi_tight_driver(override_edge):
    """OBI/TIGHT was net -$329 in paper — must be excluded by default."""
    override_edge(enabled=True, blocked_hours_utc="",
                  allowed_drivers="OBI,OBI+ROC,ROC,ROC/TIGHT")
    decision = _decision(spread_state=SpreadState.TIGHT)  # OBI+TIGHT, ROC neutral
    assert decision.signal_driver == "OBI/TIGHT"
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=20.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is False
    assert reason == "EDGE_DRIVER_BLOCKED_OBI/TIGHT"


def test_allows_roc_tight_driver(override_edge):
    """ROC/TIGHT is not in the live default allowlist as of 2026-04-21
    (9-day counterfactual showed decay to 1d -$618 / 3d -$284 with 22% short WR),
    but when an operator opts it back in via env, the filter must still honor it.
    This test pins that the allowlist machinery works for ROC/TIGHT."""
    override_edge(enabled=True, blocked_hours_utc="",
                  allowed_drivers="OBI,OBI+ROC,ROC,ROC/TIGHT",
                  block_low_conviction=False)
    decision = _decision(
        obi_dir=Direction.NEUTRAL, roc_dir=Direction.LONG,
        spread_state=SpreadState.TIGHT,
    )
    assert decision.signal_driver == "ROC/TIGHT"
    allowed, _ = evaluate_edge_profile(
        decision=decision, entry_price=20.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True


def test_blocks_obi_roc_conflict(override_edge):
    """OBI/ROC (signals disagreeing) is not in the allowed set."""
    override_edge(enabled=True, blocked_hours_utc="",
                  allowed_drivers="OBI,OBI+ROC,ROC,ROC/TIGHT",
                  long_only=False)
    # NB: This decision shape would not normally arise (resolver returns NONE
    # on conflict) — we construct it to verify the driver allowlist.
    decision = _decision(obi_dir=Direction.LONG, roc_dir=Direction.SHORT)
    assert decision.signal_driver == "OBI/ROC"
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=20.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is False
    assert reason == "EDGE_DRIVER_BLOCKED_OBI/ROC"


# ─── hour-of-day rule ─────────────────────────────────────────────────────
@pytest.mark.parametrize("hour,blocked", [
    (0, True), (3, True), (7, True),    # Asia overnight (default blocked)
    (8, False), (14, False), (20, False), (23, False),  # Allowed
])
def test_hour_blocking(override_edge, hour, blocked):
    override_edge(enabled=True, blocked_hours_utc="0,1,2,3,4,5,6,7")
    decision = _decision()
    when = datetime(2026, 4, 20, hour, 30, tzinfo=timezone.utc)
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=20.0, now_utc=when,
    )
    if blocked:
        assert allowed is False
        assert reason == f"EDGE_HOUR_BLOCKED_{hour:02d}UTC"
    else:
        assert allowed is True


def test_empty_hour_list_allows_all_hours(override_edge):
    override_edge(enabled=True, blocked_hours_utc="")
    decision = _decision()
    for hour in range(24):
        when = datetime(2026, 4, 20, hour, tzinfo=timezone.utc)
        allowed, _ = evaluate_edge_profile(
            decision=decision, entry_price=20.0, now_utc=when,
        )
        assert allowed, f"hour {hour} should be allowed when blocked list is empty"


# ─── price cap ────────────────────────────────────────────────────────────
def test_price_cap_blocks_expensive_obi_only(override_edge):
    override_edge(enabled=True, blocked_hours_utc="", max_entry_price=25.0)
    decision = _decision()  # OBI only, no agreement
    assert decision.signal_driver == "OBI"
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=42.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is False
    assert reason == "EDGE_PRICE_CAP_42c>25c"


def test_price_cap_allows_cheap_obi_only(override_edge):
    override_edge(enabled=True, blocked_hours_utc="", max_entry_price=25.0)
    decision = _decision()
    allowed, _ = evaluate_edge_profile(
        decision=decision, entry_price=18.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True


def test_price_cap_boundary_inclusive(override_edge):
    """Price exactly equal to max should be allowed (filter uses strict >)."""
    override_edge(enabled=True, blocked_hours_utc="", max_entry_price=25.0)
    decision = _decision()
    allowed, _ = evaluate_edge_profile(
        decision=decision, entry_price=25.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True


def test_obi_roc_agreement_bypasses_price_cap(override_edge):
    """OBI+ROC agreement was 88.9% WR — the cheapest setups don't matter,
    we let the higher-conviction setups through at any price."""
    override_edge(enabled=True, blocked_hours_utc="", max_entry_price=25.0,
                  agreement_overrides_price_cap=True)
    decision = _decision(obi_dir=Direction.LONG, roc_dir=Direction.LONG)
    assert decision.signal_driver == "OBI+ROC"
    allowed, _ = evaluate_edge_profile(
        decision=decision, entry_price=55.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True


def test_agreement_bypass_can_be_disabled(override_edge):
    """Operator can require ALL trades to respect the price cap by setting
    EDGE_LIVE_AGREEMENT_OVERRIDES_PRICE_CAP=false."""
    override_edge(enabled=True, blocked_hours_utc="", max_entry_price=25.0,
                  agreement_overrides_price_cap=False)
    decision = _decision(obi_dir=Direction.LONG, roc_dir=Direction.LONG)
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=55.0,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is False
    assert reason == "EDGE_PRICE_CAP_55c>25c"


def test_unknown_entry_price_skips_cap(override_edge):
    """Pre-filter call (entry_price=None) only checks direction/conviction/
    driver/hour. Price cap is deferred until actual entry price is known."""
    override_edge(enabled=True, blocked_hours_utc="", max_entry_price=25.0)
    decision = _decision()
    allowed, _ = evaluate_edge_profile(
        decision=decision, entry_price=None,
        now_utc=datetime(2026, 4, 20, 14, tzinfo=timezone.utc),
    )
    assert allowed is True


# ─── precedence: short blocked before driver/price/hour ───────────────────
def test_short_blocked_before_other_checks(override_edge):
    """Short rejection has highest precedence — first signal of mismatch."""
    override_edge(enabled=True, long_only=True, blocked_hours_utc="0",
                  max_entry_price=25.0)
    decision = _decision(
        direction=Direction.SHORT, obi_dir=Direction.SHORT,
        conviction=Conviction.LOW,
    )
    allowed, reason = evaluate_edge_profile(
        decision=decision, entry_price=99.0,
        now_utc=datetime(2026, 4, 20, 0, tzinfo=timezone.utc),  # blocked hour
    )
    assert allowed is False
    assert reason == "EDGE_SHORT_BLOCKED"


# ─── default config values ────────────────────────────────────────────────
def test_defaults_off_until_explicitly_enabled():
    """The profile must ship disabled. Operator opts in via env var.

    Defaults updated 2026-04-29 after re-calibration on 14-day paper window:
      * long_only flipped False (HIGH shorts and $50+ shorts profitable)
      * blocked_hours_utc cleared (all-hour long edge confirmed)
      * short_min_price/short_min_conviction added as targeted gates
    """
    from config.settings import EdgeProfileConfig
    cfg = EdgeProfileConfig()
    assert cfg.enabled is False, (
        "EDGE_LIVE_PROFILE_ENABLED must default to False so a deploy "
        "doesn't silently restrict live trading."
    )
    assert cfg.long_only is False, (
        "long_only flipped to False on 2026-04-29 — 14-day paper data "
        "showed +$1,799 from HIGH-conviction shorts (75% WR) and "
        "+$6,400 from $50+ shorts (70%+ WR). Re-set true only after a "
        "fresh walk-forward shows shorts have lost edge entirely."
    )
    assert cfg.short_min_price == 40.0, (
        "short_min_price guards the toxic NORMAL/cheap short cohort that "
        "lost -$12,893 in the $30 bucket alone. Loosen with caution."
    )
    assert cfg.short_min_conviction == "HIGH"
    assert cfg.block_low_conviction is True
    assert cfg.max_entry_price == 25.0
    assert cfg.agreement_overrides_price_cap is True
    assert "OBI" in cfg.allowed_drivers_set
    assert "OBI+ROC" in cfg.allowed_drivers_set
    assert "ROC" in cfg.allowed_drivers_set
    assert "ROC/TIGHT" not in cfg.allowed_drivers_set, (
        "ROC/TIGHT was removed 2026-04-21 after 9-day paper counterfactual "
        "showed 1d -$618, 3d -$284, 7d +$61 with shorts at 22% WR. "
        "Must NOT be in the default allowlist until walk-forward re-validation."
    )
    assert "OBI/TIGHT" not in cfg.allowed_drivers_set, (
        "OBI/TIGHT was net -$329 in paper attribution — must NOT be in "
        "the default allowlist."
    )
    assert cfg.blocked_hours_set == set(), (
        "blocked_hours_utc cleared 2026-04-29 — 14-day window showed "
        "hours 03/05/06 alone produced +$28,513 of long PnL. Re-add "
        "specific hours only with fresh attribution evidence."
    )


def test_invalid_hour_strings_are_dropped():
    """Operator typos shouldn't crash startup."""
    from config.settings import EdgeProfileConfig
    cfg = dataclasses.replace(
        EdgeProfileConfig(), blocked_hours_utc="0,bogus,3,99,7,",
    )
    assert cfg.blocked_hours_set == {0, 3, 7}  # 99 out of range, 'bogus' not int
