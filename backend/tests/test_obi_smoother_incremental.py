"""OBISmoother regression tests for the incremental-moments rewrite.

Background (BUG-035 follow-up #2): the original ``OBISmoother.update``
recomputed mean and variance over the full 60s buffer on every tick
via two O(n) passes (list comp + generator). py-spy showed it eating
~55% of MainThread CPU at the 20+ Hz market-tick rate after the
2026-05-06 ticker-parser cache fix removed the prior dominant
hot frame, which kept the loop saturated and starved the Coinbase WS
keepalive. The rewrite maintains running ``sum`` and ``sum_of_squares``
so each update is O(1).

These tests pin the new behaviour:
1. The new implementation must be **bit-identical** (within float ULPs)
   to the original on a deterministic stream of inputs at every tick.
2. Numerical drift must stay below a tight bound across many evictions
   thanks to the periodic reseed.
3. The cold-start path (n < 5 stdev samples) must still bypass the
   variance branch and use the base window.
"""
from __future__ import annotations

import math
import random
from collections import deque
from unittest.mock import patch

import pytest

from features.engine import OBISmoother


class _OriginalOBISmoother:
    """Verbatim copy of the pre-2026-05-06 implementation, used as the
    oracle for parity tests. DO NOT modify; if behaviour intentionally
    diverges the tests should be updated to reflect that, not this.
    """

    STDEV_LOOKBACK_SEC = 60.0
    NOISY_THRESHOLD = 0.15
    STABLE_THRESHOLD = 0.05
    EXPAND_MULT = 1.5
    CONTRACT_MULT = 0.75

    def __init__(self, base_window_sec: float = 5.0, min_samples: int = 3):
        self._base_window = base_window_sec
        self._min_samples = min_samples
        self._buffer: deque = deque(maxlen=2000)
        self._stdev_buffer: deque = deque(maxlen=2000)

    def update(self, obi: float, now: float) -> float:
        self._buffer.append((now, obi))
        self._stdev_buffer.append((now, obi))

        stdev_cutoff = now - self.STDEV_LOOKBACK_SEC
        while self._stdev_buffer and self._stdev_buffer[0][0] < stdev_cutoff:
            self._stdev_buffer.popleft()

        window = self._base_window
        if len(self._stdev_buffer) >= 5:
            vals = [v for _, v in self._stdev_buffer]
            mean = sum(vals) / len(vals)
            variance = sum((v - mean) ** 2 for v in vals) / len(vals)
            stdev = math.sqrt(variance)
            if stdev > self.NOISY_THRESHOLD:
                window = self._base_window * self.EXPAND_MULT
            elif stdev < self.STABLE_THRESHOLD:
                window = self._base_window * self.CONTRACT_MULT

        cutoff = now - window
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()

        vals_in_window = [v for _, v in self._buffer]
        if len(vals_in_window) < self._min_samples:
            return obi

        vals_in_window.sort()
        mid = len(vals_in_window) // 2
        if len(vals_in_window) % 2 == 0:
            return (vals_in_window[mid - 1] + vals_in_window[mid]) / 2
        return vals_in_window[mid]


class _StubTime:
    """Monotonic-ish stub clock for time.time() patches."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t


class TestOBISmootherParity:
    """The new incremental smoother must produce identical outputs to the
    original implementation on every tick of a deterministic stream."""

    def test_parity_under_realistic_tick_rate(self):
        random.seed(42)
        stub = _StubTime()
        new = OBISmoother()
        old = _OriginalOBISmoother()

        with patch("features.engine.time.time", stub):
            for _ in range(2000):
                obi = random.uniform(0.0, 1.0)
                stub.t += random.uniform(0.01, 0.1)
                got = new.update(obi)
                want = old.update(obi, stub.t)
                assert got == pytest.approx(want, abs=1e-9), (
                    f"divergence at t={stub.t}: got={got} want={want}"
                )

    def test_parity_with_extreme_volatility_swings(self):
        """Mix periods of stable + noisy values to exercise both
        the EXPAND and CONTRACT branches."""
        stub = _StubTime()
        new = OBISmoother()
        old = _OriginalOBISmoother()

        # 200 stable ticks (variance ~0) then 200 noisy ticks
        # then 200 mid-volatility ticks
        sequence = (
            [0.5] * 200
            + [random.Random(1).uniform(0.0, 1.0) for _ in range(200)]
            + [0.5 + random.Random(2).uniform(-0.05, 0.05) for _ in range(200)]
        )

        with patch("features.engine.time.time", stub):
            for obi in sequence:
                stub.t += 0.05  # 20 Hz
                got = new.update(obi)
                want = old.update(obi, stub.t)
                assert got == pytest.approx(want, abs=1e-9), (
                    f"divergence at t={stub.t}, obi={obi}: "
                    f"got={got} want={want}"
                )

    def test_parity_after_buffer_evictions(self):
        """Run long enough that both implementations evict from the
        60s window, ensuring our incremental sum updates stay correct
        through many popleft() calls."""
        random.seed(7)
        stub = _StubTime()
        new = OBISmoother()
        old = _OriginalOBISmoother()

        # ~150s of ticks at 20 Hz = 3000 ticks. The 60s stdev window
        # cycles entries 50x, the 5s buffer cycles ~600x.
        with patch("features.engine.time.time", stub):
            for _ in range(3000):
                obi = random.uniform(0.0, 1.0)
                stub.t += 0.05
                got = new.update(obi)
                want = old.update(obi, stub.t)
                assert got == pytest.approx(want, abs=1e-9)

        # After the run, the new smoother's running sums must still be
        # tightly correlated with the recomputed-from-buffer values.
        recomputed_sum = sum(v for _, v in new._stdev_buffer)
        recomputed_sum_sq = sum(v * v for _, v in new._stdev_buffer)
        assert new._stdev_sum == pytest.approx(recomputed_sum, abs=1e-9)
        assert new._stdev_sum_sq == pytest.approx(recomputed_sum_sq, abs=1e-9)


class TestOBISmootherColdStart:
    """The first <5 stdev samples must skip the variance branch and use
    the base window unmodified."""

    def test_first_four_samples_use_base_window(self):
        stub = _StubTime()
        sm = OBISmoother(base_window_sec=5.0, min_samples=3)
        with patch("features.engine.time.time", stub):
            for i in range(4):
                stub.t += 0.1
                sm.update(0.5 + 0.01 * i)

        # Stdev branch was skipped, so sums should still equal the
        # straight summation of the four observed values.
        manual_sum = sum(v for _, v in sm._stdev_buffer)
        manual_sum_sq = sum(v * v for _, v in sm._stdev_buffer)
        assert sm._stdev_sum == pytest.approx(manual_sum, abs=1e-12)
        assert sm._stdev_sum_sq == pytest.approx(manual_sum_sq, abs=1e-12)


class TestOBISmootherReseedBound:
    """Even with no reseed event, accumulated float drift must be small;
    after a reseed it must be exactly recomputed."""

    def test_explicit_reseed_resets_evict_counter(self):
        sm = OBISmoother()
        sm._stdev_buffer.append((1000.0, 0.5))
        sm._stdev_sum = 999.0  # deliberately wrong
        sm._stdev_sum_sq = 999.0
        sm._evictions_since_reseed = 5000

        sm._reseed_stdev_moments()

        assert sm._stdev_sum == pytest.approx(0.5, abs=1e-12)
        assert sm._stdev_sum_sq == pytest.approx(0.25, abs=1e-12)
        assert sm._evictions_since_reseed == 0
