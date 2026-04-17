"""
Signal Conflict Resolver — the brain between individual signals and the order manager.
Per the signal-conflict-resolver skill.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from strategies.obi import Direction


class Conviction(str, Enum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"
    NONE = "NONE"

    @staticmethod
    def downgrade(level: "Conviction") -> "Conviction":
        _ladder = {
            Conviction.HIGH: Conviction.NORMAL,
            Conviction.NORMAL: Conviction.LOW,
            Conviction.LOW: Conviction.NONE,
            Conviction.NONE: Conviction.NONE,
        }
        return _ladder[level]


COORDINATION_TABLE: dict[tuple[Direction, Direction], tuple[Optional[Direction], Conviction]] = {
    (Direction.LONG, Direction.LONG): (Direction.LONG, Conviction.HIGH),
    (Direction.SHORT, Direction.SHORT): (Direction.SHORT, Conviction.HIGH),
    (Direction.LONG, Direction.NEUTRAL): (Direction.LONG, Conviction.NORMAL),
    (Direction.SHORT, Direction.NEUTRAL): (Direction.SHORT, Conviction.NORMAL),
    (Direction.NEUTRAL, Direction.LONG): (Direction.LONG, Conviction.LOW),
    (Direction.NEUTRAL, Direction.SHORT): (Direction.SHORT, Conviction.LOW),
    (Direction.LONG, Direction.SHORT): (None, Conviction.NONE),
    (Direction.SHORT, Direction.LONG): (None, Conviction.NONE),
    (Direction.NEUTRAL, Direction.NEUTRAL): (None, Conviction.NONE),
}

CONVICTION_SIZE_MAP = {
    Conviction.HIGH: 1.30,
    Conviction.NORMAL: 1.00,
    Conviction.LOW: 0.65,
    Conviction.NONE: 0.00,
}


@dataclass
class TradeDecision:
    direction: Optional[Direction]
    conviction: Conviction
    obi_dir: Direction
    roc_dir: Direction
    skip_reason: Optional[str] = None

    @property
    def size_multiplier(self) -> float:
        return CONVICTION_SIZE_MAP[self.conviction]

    @property
    def should_trade(self) -> bool:
        return (
            self.direction is not None
            and self.conviction not in (Conviction.NONE, Conviction.LOW)
        )

    def with_conviction(self, new_conviction: Conviction, skip_reason: Optional[str] = None) -> "TradeDecision":
        """Return a copy with conviction changed. Clears direction if NONE."""
        new_dir = self.direction if new_conviction != Conviction.NONE else None
        return TradeDecision(
            direction=new_dir,
            conviction=new_conviction,
            obi_dir=self.obi_dir,
            roc_dir=self.roc_dir,
            skip_reason=skip_reason or self.skip_reason,
        )


class SignalConflictResolver:
    def resolve(
        self,
        obi_direction: Direction,
        roc_direction: Direction,
        atr_regime: str,
        can_trade: bool,
    ) -> TradeDecision:
        if not can_trade:
            return TradeDecision(
                direction=None,
                conviction=Conviction.NONE,
                obi_dir=obi_direction,
                roc_dir=roc_direction,
                skip_reason="CIRCUIT_BREAKER_ACTIVE",
            )

        if atr_regime == "HIGH":
            return TradeDecision(
                direction=None,
                conviction=Conviction.NONE,
                obi_dir=obi_direction,
                roc_dir=roc_direction,
                skip_reason="ATR_REGIME_HIGH",
            )

        key = (obi_direction, roc_direction)
        trade_dir, conviction = COORDINATION_TABLE.get(key, (None, Conviction.NONE))

        skip_reason = None
        if conviction == Conviction.NONE:
            if (
                obi_direction != Direction.NEUTRAL
                and roc_direction != Direction.NEUTRAL
                and obi_direction != roc_direction
            ):
                skip_reason = "SIGNAL_CONFLICT"
            else:
                skip_reason = "NO_SIGNAL"

        return TradeDecision(
            direction=trade_dir,
            conviction=conviction,
            obi_dir=obi_direction,
            roc_dir=roc_direction,
            skip_reason=skip_reason,
        )
