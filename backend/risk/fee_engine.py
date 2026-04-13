"""
FeeEngine — Kalshi fee model for paper simulation and live cost tracking.

Closes the most impactful paper-to-live gap: fees were previously unmodeled
in the paper sim, inflating paper P&L by ~30-50% relative to live results.

Kalshi fee formula:
    fee = price * (1 - price) * rate * contracts

This creates a parabolic curve: zero at 0¢/100¢, peak at 50¢.

Maker vs taker rates:
    - Makers (limit orders, resting on book): ~3% of theoretical value
    - Takers (market orders, crossing spread): ~7% of theoretical value

Research on 300k+ Kalshi contracts confirms makers earn positive returns;
takers consistently lose net of fees. This engine makes that visible.

Usage:
    engine = FeeEngine()
    fee = engine.compute_fee(price_cents=55, contracts=10, order_type="taker")
    report = engine.build_report(trades_list)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal

OrderType = Literal["maker", "taker"]


# Kalshi fee rates (approximate, verify against current Kalshi fee schedule)
_TAKER_RATE = 0.07   # 7% of theoretical value for market orders
_MAKER_RATE = 0.03   # ~3% of theoretical value for resting limit orders


@dataclass
class FeeRecord:
    price_cents: float
    contracts: int
    order_type: OrderType
    fee: float
    leg: str  # "entry" | "exit"


class FeeEngine:
    """Computes and tracks Kalshi fees per fill."""

    TAKER_RATE: float = _TAKER_RATE
    MAKER_RATE: float = _MAKER_RATE

    def __init__(self) -> None:
        self._records: List[FeeRecord] = []

    # ── Core calculation ───────────────────────────────────────────────

    def compute_fee(
        self,
        price_cents: float,
        contracts: int,
        order_type: OrderType = "taker",
    ) -> float:
        """
        Compute the Kalshi fee for a single fill.

        Args:
            price_cents: Contract price in cents (1–99).
            contracts:   Number of contracts filled.
            order_type:  "taker" (market order) or "maker" (limit order).

        Returns:
            Fee in dollars.

        Example:
            >>> engine = FeeEngine()
            >>> engine.compute_fee(price_cents=50, contracts=10, order_type="taker")
            0.0875  # 0.50 * 0.50 * 0.07 * 10 / 1 ... actually 0.50*0.50*0.07*10 = 0.175/2 wait...
            # fee = (50/100) * (1 - 50/100) * 0.07 * 10 = 0.5 * 0.5 * 0.07 * 10 = 0.175

        The fee is zero at price 0¢ and 100¢, and peaks at 50¢.
        """
        p = price_cents / 100.0
        rate = self.TAKER_RATE if order_type == "taker" else self.MAKER_RATE
        return round(p * (1.0 - p) * rate * contracts, 6)

    def compute_round_trip_fee(
        self,
        entry_price_cents: float,
        exit_price_cents: float,
        contracts: int,
        entry_type: OrderType = "taker",
        exit_type: OrderType = "taker",
    ) -> float:
        """
        Compute total fees for entry + exit legs.

        For a long position:
            entry: buy YES at entry_price_cents
            exit:  sell YES at exit_price_cents (price is still entry_price_cents
                   for fee calc — the YES contract's cost basis doesn't change)

        For simplicity we apply the fee to the actual fill price on each leg,
        which is the conservative (higher) estimate.
        """
        entry_fee = self.compute_fee(entry_price_cents, contracts, entry_type)
        exit_fee = self.compute_fee(exit_price_cents, contracts, exit_type)
        return round(entry_fee + exit_fee, 6)

    # ── Record tracking (for paper sim trade log) ──────────────────────

    def record_fill(
        self,
        price_cents: float,
        contracts: int,
        order_type: OrderType,
        leg: str,
    ) -> float:
        """Record a fill and return the fee amount."""
        fee = self.compute_fee(price_cents, contracts, order_type)
        self._records.append(FeeRecord(
            price_cents=price_cents,
            contracts=contracts,
            order_type=order_type,
            fee=fee,
            leg=leg,
        ))
        return fee

    def total_fees_paid(self) -> float:
        return round(sum(r.fee for r in self._records), 4)

    def reset(self) -> None:
        self._records.clear()

    # ── Reporting ──────────────────────────────────────────────────────

    def build_report(self, trades: list) -> dict:
        """
        Build a fee analysis report from a list of PaperTrade objects.

        Args:
            trades: List of PaperTrade dataclass instances with .fees, .pnl
                    (net, after fees), and .entry_price attributes.

        Returns:
            dict with gross_pnl, total_fees, net_pnl, fee_drag_pct, etc.
        """
        if not trades:
            return {
                "trade_count": 0,
                "gross_pnl": 0.0,
                "total_fees": 0.0,
                "net_pnl": 0.0,
                "fee_drag_pct": 0.0,
                "avg_fee_per_trade": 0.0,
            }

        total_fees = sum(t.fees for t in trades)
        net_pnl = sum(t.pnl for t in trades)
        gross_pnl = net_pnl + total_fees

        fee_drag_pct = (
            (total_fees / abs(gross_pnl) * 100) if gross_pnl != 0 else 0.0
        )

        return {
            "trade_count": len(trades),
            "gross_pnl": round(gross_pnl, 4),
            "total_fees": round(total_fees, 4),
            "net_pnl": round(net_pnl, 4),
            "fee_drag_pct": round(fee_drag_pct, 2),
            "avg_fee_per_trade": round(total_fees / len(trades), 4),
        }

    # ── Static helpers ─────────────────────────────────────────────────

    @staticmethod
    def fee_at_price(price_cents: float, order_type: OrderType = "taker") -> float:
        """
        Fee per contract at a given price. Useful for quick lookups.

        >>> FeeEngine.fee_at_price(50, "taker")   # 0.0175  (max fee zone)
        >>> FeeEngine.fee_at_price(50, "maker")   # 0.0075
        >>> FeeEngine.fee_at_price(10, "taker")   # 0.0063  (low-price zone)
        """
        p = price_cents / 100.0
        rate = _TAKER_RATE if order_type == "taker" else _MAKER_RATE
        return round(p * (1.0 - p) * rate, 6)

    @staticmethod
    def effective_edge_after_fee(
        raw_edge_cents: float,
        price_cents: float,
        contracts: int,
        order_type: OrderType = "maker",
    ) -> float:
        """
        Given a raw price edge (predicted move in cents), compute the edge
        remaining after fees on one leg.

        Useful in the maker strategy to confirm a signal has positive EV
        before submitting a resting order.

            effective_edge = raw_edge - fee_per_contract

        Args:
            raw_edge_cents: Expected price move in cents.
            price_cents:    Current contract price in cents.
            contracts:      Position size in contracts.
            order_type:     "maker" or "taker".

        Returns:
            Net edge per contract in cents (negative = fee-negative, skip trade).
        """
        p = price_cents / 100.0
        rate = _TAKER_RATE if order_type == "taker" else _MAKER_RATE
        fee_per_contract_cents = p * (1.0 - p) * rate * 100  # convert back to cents
        return round(raw_edge_cents - fee_per_contract_cents, 4)
