"""
Signal Conflict Resolver — the brain between individual signals and the order manager.
Per the signal-conflict-resolver skill.

v2: adds SpreadDivergence post-resolver conviction modifier.
The coordination table (OBI x ROC) is unchanged. SpreadState is applied
after table lookup as a graduated confidence adjustment.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from strategies.obi import Direction
from strategies.spread_div import SpreadState


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

    @staticmethod
    def upgrade(level: "Conviction") -> "Conviction":
        """Upgrade conviction one step, capped at NORMAL.

        NONE is never upgraded — spread tightness cannot manufacture a trade
        where no signal exists.  NORMAL is the ceiling — upgrading to HIGH
        caused oversized positions on single-signal (OBI-only) trades that
        were net negative in production paper trading.
        """
        _ladder = {
            Conviction.HIGH: Conviction.HIGH,
            Conviction.NORMAL: Conviction.NORMAL,
            Conviction.LOW: Conviction.NORMAL,
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


def describe_signal_driver(
    obi_dir: Direction,
    roc_dir: Direction,
    spread_state: SpreadState = SpreadState.NORMAL,
) -> str:
    """Produce a short, human-readable label of which signals drove the trade.

    The string is the attribution tag stored alongside the trade so we can
    later aggregate PnL by driver. Does NOT affect any trading logic.

    Format:
      - 'OBI+ROC'          both primary signals agreed
      - 'OBI'              OBI fired, ROC neutral
      - 'ROC'              ROC fired, OBI neutral
      - '-'                neither signal fired
      - suffix '/TIGHT'    current spread was anomalously tight  (upgrade)
      - suffix '/WIDE'     current spread was anomalously wide   (downgrade)
    """
    obi_has = obi_dir != Direction.NEUTRAL
    roc_has = roc_dir != Direction.NEUTRAL
    if obi_has and roc_has:
        base = "OBI+ROC" if obi_dir == roc_dir else "OBI/ROC"
    elif obi_has:
        base = "OBI"
    elif roc_has:
        base = "ROC"
    else:
        base = "-"
    if spread_state == SpreadState.TIGHT:
        return f"{base}/TIGHT"
    if spread_state == SpreadState.WIDE:
        return f"{base}/WIDE"
    return base


@dataclass
class TradeDecision:
    direction: Optional[Direction]
    conviction: Conviction
    obi_dir: Direction
    roc_dir: Direction
    spread_state: SpreadState = SpreadState.NORMAL
    skip_reason: Optional[str] = None

    @property
    def size_multiplier(self) -> float:
        return CONVICTION_SIZE_MAP[self.conviction]

    @property
    def signal_driver(self) -> str:
        return describe_signal_driver(self.obi_dir, self.roc_dir, self.spread_state)

    @property
    def should_trade(self) -> bool:
        """Default accessor — treats the caller as the paper lane.

        Preserved for backwards compatibility with existing tests and
        dashboard serialization. Coordinator callers should prefer
        should_trade_in(mode) to distinguish paper vs live lanes.
        """
        return self.should_trade_in("paper")

    def should_trade_in(self, mode: str) -> bool:
        """Lane-aware gate. LOW conviction is only tradeable when the
        corresponding per-lane ROC activation flag is set.
        """
        if self.direction is None or self.conviction == Conviction.NONE:
            return False
        if self.conviction == Conviction.LOW:
            from config import settings
            if mode == "live":
                return settings.bot.roc_low_conviction_live_enabled
            return settings.bot.roc_low_conviction_paper_enabled
        return True

    def with_conviction(self, new_conviction: Conviction, skip_reason: Optional[str] = None) -> "TradeDecision":
        """Return a copy with conviction changed. Clears direction if NONE."""
        new_dir = self.direction if new_conviction != Conviction.NONE else None
        return TradeDecision(
            direction=new_dir,
            conviction=new_conviction,
            obi_dir=self.obi_dir,
            roc_dir=self.roc_dir,
            spread_state=self.spread_state,
            skip_reason=skip_reason or self.skip_reason,
        )


class SignalConflictResolver:
    def resolve(
        self,
        obi_direction: Direction,
        roc_direction: Direction,
        atr_regime: str,
        can_trade: bool,
        spread_state: SpreadState = SpreadState.NORMAL,
    ) -> TradeDecision:
        """Single entry point for all signal resolution.

        spread_state is applied AFTER the coordination table lookup:
          WIDE  -> downgrade conviction one step
          TIGHT -> upgrade conviction one step (NONE is never upgraded)
        """
        if not can_trade:
            return TradeDecision(
                direction=None,
                conviction=Conviction.NONE,
                obi_dir=obi_direction,
                roc_dir=roc_direction,
                spread_state=spread_state,
                skip_reason="CIRCUIT_BREAKER_ACTIVE",
            )

        if atr_regime == "HIGH":
            return TradeDecision(
                direction=None,
                conviction=Conviction.NONE,
                obi_dir=obi_direction,
                roc_dir=roc_direction,
                spread_state=spread_state,
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

        # Spread modifier — MEDIUM regime only, requires live signal and SD enabled
        from config import settings
        sd_cfg = settings.spread_div
        if sd_cfg.enabled and atr_regime == "MEDIUM" and conviction != Conviction.NONE:
            if spread_state == SpreadState.WIDE:
                conviction = Conviction.downgrade(conviction)
                if conviction == Conviction.NONE:
                    skip_reason = "SPREAD_WIDE_DOWNGRADE"
                    trade_dir = None
            elif spread_state == SpreadState.TIGHT:
                conviction = Conviction.upgrade(conviction)

        return TradeDecision(
            direction=trade_dir,
            conviction=conviction,
            obi_dir=obi_direction,
            roc_dir=roc_direction,
            spread_state=spread_state,
            skip_reason=skip_reason,
        )
