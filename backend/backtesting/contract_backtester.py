"""
Contract-price backtesting engine with production guard parity.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional

from backtesting.contract_timeline import ContractTick, ContractTimeline
from backtesting.metrics import compute_metrics
from config import settings
from filters.atr_regime import ATRRegimeFilter
from filters.edge_profile import evaluate as evaluate_edge_profile
from filters.price_guard import PriceGuard
from filters.spread_regime import SpreadRegimeFilter
from filters.trend_guard import TrendGuard
from ml.inference import ml_gate
from risk.circuit_breaker import CircuitBreaker
from risk.fee_engine import FeeEngine
from risk.position_sizer import PositionSizer
from strategies.exit_intelligence import compute_position_health_score
from strategies.obi import Direction, check_obi_exit, evaluate_obi
from strategies.roc import calculate_roc, check_roc_exit, evaluate_roc
from strategies.resolver import Conviction, SignalConflictResolver
from strategies.spread_div import evaluate_spread_divergence

try:
    from zoneinfo import ZoneInfo

    _ET_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET_TZ = timezone(timedelta(hours=-5))

_TICKER_CLOSE_RE = re.compile(r"^KX[A-Z]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})-[BT].+$")
_MONTH_MAP = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


@dataclass
class SimPosition:
    position_uid: str
    ticker: str
    direction: str
    contracts: int
    entry_price: float
    entry_timestamp: float
    conviction: str
    regime_at_entry: str
    entry_obi: float
    entry_roc: float
    signal_driver: str
    candles_held: int = 0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0


class ContractBacktester:
    def __init__(
        self,
        candles: list[dict],
        contract_timelines: dict[str, ContractTimeline],
        config: Optional[dict] = None,
        settlement_data: Optional[dict] = None,
        tfi_history: Optional[dict] = None,
        sim_clock=None,
    ):
        self.candles = sorted(candles, key=lambda c: c["timestamp"])
        self.contract_timelines = contract_timelines or {}
        self.config = config or {}
        self.settlement_data = settlement_data or {}
        self.tfi_history = tfi_history or {}
        self.trades: list[dict] = []
        self.equity_curve: list[float] = []
        self.signal_log: list[dict] = []

        self.mode = str(self.config.get("mode", "paper")).lower()
        self._sim_now = (
            self.candles[0]["timestamp"]
            if self.candles
            else 0.0
        )
        self._external_clock = sim_clock
        forced_entry = self.config.get("forced_entry")
        self._forced_entry = (
            dict(forced_entry)
            if isinstance(forced_entry, dict)
            else None
        )
        self._forced_entry_opened = False
        self._disable_signal_entries = bool(
            self.config.get("disable_signal_entries", False)
        )
        self._ml_gate_mode = str(self.config.get("ml_gate_mode", "config")).lower()
        if self._ml_gate_mode not in {"config", "disabled"}:
            self._ml_gate_mode = "config"
        self._exit_fill_mode = str(self.config.get("exit_fill_mode", "mark")).lower()
        if self._exit_fill_mode not in {"mark", "executable"}:
            self._exit_fill_mode = "mark"

        self._position_seq = 0
        self._last_paper_exit_wall_ts = 0.0
        self._last_paper_exit_per_pair: dict[tuple[str, str], float] = {}
        self._health_breach_count = 0

        self._price_guard = PriceGuard()
        self._trend_guard = TrendGuard()
        self._resolver = SignalConflictResolver()
        self._fee_engine = FeeEngine()

    def _now(self) -> float:
        if self._external_clock is not None:
            return float(self._external_clock())
        return self._sim_now

    @staticmethod
    def _entry_price_from_tick(tick: ContractTick, direction: Direction) -> Optional[float]:
        if direction == Direction.LONG:
            return tick.best_ask if tick.best_ask is not None else tick.mid_cents
        return tick.best_bid if tick.best_bid is not None else tick.mid_cents

    @staticmethod
    def _mark_exit_price_from_tick(pos: SimPosition, tick: ContractTick) -> Optional[float]:
        if tick.mid_cents is not None:
            return tick.mid_cents
        if pos.direction == "long":
            return tick.best_bid
        return tick.best_ask

    @staticmethod
    def _executable_exit_price_from_tick(pos: SimPosition, tick: ContractTick) -> Optional[float]:
        # Mirrors coordinator._get_executable_exit_price_for.
        if pos.direction == "long":
            return tick.best_bid
        if tick.best_ask is None:
            return None
        return 100.0 - tick.best_ask

    def _exit_price_from_tick(self, pos: SimPosition, tick: ContractTick) -> Optional[float]:
        if self._exit_fill_mode == "executable":
            executable = self._executable_exit_price_from_tick(pos, tick)
            if executable is not None:
                return executable
        return self._mark_exit_price_from_tick(pos, tick)

    @staticmethod
    def _opposite_direction(direction: str) -> Optional[str]:
        d = (direction or "").strip().lower()
        if d == "long":
            return "short"
        if d == "short":
            return "long"
        return None

    def _paper_same_thesis_gate(
        self,
        ticker: str,
        direction: str,
        now_ts: float,
        cooldown_sec: float,
    ) -> bool:
        if cooldown_sec <= 0 or not ticker:
            return True

        if bool(self.config.get("paper_thesis_flip_unlock_enabled", settings.bot.paper_thesis_flip_unlock_enabled)):
            opposite = self._opposite_direction(direction)
            if opposite is not None:
                self._last_paper_exit_per_pair.pop((ticker, opposite), None)

        key = (ticker, direction)
        last_exit = self._last_paper_exit_per_pair.get(key)
        if last_exit is None:
            return True
        if (now_ts - last_exit) >= cooldown_sec:
            self._last_paper_exit_per_pair.pop(key, None)
            return True
        return False

    def _health_exit_confirmation_met(
        self,
        position: SimPosition,
        current_obi: float,
        current_roc: Optional[float],
    ) -> bool:
        enabled = bool(
            self.config.get(
                "health_exit_confirmation_enabled",
                settings.bot.health_exit_confirmation_enabled,
            )
        )
        if not enabled:
            return True

        roc_delta = max(
            0.0,
            float(
                self.config.get(
                    "health_exit_confirmation_roc_delta",
                    settings.bot.health_exit_confirmation_roc_delta,
                )
            ),
        )
        obi_delta = max(
            0.0,
            float(
                self.config.get(
                    "health_exit_confirmation_obi_delta",
                    settings.bot.health_exit_confirmation_obi_delta,
                )
            ),
        )
        neutral_obi = max(
            0.0,
            min(
                1.0,
                float(
                    self.config.get(
                        "health_exit_confirmation_neutral_obi",
                        settings.bot.health_exit_confirmation_neutral_obi,
                    )
                ),
            ),
        )

        entry_roc = float(position.entry_roc)
        entry_obi = float(position.entry_obi)
        roc_now = float(current_roc) if isinstance(current_roc, (int, float)) else None

        if position.direction == "long":
            roc_deteriorated = roc_now is not None and roc_now <= (entry_roc - roc_delta)
            obi_deteriorated = current_obi <= min(neutral_obi, entry_obi - obi_delta)
            return roc_deteriorated or obi_deteriorated

        roc_deteriorated = roc_now is not None and roc_now >= (entry_roc + roc_delta)
        obi_deteriorated = current_obi >= max(neutral_obi, entry_obi + obi_delta)
        return roc_deteriorated or obi_deteriorated

    def _settlement_for(self, ticker: str) -> dict:
        meta = {}
        if ticker in self.settlement_data:
            meta = dict(self.settlement_data[ticker] or {})
        timeline = self.contract_timelines.get(ticker)
        if timeline is not None:
            if timeline.close_time is not None and meta.get("close_time") is None:
                meta["close_time"] = timeline.close_time
            if timeline.result is not None and meta.get("result") is None:
                meta["result"] = timeline.result
            if timeline.expiration_value is not None and meta.get("expiration_value") is None:
                meta["expiration_value"] = timeline.expiration_value
        if meta.get("close_time") is None:
            parsed = self._ticker_close_time(ticker)
            if parsed is not None:
                meta["close_time"] = parsed
        return meta

    @staticmethod
    def _ticker_close_time(ticker: str) -> Optional[float]:
        m = _TICKER_CLOSE_RE.match((ticker or "").upper())
        if not m:
            return None
        yy, mmm, dd, hh = m.groups()
        month = _MONTH_MAP.get(mmm)
        if month is None:
            return None
        try:
            et_dt = datetime(
                year=2000 + int(yy),
                month=month,
                day=int(dd),
                hour=int(hh),
                minute=0,
                second=0,
                tzinfo=_ET_TZ,
            )
        except ValueError:
            return None
        return et_dt.astimezone(timezone.utc).timestamp()

    def _time_remaining_sec(self, ticker: str, ts: float) -> Optional[int]:
        close_time = self._settlement_for(ticker).get("close_time")
        if close_time is None:
            return None
        return int(close_time - ts)

    @staticmethod
    def _calc_pnl_pct(position: SimPosition, current_price: float) -> float:
        d = 1 if position.direction == "long" else -1
        return d * (current_price - position.entry_price) / position.entry_price

    def _close_position(
        self,
        position: SimPosition,
        exit_price: float,
        exit_ts: float,
        reason: str,
        current_bankroll: float,
        sizer: PositionSizer,
    ) -> float:
        pnl_pct = self._calc_pnl_pct(position, exit_price)
        pnl_per_contract = (1 if position.direction == "long" else -1) * (
            exit_price - position.entry_price
        ) / 100.0
        gross_pnl = pnl_per_contract * position.contracts
        fees = self._fee_engine.compute_round_trip_fee(
            entry_price_cents=position.entry_price,
            exit_price_cents=exit_price,
            contracts=position.contracts,
            entry_type="taker",
            exit_type="taker",
        )
        net_pnl = gross_pnl - fees

        self.trades.append(
            {
                "timestamp": position.entry_timestamp,
                "exit_timestamp": exit_ts,
                "ticker": position.ticker,
                "direction": position.direction,
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "pnl": round(net_pnl, 4),
                "pnl_pct": round(pnl_pct, 4),
                "fees": round(fees, 4),
                "exit_reason": reason,
                "conviction": position.conviction,
                "regime_at_entry": position.regime_at_entry,
                "candles_held": position.candles_held,
                "obi": position.entry_obi,
                "roc": position.entry_roc,
                "signal_driver": position.signal_driver,
                "max_favorable_excursion": round(position.max_favorable_excursion, 4),
                "max_adverse_excursion": round(position.max_adverse_excursion, 4),
                "position_uid": position.position_uid,
            }
        )

        sizer.record_trade(net_pnl)
        next_bankroll = current_bankroll + net_pnl
        self.equity_curve.append(next_bankroll)

        if self.mode == "paper":
            self._last_paper_exit_wall_ts = exit_ts
            self._last_paper_exit_per_pair[(position.ticker, position.direction)] = exit_ts

        self._health_breach_count = 0
        return next_bankroll

    def _lookup_tfi(self, ticker: str, ts: float) -> Optional[float]:
        entry = self.tfi_history.get(ticker)
        if isinstance(entry, (int, float)):
            return float(entry)
        if isinstance(entry, dict):
            candidate_ts = [k for k in entry.keys() if isinstance(k, (int, float)) and k <= ts]
            if not candidate_ts:
                return None
            return float(entry[max(candidate_ts)])
        return None

    def _events(self, start_ts: float, end_ts: float) -> list[ContractTick]:
        events: list[ContractTick] = []
        for timeline in self.contract_timelines.values():
            for tick in timeline.iter_ticks(start_ts, end_ts):
                events.append(tick)
        events.sort(key=lambda t: (t.timestamp, t.ticker))
        return events

    def _emit_signal_log(
        self,
        tick: ContractTick,
        decision,
        obi_dir: Direction,
        roc_dir: Direction,
        regime: str,
        ml_gate_mode: str,
    ) -> None:
        self.signal_log.append(
            {
                "timestamp": tick.timestamp,
                "ticker": tick.ticker,
                "obi": tick.obi,
                "obi_dir": obi_dir.value,
                "roc_dir": roc_dir.value,
                "decision": decision.direction.value if decision.direction else None,
                "conviction": decision.conviction.value,
                "regime": regime,
                "skip_reason": decision.skip_reason,
                "spread_state": decision.spread_state.value,
                "spread_cents": tick.spread_cents,
                "signal_driver": decision.signal_driver,
                "ml_gate_mode": ml_gate_mode,
            }
        )

    def run(self, bankroll: float = 10000.0) -> dict:
        if not self.candles:
            return compute_metrics([], [bankroll], bankroll)

        start_ts = self.candles[0]["timestamp"]
        end_ts = self.candles[-1]["timestamp"] + 900.0
        events = self._events(start_ts, end_ts)
        if not events:
            return compute_metrics([], [bankroll], bankroll)

        atr_filter = ATRRegimeFilter()
        spread_filter = SpreadRegimeFilter(time_fn=self._now)
        sizer = PositionSizer(bankroll)
        breaker = CircuitBreaker(sizer, never_halt=(self.mode != "live"))

        overrides = self.config
        hard_stop_loss_pct = float(
            overrides.get("hard_stop_loss_pct", settings.risk.hard_stop_loss_pct)
        )
        min_candles_before_early_exit = int(
            overrides.get(
                "min_candles_before_early_exit",
                settings.risk.min_candles_before_early_exit,
            )
        )
        paper_reentry_cooldown_sec = float(
            overrides.get(
                "paper_reentry_cooldown_sec",
                settings.bot.paper_reentry_cooldown_sec,
            )
        )
        paper_same_side_cooldown_sec = float(
            overrides.get(
                "paper_same_side_cooldown_sec",
                settings.bot.paper_same_side_cooldown_sec,
            )
        )

        current_bankroll = bankroll
        candle_idx = 0
        closes: list[float] = []
        recent_candles: list[dict] = []
        latest_tick_by_ticker: dict[str, ContractTick] = {}
        obi_history_by_ticker: dict[str, list[float]] = {}

        position: Optional[SimPosition] = None

        for tick in events:
            self._sim_now = tick.timestamp
            latest_tick_by_ticker[tick.ticker] = tick

            newly_closed = 0
            while candle_idx < len(self.candles) and self.candles[candle_idx]["timestamp"] <= tick.timestamp:
                candle = self.candles[candle_idx]
                atr_filter.update(candle["high"], candle["low"], candle["close"])
                closes.append(candle["close"])
                recent_candles.append(candle)
                if len(recent_candles) > 50:
                    recent_candles.pop(0)
                newly_closed += 1
                candle_idx += 1
            if position is not None and newly_closed > 0:
                position.candles_held += newly_closed

            regime = atr_filter.current_regime
            spread_filter.update(tick.spread_cents)

            if position is not None:
                settlement = self._settlement_for(position.ticker)
                close_time = settlement.get("close_time")
                if (
                    self.mode == "paper"
                    and close_time is not None
                    and tick.timestamp > (close_time + settings.bot.stale_paper_grace_sec)
                    and settlement.get("expiration_value") is None
                    and settlement.get("result") is None
                ):
                    current_bankroll = self._close_position(
                        position=position,
                        exit_price=position.entry_price,
                        exit_ts=tick.timestamp,
                        reason="STALE_TICKER_CLEANUP",
                        current_bankroll=current_bankroll,
                        sizer=sizer,
                    )
                    position = None
                    continue
                if close_time is not None and tick.timestamp >= close_time:
                    expiration_value = settlement.get("expiration_value")
                    if expiration_value is None:
                        result = settlement.get("result")
                        if result == "yes":
                            expiration_value = 100.0
                        elif result == "no":
                            expiration_value = 0.0
                    if expiration_value is not None:
                        current_bankroll = self._close_position(
                            position=position,
                            exit_price=float(expiration_value),
                            exit_ts=tick.timestamp,
                            reason="CONTRACT_SETTLED",
                            current_bankroll=current_bankroll,
                            sizer=sizer,
                        )
                        position = None
                        continue

                pos_tick = latest_tick_by_ticker.get(position.ticker)
                if pos_tick is not None:
                    time_remaining_sec = self._time_remaining_sec(position.ticker, tick.timestamp)

                    if (
                        position.direction == "short"
                        and time_remaining_sec is not None
                        and time_remaining_sec < settings.risk.short_settlement_guard_sec
                        and time_remaining_sec >= 60
                    ):
                        guard_price = self._exit_price_from_tick(position, pos_tick)
                        if guard_price is not None and guard_price > position.entry_price:
                            current_bankroll = self._close_position(
                                position=position,
                                exit_price=guard_price,
                                exit_ts=tick.timestamp,
                                reason="SHORT_SETTLEMENT_GUARD",
                                current_bankroll=current_bankroll,
                                sizer=sizer,
                            )
                            position = None
                            continue

                    if (
                        time_remaining_sec is not None
                        and time_remaining_sec < settings.bot.expiry_guard_trigger_sec
                    ):
                        guard_price = self._exit_price_from_tick(position, pos_tick)
                        if guard_price is not None:
                            current_bankroll = self._close_position(
                                position=position,
                                exit_price=guard_price,
                                exit_ts=tick.timestamp,
                                reason="EXPIRY_GUARD",
                                current_bankroll=current_bankroll,
                                sizer=sizer,
                            )
                            position = None
                            continue

                    current_price = self._exit_price_from_tick(position, pos_tick)
                    if current_price is not None:
                        pnl_pct = self._calc_pnl_pct(position, current_price)
                        position.max_favorable_excursion = max(
                            position.max_favorable_excursion,
                            pnl_pct,
                        )
                        position.max_adverse_excursion = min(
                            position.max_adverse_excursion,
                            pnl_pct,
                        )

                        current_roc = calculate_roc(
                            closes,
                            int(overrides.get("roc_lookback", settings.roc.lookback)),
                        )
                        latest_candle = recent_candles[-1] if recent_candles else None

                        health_exit_reason: Optional[str] = None
                        exit_intel_enabled = bool(
                            overrides.get(
                                "exit_intelligence_enabled",
                                settings.bot.exit_intelligence_enabled,
                            )
                        )
                        exit_intel_shadow_only = bool(
                            overrides.get(
                                "exit_intelligence_shadow_only",
                                settings.bot.exit_intelligence_shadow_only,
                            )
                        )
                        if exit_intel_enabled:
                            health_score, _ = compute_position_health_score(
                                direction=position.direction,
                                current_obi=tick.obi,
                                current_roc=current_roc,
                                entry_roc=position.entry_roc,
                                atr_regime=regime,
                                regime_at_entry=position.regime_at_entry,
                                pnl_pct=pnl_pct,
                                max_favorable_excursion=position.max_favorable_excursion,
                                mini_roc_fast=None,
                                mini_roc_slow=None,
                                weight_obi=settings.bot.health_weight_obi,
                                weight_roc=settings.bot.health_weight_roc,
                                weight_regime=settings.bot.health_weight_regime,
                                weight_mfe=settings.bot.health_weight_mfe,
                                weight_momentum=settings.bot.health_weight_momentum,
                            )
                            threshold = max(
                                0.0,
                                min(
                                    100.0,
                                    float(
                                        overrides.get(
                                            "health_score_threshold",
                                            settings.bot.health_score_threshold,
                                        )
                                    ),
                                ),
                            )
                            if health_score < threshold:
                                self._health_breach_count += 1
                            else:
                                self._health_breach_count = 0
                            breach_ticks = int(
                                overrides.get(
                                    "health_score_breach_ticks",
                                    settings.bot.health_score_breach_ticks,
                                )
                            )
                            if (
                                self._health_breach_count >= max(1, breach_ticks)
                                and not exit_intel_shadow_only
                            ):
                                if self._health_exit_confirmation_met(
                                    position=position,
                                    current_obi=tick.obi,
                                    current_roc=current_roc,
                                ):
                                    health_exit_reason = "HEALTH_SCORE_DECAY"
                        else:
                            self._health_breach_count = 0

                        if hard_stop_loss_pct > 0 and pnl_pct <= -hard_stop_loss_pct:
                            current_bankroll = self._close_position(
                                position=position,
                                exit_price=current_price,
                                exit_ts=tick.timestamp,
                                reason="HARD_STOP_LOSS",
                                current_bankroll=current_bankroll,
                                sizer=sizer,
                            )
                            position = None
                            continue

                        if (
                            position.candles_held < min_candles_before_early_exit
                            and regime != "HIGH"
                        ):
                            if health_exit_reason:
                                current_bankroll = self._close_position(
                                    position=position,
                                    exit_price=current_price,
                                    exit_ts=tick.timestamp,
                                    reason=health_exit_reason,
                                    current_bankroll=current_bankroll,
                                    sizer=sizer,
                                )
                                position = None
                            self.equity_curve.append(current_bankroll)
                            continue

                        obi_exit = check_obi_exit(
                            direction=position.direction,
                            current_obi=tick.obi,
                            pnl_pct=pnl_pct,
                            candles_held=position.candles_held,
                            atr_regime=regime,
                            overrides=overrides,
                        )
                        if obi_exit:
                            current_bankroll = self._close_position(
                                position=position,
                                exit_price=current_price,
                                exit_ts=tick.timestamp,
                                reason=obi_exit,
                                current_bankroll=current_bankroll,
                                sizer=sizer,
                            )
                            position = None
                            continue

                        roc_exit = check_roc_exit(
                            direction=position.direction,
                            pnl_pct=pnl_pct,
                            entry_roc=position.entry_roc,
                            current_roc=current_roc,
                            latest_candle=latest_candle,
                            candles_held=position.candles_held,
                            overrides=overrides,
                        )
                        if roc_exit:
                            current_bankroll = self._close_position(
                                position=position,
                                exit_price=current_price,
                                exit_ts=tick.timestamp,
                                reason=roc_exit,
                                current_bankroll=current_bankroll,
                                sizer=sizer,
                            )
                            position = None
                            continue

                        if health_exit_reason:
                            current_bankroll = self._close_position(
                                position=position,
                                exit_price=current_price,
                                exit_ts=tick.timestamp,
                                reason=health_exit_reason,
                                current_bankroll=current_bankroll,
                                sizer=sizer,
                            )
                            position = None
                            continue

            if (
                position is None
                and self._forced_entry is not None
                and not self._forced_entry_opened
            ):
                forced_ticker = str(self._forced_entry.get("ticker", tick.ticker))
                forced_direction = str(
                    self._forced_entry.get("direction", "")
                ).strip().lower()
                forced_entry_ts = float(
                    self._forced_entry.get("entry_ts", tick.timestamp)
                )
                allow_open_on_first_tick = bool(
                    self._forced_entry.get("allow_open_on_first_tick", False)
                )
                forced_entry_price = self._forced_entry.get("entry_price")
                if (
                    tick.ticker == forced_ticker
                    and (
                        tick.timestamp >= forced_entry_ts
                        or allow_open_on_first_tick
                    )
                    and forced_direction in {"long", "short"}
                    and isinstance(forced_entry_price, (int, float))
                    and float(forced_entry_price) > 0
                ):
                    self._position_seq += 1
                    contracts = int(self._forced_entry.get("contracts", 1))
                    position = SimPosition(
                        position_uid=f"sim-{self._position_seq:08d}",
                        ticker=forced_ticker,
                        direction=forced_direction,
                        contracts=max(1, contracts),
                        entry_price=float(forced_entry_price),
                        entry_timestamp=forced_entry_ts,
                        conviction=str(
                            self._forced_entry.get("conviction", "NORMAL")
                        ).upper(),
                        regime_at_entry=regime,
                        entry_obi=float(self._forced_entry.get("entry_obi", tick.obi)),
                        entry_roc=float(self._forced_entry.get("entry_roc", 0.0)),
                        signal_driver=str(
                            self._forced_entry.get("signal_driver", "FORCED_REPLAY")
                        ),
                    )
                    self._forced_entry_opened = True
                    self._health_breach_count = 0

            if position is None and not self._disable_signal_entries:
                can_trade, _halt_reason = breaker.can_trade()
                ticker_obi_history = obi_history_by_ticker.setdefault(tick.ticker, [])
                ticker_obi_history.append(tick.obi)
                total_book_volume = tick.total_bid_vol + tick.total_ask_vol

                obi_dir = evaluate_obi(
                    obi_history=ticker_obi_history,
                    total_book_volume=total_book_volume,
                    atr_regime=regime,
                    has_position=False,
                    overrides=overrides,
                )

                candle_dicts = recent_candles[-6:]
                current_atr_pct = (
                    atr_filter.atr_pct_history[-1]
                    if atr_filter.atr_pct_history
                    else None
                )
                roc_dir = evaluate_roc(
                    closes=closes,
                    candles=candle_dicts,
                    atr_regime=regime,
                    obi_direction=obi_dir,
                    has_position=False,
                    overrides=overrides,
                    atr_pct=current_atr_pct,
                )

                spread_state = evaluate_spread_divergence(
                    spread_history=spread_filter.spread_history(),
                    current_spread=tick.spread_cents,
                    atr_regime=regime,
                    overrides=overrides,
                )

                decision = self._resolver.resolve(
                    obi_direction=obi_dir,
                    roc_direction=roc_dir,
                    atr_regime=regime,
                    can_trade=can_trade,
                    spread_state=spread_state,
                )

                if (
                    decision.should_trade_in(self.mode)
                    and decision.obi_dir != Direction.NEUTRAL
                ):
                    tfi = self._lookup_tfi(tick.ticker, tick.timestamp)
                    if tfi is not None:
                        thresh = float(
                            overrides.get(
                                "tfi_disagree_threshold",
                                settings.historical_sync.tfi_disagree_threshold,
                            )
                        )
                        disagrees = (
                            (decision.obi_dir == Direction.LONG and tfi < 0.5 - thresh)
                            or (
                                decision.obi_dir == Direction.SHORT
                                and tfi > 0.5 + thresh
                            )
                        )
                        if disagrees:
                            new_conv = Conviction.downgrade(decision.conviction)
                            decision = decision.with_conviction(
                                new_conv,
                                skip_reason=(
                                    "TFI_DISAGREE"
                                    if new_conv == Conviction.NONE
                                    else None
                                ),
                            )

                self._trend_guard.apply_short_trend_filter(decision, closes, self.mode)

                roc_val = calculate_roc(
                    closes,
                    int(overrides.get("roc_lookback", settings.roc.lookback)),
                ) or 0.0

                ml_gate_mode = str(overrides.get("ml_gate_mode", self._ml_gate_mode)).lower()
                if ml_gate_mode not in {"config", "disabled"}:
                    ml_gate_mode = "config"
                if (
                    ml_gate_mode != "disabled"
                    and settings.ml.gate_enabled
                    and decision.should_trade_in(self.mode)
                    and (
                        (self.mode == "paper" and settings.ml.gate_paper)
                        or (self.mode == "live" and settings.ml.gate_live)
                    )
                ):
                    feat = {
                        "obi": tick.obi,
                        "spread_cents": tick.spread_cents or 0.0,
                        "roc_5": roc_val,
                        "time_remaining_sec": self._time_remaining_sec(
                            tick.ticker, tick.timestamp
                        )
                        or 0,
                    }
                    allowed, p_win = ml_gate(feat)
                    if not allowed:
                        decision = decision.with_conviction(
                            Conviction.NONE,
                            skip_reason=f"ML_GATE_REJECTED_p{p_win:.2f}",
                        )

                if (
                    self.mode == "live"
                    and settings.edge_profile.enabled
                    and decision.should_trade_in(self.mode)
                ):
                    edge_ok, edge_reason = evaluate_edge_profile(
                        decision=decision,
                        entry_price=None,
                        roc_value=roc_val,
                    )
                    if not edge_ok:
                        decision = decision.with_conviction(
                            Conviction.NONE,
                            skip_reason=edge_reason,
                        )

                self._emit_signal_log(
                    tick=tick,
                    decision=decision,
                    obi_dir=obi_dir,
                    roc_dir=roc_dir,
                    regime=regime,
                    ml_gate_mode=ml_gate_mode,
                )

                if decision.should_trade_in(self.mode) and decision.direction is not None:
                    if (
                        self.mode == "paper"
                        and paper_reentry_cooldown_sec > 0
                        and (tick.timestamp - self._last_paper_exit_wall_ts)
                        < paper_reentry_cooldown_sec
                    ):
                        self.equity_curve.append(current_bankroll)
                        continue

                    if self.mode == "paper" and paper_same_side_cooldown_sec > 0:
                        same_thesis_ok = self._paper_same_thesis_gate(
                            ticker=tick.ticker,
                            direction=decision.direction.value,
                            now_ts=tick.timestamp,
                            cooldown_sec=paper_same_side_cooldown_sec,
                        )
                        if not same_thesis_ok:
                            self.equity_curve.append(current_bankroll)
                            continue

                    time_remaining_sec = self._time_remaining_sec(tick.ticker, tick.timestamp)
                    if time_remaining_sec is None or time_remaining_sec < settings.bot.min_seconds_to_expiry:
                        self.equity_curve.append(current_bankroll)
                        continue

                    entry_price = self._entry_price_from_tick(tick, decision.direction)
                    if entry_price is None or entry_price <= 0:
                        self.equity_curve.append(current_bankroll)
                        continue

                    allowed, guard_reason = self._price_guard.is_allowed(
                        entry_price=entry_price,
                        direction=decision.direction.value,
                        atr_regime=regime,
                        time_remaining_sec=time_remaining_sec,
                    )
                    if not allowed:
                        # Keep reason in signal history for diagnostics.
                        self.signal_log[-1]["skip_reason"] = guard_reason
                        self.equity_curve.append(current_bankroll)
                        continue

                    if self.mode == "live" and settings.edge_profile.enabled:
                        edge_ok, edge_reason = evaluate_edge_profile(
                            decision=decision,
                            entry_price=entry_price,
                            roc_value=roc_val,
                        )
                        if not edge_ok:
                            self.signal_log[-1]["skip_reason"] = edge_reason
                            self.equity_curve.append(current_bankroll)
                            continue

                    size_dollars = sizer.calculate_size(
                        decision.conviction.value,
                        decision.direction.value,
                    )
                    override_risk = overrides.get("risk_per_trade_pct")
                    if (
                        override_risk is not None
                        and settings.risk.risk_per_trade_pct > 0
                    ):
                        size_dollars *= float(override_risk) / settings.risk.risk_per_trade_pct

                    contracts = int(size_dollars / (entry_price / 100.0)) if entry_price > 0 else 0
                    if contracts < 1:
                        self.equity_curve.append(current_bankroll)
                        continue

                    self._position_seq += 1
                    position = SimPosition(
                        position_uid=f"sim-{self._position_seq:08d}",
                        ticker=tick.ticker,
                        direction=decision.direction.value,
                        contracts=contracts,
                        entry_price=entry_price,
                        entry_timestamp=tick.timestamp,
                        conviction=decision.conviction.value,
                        regime_at_entry=regime,
                        entry_obi=tick.obi,
                        entry_roc=roc_val,
                        signal_driver=decision.signal_driver,
                    )
                    self._health_breach_count = 0

            self.equity_curve.append(current_bankroll)

        if position is not None:
            final_tick = latest_tick_by_ticker.get(position.ticker)
            final_price = (
                self._exit_price_from_tick(position, final_tick)
                if final_tick is not None
                else position.entry_price
            )
            if final_price is None:
                final_price = position.entry_price
            current_bankroll = self._close_position(
                position=position,
                exit_price=final_price,
                exit_ts=end_ts,
                reason="END_OF_DATA",
                current_bankroll=current_bankroll,
                sizer=sizer,
            )

        return compute_metrics(self.trades, self.equity_curve, bankroll)
