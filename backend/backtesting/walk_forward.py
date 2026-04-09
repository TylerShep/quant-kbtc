"""
Walk-forward optimizer — rolling train/test windows.
Per the walk-forward-optimizer skill.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backtesting.backtester import Backtester


WALK_FORWARD_CONFIG = {
    "total_candles": 17520,
    "train_window": 3500,
    "test_window": 500,
    "step_size": 500,
    "min_trades_per_window": 30,
    "train_test_ratio": 0.70,
}


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
    def __init__(self, candles: list[dict], ob_history: dict):
        self.candles = candles
        self.ob_history = ob_history

    def run(self, param_space: dict, objective: str = "sharpe_ratio") -> list[WindowResult]:
        windows = self._generate_windows()
        results: list[WindowResult] = []

        for i, (train_range, test_range) in enumerate(windows):
            best_params, train_sharpe = self._optimize_on_window(
                train_range, param_space, objective
            )

            test_candles = self.candles[test_range[0] : test_range[1]]
            bt = Backtester(test_candles, self.ob_history, best_params)
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
        """Grid search over param_space on training window."""
        train_candles = self.candles[train_range[0] : train_range[1]]

        best_params = {}
        best_score = float("-inf")

        param_combos = self._expand_grid(param_space)
        for params in param_combos:
            bt = Backtester(train_candles, self.ob_history, params)
            result = bt.run()
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
        """
        Majority vote across winning windows.
        Returns the most common parameter set among windows with Sharpe > 1.0.
        """
        winning = [r for r in results if r.test_sharpe > 1.0]
        if not winning:
            return None

        param_counts: dict[str, int] = {}
        for r in winning:
            key = str(sorted(r.best_params.items()))
            param_counts[key] = param_counts.get(key, 0) + 1

        best_key = max(param_counts, key=param_counts.get)
        for r in winning:
            if str(sorted(r.best_params.items())) == best_key:
                return r.best_params

        return winning[0].best_params

    @staticmethod
    def edge_consistency(results: list[WindowResult]) -> float:
        """Fraction of test windows with Sharpe > 1.0."""
        if not results:
            return 0.0
        return sum(1 for r in results if r.test_sharpe > 1.0) / len(results)
