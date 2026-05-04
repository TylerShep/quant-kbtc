"""Unit tests for PaperTrader."""
from execution.paper_trader import PaperTrader
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
