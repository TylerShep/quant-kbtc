"""
Walk-forward optimizer — rolling train/test windows with overfitting diagnosis.
Per the walk-forward-optimizer skill.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from backtesting.backtester import Backtester


WALK_FORWARD_CONFIG = {
    "train_window": 3500,
    "test_window": 500,
    "step_size": 500,
    "min_trades_per_window": 30,
}

MAX_GRID_COMBOS = 10000
RANDOM_TRIALS = 200


@dataclass
class WindowResult:
    window_id: int
    train_range: tuple[int, int]
    test_range: tuple[int, int]
    best_params: dict
    train_sharpe: float
    test_sharpe: float
    test_win_rate: float
    test_trades: int
    overfitting_gap: float


class WalkForwardOptimizer:
    def __init__(self, candles: list[dict], ob_history: dict,
                 settlement_data: Optional[dict] = None,
                 tfi_history: Optional[dict] = None):
        self.candles = candles
        self.ob_history = ob_history
        self.settlement_data = settlement_data or {}
        self.tfi_history = tfi_history or {}

    def run(self, param_space: dict, objective: str = "sharpe_ratio") -> list[WindowResult]:
        windows = self._generate_windows()
        results: list[WindowResult] = []

        for i, (train_range, test_range) in enumerate(windows):
            best_params, train_sharpe = self._optimize_on_window(
                train_range, param_space, objective
            )

            test_candles = self.candles[test_range[0] : test_range[1]]
            bt = Backtester(test_candles, self.ob_history, best_params,
                            settlement_data=self.settlement_data)
            test_result = bt.run()

            if test_result["total_trades"] < WALK_FORWARD_CONFIG["min_trades_per_window"]:
                continue

            results.append(
                WindowResult(
                    window_id=i,
                    train_range=train_range,
                    test_range=test_range,
                    best_params=best_params,
                    train_sharpe=train_sharpe,
                    test_sharpe=test_result.get("sharpe_ratio", 0),
                    test_win_rate=test_result.get("win_rate", 0),
                    test_trades=test_result["total_trades"],
                    overfitting_gap=train_sharpe - test_result.get("sharpe_ratio", 0),
                )
            )

        return results

    def _generate_windows(self) -> list[tuple[tuple[int, int], tuple[int, int]]]:
        cfg = WALK_FORWARD_CONFIG
        windows = []
        start = 0
        while start + cfg["train_window"] + cfg["test_window"] <= len(self.candles):
            train_end = start + cfg["train_window"]
            test_end = train_end + cfg["test_window"]
            windows.append(((start, train_end), (train_end, test_end)))
            start += cfg["step_size"]
        return windows

    def _optimize_on_window(
        self, train_range: tuple[int, int], param_space: dict, objective: str
    ) -> tuple[dict, float]:
        """Grid search (or random search if grid is too large) on training window."""
        train_candles = self.candles[train_range[0] : train_range[1]]

        full_grid = self._expand_grid(param_space)
        if len(full_grid) > MAX_GRID_COMBOS:
            param_combos = [
                {k: random.choice(v) for k, v in param_space.items()}
                for _ in range(RANDOM_TRIALS)
            ]
        else:
            param_combos = full_grid

        best_params = {}
        best_score = float("-inf")

        for params in param_combos:
            bt = Backtester(train_candles, self.ob_history, params,
                            settlement_data=self.settlement_data)
            result = bt.run()
            if result["total_trades"] < 30:
                continue
            score = result.get(objective, 0)
            if score > best_score:
                best_score = score
                best_params = params

        return best_params, best_score

    @staticmethod
    def _expand_grid(param_space: dict) -> list[dict]:
        """Expand parameter space into all combinations."""
        keys = list(param_space.keys())
        if not keys:
            return [{}]

        combos = [{}]
        for key in keys:
            new_combos = []
            for combo in combos:
                for val in param_space[key]:
                    new_combo = {**combo, key: val}
                    new_combos.append(new_combo)
            combos = new_combos
        return combos

    @staticmethod
    def select_final_params(results: list[WindowResult]) -> Optional[dict]:
        """Per-parameter majority vote across winning windows.

        Instead of voting on whole parameter sets, each parameter is voted
        on independently — the most common value for each key across all
        profitable windows is selected.  This is more robust than string-matching
        entire param dicts (per walk-forward-optimizer skill).
        """
        winning = [r for r in results if r.test_sharpe > 0.5]
        if not winning:
            return None

        param_votes: dict[str, Counter] = {}
        for r in winning:
            for key, val in r.best_params.items():
                if key not in param_votes:
                    param_votes[key] = Counter()
                param_votes[key][val] += 1

        return {key: counter.most_common(1)[0][0] for key, counter in param_votes.items()}

    @staticmethod
    def edge_consistency(results: list[WindowResult]) -> float:
        """Fraction of test windows with Sharpe > 0.5."""
        if not results:
            return 0.0
        return sum(1 for r in results if r.test_sharpe > 0.5) / len(results)

    @staticmethod
    def diagnose_overfitting(results: list[WindowResult]) -> dict:
        """Analyze walk-forward results for signs of overfitting.

        Returns a dict with core metrics, boolean flags, and a recommendation
        string per the walk-forward-optimizer skill.
        """
        if not results:
            return {
                "avg_train_sharpe": 0,
                "avg_test_sharpe": 0,
                "avg_overfitting_gap": 0,
                "pct_windows_profitable": 0,
                "high_overfitting": False,
                "inconsistent_edge": True,
                "edge_confirmed": False,
                "recommendation": "INSUFFICIENT DATA — no valid windows",
            }

        train_sharpes = [w.train_sharpe for w in results]
        test_sharpes = [w.test_sharpe for w in results]
        gaps = [w.overfitting_gap for w in results]

        avg_train = sum(train_sharpes) / len(train_sharpes)
        avg_test = sum(test_sharpes) / len(test_sharpes)
        avg_gap = sum(gaps) / len(gaps)

        pct_profitable = sum(1 for s in test_sharpes if s > 0) / len(test_sharpes)
        pct_strong = sum(1 for s in test_sharpes if s > 0.5) / len(test_sharpes)

        high_overfitting = avg_gap > 1.0
        inconsistent_edge = pct_strong < 0.6
        edge_confirmed = avg_test > 1.0 and avg_gap < 1.0

        recommendation = _overfitting_recommendation(avg_gap, avg_test)

        return {
            "avg_train_sharpe": round(avg_train, 4),
            "avg_test_sharpe": round(avg_test, 4),
            "avg_overfitting_gap": round(avg_gap, 4),
            "pct_windows_profitable": round(pct_profitable, 4),
            "high_overfitting": high_overfitting,
            "inconsistent_edge": inconsistent_edge,
            "edge_confirmed": edge_confirmed,
            "recommendation": recommendation,
        }


def _overfitting_recommendation(avg_gap: float, avg_test_sharpe: float) -> str:
    if avg_test_sharpe < 0:
        return "ABANDON — signal has no OOS edge"
    if avg_gap > 2.0:
        return "HIGH OVERFITTING — reduce parameter space, use simpler model"
    if avg_gap > 1.0:
        return "MODERATE OVERFITTING — widen test windows, reduce combos"
    if avg_test_sharpe > 1.0 and avg_gap < 1.0:
        return "DEPLOY CANDIDATE — edge appears robust"
    return "MARGINAL — gather more data before deploying"
