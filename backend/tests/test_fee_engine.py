"""Unit tests for FeeEngine (REQ-2)."""
from risk.fee_engine import FeeEngine


def test_fee_parabolic_shape():
    """Fee is zero at 0¢ and 100¢, peaks at 50¢."""
    engine = FeeEngine()
    fee_0 = engine.compute_fee(price_cents=0, contracts=10, order_type="taker")
    fee_100 = engine.compute_fee(price_cents=100, contracts=10, order_type="taker")
    fee_50 = engine.compute_fee(price_cents=50, contracts=10, order_type="taker")
    assert fee_0 == 0.0
    assert fee_100 == 0.0
    assert fee_50 > 0


def test_maker_cheaper_than_taker():
    """Maker fee is strictly less than taker fee at the same price."""
    engine = FeeEngine()
    maker = engine.compute_fee(price_cents=50, contracts=10, order_type="maker")
    taker = engine.compute_fee(price_cents=50, contracts=10, order_type="taker")
    assert maker < taker


def test_fee_formula_taker_at_50():
    """Verify exact formula: fee = p * (1-p) * rate * contracts."""
    engine = FeeEngine()
    fee = engine.compute_fee(price_cents=50, contracts=10, order_type="taker")
    expected = 0.50 * 0.50 * 0.07 * 10
    assert abs(fee - expected) < 1e-6


def test_fee_formula_maker_at_50():
    """Verify exact formula for maker at 50¢."""
    engine = FeeEngine()
    fee = engine.compute_fee(price_cents=50, contracts=10, order_type="maker")
    expected = 0.50 * 0.50 * 0.03 * 10
    assert abs(fee - expected) < 1e-6


def test_round_trip_fee():
    """Round-trip fee sums entry and exit fees."""
    engine = FeeEngine()
    rt = engine.compute_round_trip_fee(
        entry_price_cents=45, exit_price_cents=55,
        contracts=10, entry_type="maker", exit_type="taker",
    )
    entry_fee = engine.compute_fee(45, 10, "maker")
    exit_fee = engine.compute_fee(55, 10, "taker")
    assert abs(rt - (entry_fee + exit_fee)) < 1e-6


def test_record_and_total():
    """record_fill() accumulates and total_fees_paid() reports total."""
    engine = FeeEngine()
    engine.record_fill(price_cents=50, contracts=5, order_type="taker", leg="entry")
    engine.record_fill(price_cents=55, contracts=5, order_type="taker", leg="exit")
    total = engine.total_fees_paid()
    assert total > 0
    engine.reset()
    assert engine.total_fees_paid() == 0


def test_fee_at_price_static():
    """Static helper returns fee per contract."""
    fee = FeeEngine.fee_at_price(50, "taker")
    expected = 0.50 * 0.50 * 0.07
    assert abs(fee - expected) < 1e-6
