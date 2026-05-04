"""Unit tests for PaperTrader."""
from execution.paper_trader import PaperTrade, PaperTrader
from risk.position_sizer import PositionSizer
from risk.fee_engine import FeeEngine


def test_enter_creates_position():
    sizer = PositionSizer(50_000.0)
    trader = PaperTrader(sizer)
    pos = trader.enter(
        ticker="KXBTC-TEST",
        direction="long",
        price=50.0,
        conviction="HIGH",
        regime="MEDIUM",
    )
    assert pos is not None
    assert trader.has_position is True
    assert trader.position.ticker == "KXBTC-TEST"
    assert trader.position.direction == "long"
    assert trader.position.contracts >= 1


def test_exit_calculates_pnl_correctly():
    sizer = PositionSizer(50_000.0)
    trader = PaperTrader(sizer)
    trader.enter(ticker="T", direction="long", price=50.0, conviction="HIGH", regime="MEDIUM")
    contracts = trader.position.contracts
    exit_price = 55.0
    trade = trader.exit(exit_price, reason="TEST")
    assert trade is not None
    gross = contracts * (exit_price - 50.0) / 100.0
    fe = FeeEngine()
    expected_fees = fe.compute_round_trip_fee(
        entry_price_cents=50.0, exit_price_cents=exit_price,
        contracts=contracts, entry_type="taker", exit_type="taker",
    )
    assert abs(trade.fees - round(expected_fees, 4)) < 1e-4
    assert abs(trade.pnl - round(gross - expected_fees, 4)) < 1e-3
    assert trader.has_position is False


def test_fee_calculation_uses_fee_engine():
    sizer = PositionSizer(50_000.0)
    trader = PaperTrader(sizer)
    trader.enter(ticker="T", direction="long", price=40.0, conviction="HIGH", regime="MEDIUM")
    contracts = trader.position.contracts
    trade = trader.exit(40.0, reason="FLAT")
    fe = FeeEngine()
    expected_fees = fe.compute_round_trip_fee(
        entry_price_cents=40.0, exit_price_cents=40.0,
        contracts=contracts, entry_type="taker", exit_type="taker",
    )
    assert trade.pnl == round(-expected_fees, 4)


def test_cannot_enter_when_already_in_position():
    sizer = PositionSizer(50_000.0)
    trader = PaperTrader(sizer)
    first = trader.enter(ticker="T", direction="long", price=50.0, conviction="NORMAL", regime="MEDIUM")
    second = trader.enter(ticker="T2", direction="short", price=50.0, conviction="NORMAL", regime="MEDIUM")
    assert first is not None
    assert second is None
    assert trader.position.ticker == "T"


def test_paper_trade_preserves_entry_obi_and_roc_bug030():
    """BUG-030 regression: PaperTrade was missing entry_obi/entry_roc
    fields entirely, so coordinator's getattr(trade, "entry_obi", 0.0)
    always wrote 0 to the trades table. The 14-day paper window had
    532 trades with all-zeros in both columns, breaking attribution
    that joined on trades for these features (e.g. the new
    edge_profile_review's ROC bucketing). trade_features.roc_5 stays
    the source of truth for ML; this test pins that the trades table
    row also carries the entry-time signal values."""
    sizer = PositionSizer(50_000.0)
    trader = PaperTrader(sizer)
    pos = trader.enter(
        ticker="KXBTC-TEST", direction="short", price=50.0,
        conviction="NORMAL", regime="LOW", obi=0.42, roc=-0.073,
    )
    assert pos is not None
    assert pos.entry_obi == 0.42
    assert pos.entry_roc == -0.073

    trade = trader.exit(60.0, reason="TEST")
    assert trade is not None
    assert trade.entry_obi == 0.42, (
        "PaperTrade.entry_obi must equal PaperPosition.entry_obi or the "
        "trades.entry_obi column will silently store zero (BUG-030)."
    )
    assert trade.entry_roc == -0.073, (
        "PaperTrade.entry_roc must equal PaperPosition.entry_roc or the "
        "trades.entry_roc column will silently store zero (BUG-030)."
    )


# ════════════════════════════════════════════════════════════════════════
# Phase 1 (Expiry Exit Reliability): paper guard exits must stamp a
# `fill_source` so analytics can separate realistic taker fills from
# legacy synthetic mid-price fills. Default behavior (no fill_source
# passed) must remain identical to pre-Phase 1 to avoid invalidating
# existing paper trades.
# ════════════════════════════════════════════════════════════════════════


def _enter_long(trader: PaperTrader, price: float = 50.0) -> None:
    trader.enter(
        ticker="KXBTC-T",
        direction="long",
        price=price,
        conviction="HIGH",
        regime="MEDIUM",
    )


def test_exit_default_fill_source_is_none_for_backward_compat():
    """Non-guard paper exits do not pass `fill_source`. The default must
    remain None so existing call sites and snapshot tests don't change
    behavior. Coordinator persistence layer maps None -> "paper_mid_mark"
    when writing to the trades table."""
    sizer = PositionSizer(50_000.0)
    trader = PaperTrader(sizer)
    _enter_long(trader)
    trade = trader.exit(55.0, reason="STOP_LOSS")
    assert trade is not None
    assert trade.fill_source is None


def test_exit_records_explicit_fill_source_on_guard_exit():
    """Realistic paper guard exits MUST be labeled so analytics can
    distinguish them from the legacy synthetic mid-price baseline.
    Without this label there is no post-rollout way to confirm the
    fix shifted paper EXPIRY_GUARD outcomes."""
    sizer = PositionSizer(50_000.0)
    trader = PaperTrader(sizer)
    _enter_long(trader)
    trade = trader.exit(
        47.0,
        reason="EXPIRY_GUARD",
        fill_source="paper_guard_taker_bidask",
    )
    assert trade is not None
    assert trade.fill_source == "paper_guard_taker_bidask"
    assert trade.exit_price == 47.0
    assert isinstance(trade, PaperTrade)


def test_exit_no_position_returns_none_with_explicit_fill_source():
    """Calling exit() with no open position must remain a safe no-op
    even when a fill_source is provided -- the new kwarg must NOT
    accidentally transform the no-op path into something that touches
    sizer state."""
    sizer = PositionSizer(50_000.0)
    trader = PaperTrader(sizer)
    initial_bankroll = sizer.bankroll
    trade = trader.exit(99.0, reason="EXPIRY_GUARD",
                        fill_source="paper_guard_taker_bidask")
    assert trade is None
    assert sizer.bankroll == initial_bankroll
