"""
Core backtesting simulation engine.
Per the backtesting-framework skill.
"""
from __future__ import annotations

from typing import Optional

from filters.atr_regime import ATRRegimeFilter
from strategies.obi import evaluate_obi, check_obi_exit, Direction
from strategies.roc import evaluate_roc, calculate_roc
from strategies.resolver import SignalConflictResolver, Conviction
from backtesting.metrics import compute_metrics
from config import settings


class Backtester:
    FEE_RATE = 0.007

    def __init__(self, candles: list[dict], ob_history: dict,
                 config: Optional[dict] = None):
        self.candles = candles
        self.ob_history = ob_history
        self.config = config or {}
        self.trades: list[dict] = []
        self.equity_curve: list[float] = []

    def run(self, bankroll: float = 10000.0) -> dict:
        atr_filter = ATRRegimeFilter()
        resolver = SignalConflictResolver()
        position = None
        current_bankroll = bankroll
        obi_history: list[float] = []

        risk_pct = self.config.get("risk_per_trade_pct", settings.risk.risk_per_trade_pct)
        stop_loss = self.config.get("stop_loss_pct", settings.risk.stop_loss_pct)

        closes: list[float] = []

        for i, candle in enumerate(self.candles):
            regime = atr_filter.update(candle["high"], candle["low"], candle["close"])
            closes.append(candle["close"])

            ob = self.ob_history.get(candle["timestamp"])
            obi_val = ob["obi"] if ob else 0.5
            obi_history.append(obi_val)

            if position:
                pnl_pct = self._calc_pnl_pct(position, candle["close"])
                position["candles_held"] += 1

                exit_reason = check_obi_exit(
                    position["direction"], obi_val, pnl_pct,
                    position["candles_held"], regime,
                )
                if exit_reason:
                    self._close_position(position, candle, exit_reason, current_bankroll)
                    current_bankroll = self.equity_curve[-1]
                    position = None

            if position is None and ob:
                total_vol = sum(b.get("size", 0) for b in ob.get("bids", [])) + \
                            sum(a.get("size", 0) for a in ob.get("asks", []))

                obi_dir = evaluate_obi(
                    obi_history, total_vol, regime, False,
                )

                candle_dicts = self.candles[max(0, i - 5) : i + 1]
                roc_dir = evaluate_roc(
                    closes, candle_dicts, regime, obi_dir, False,
                )

                decision = resolver.resolve(obi_dir, roc_dir, regime, True)

                if decision.should_trade:
                    risk_amount = current_bankroll * risk_pct
                    entry_price = candle["close"]
                    contracts = int(risk_amount / (entry_price / 100)) if entry_price > 0 else 0
                    if contracts >= 1:
                        position = {
                            "direction": decision.direction.value,
                            "entry_price": entry_price,
                            "entry_idx": i,
                            "contracts": contracts,
                            "risk_amount": risk_amount,
                            "conviction": decision.conviction.value,
                            "regime": regime,
                            "candles_held": 0,
                            "obi": obi_val,
                            "roc": calculate_roc(closes, settings.roc.lookback) or 0,
                        }

            self.equity_curve.append(current_bankroll)

        if position:
            self._close_position(
                position, self.candles[-1], "END_OF_DATA", current_bankroll
            )

        return compute_metrics(self.trades, self.equity_curve, bankroll)

    def _calc_pnl_pct(self, position: dict, current_price: float) -> float:
        d = 1 if position["direction"] == "long" else -1
        return d * (current_price - position["entry_price"]) / position["entry_price"]

    def _close_position(self, position: dict, candle: dict,
                        reason: str, current_bankroll: float):
        pnl_pct = self._calc_pnl_pct(position, candle["close"])
        notional = position["contracts"] * position["entry_price"] / 100
        gross_pnl = pnl_pct * notional
        fees = notional * self.FEE_RATE
        net_pnl = gross_pnl - fees

        self.trades.append({
            "timestamp": self.candles[position["entry_idx"]]["timestamp"],
            "exit_timestamp": candle["timestamp"],
            "direction": position["direction"],
            "entry_price": position["entry_price"],
            "exit_price": candle["close"],
            "pnl": round(net_pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "fees": round(fees, 4),
            "exit_reason": reason,
            "conviction": position["conviction"],
            "regime_at_entry": position["regime"],
            "candles_held": position["candles_held"],
            "obi": position["obi"],
            "roc": position["roc"],
        })
        self.equity_curve.append(current_bankroll + net_pnl)
