"""Boundary coverage for the shared ``wait_until`` helper (issue #143).

``wait_until`` is condition-first: the predicate always receives at
least one evaluation, so an already-satisfied condition succeeds even
at a zero or exhausted budget, while a false condition still fails
immediately at the hard deadline. These tests are deterministic — the
satisfied paths never sleep, and the exhausted-budget failure raises
without waiting out any interval.
"""

from __future__ import annotations

import time
import unittest

from tests.m10_fixtures import wait_until


class WaitUntilBoundaryTests(unittest.TestCase):
    def test_already_true_with_zero_budget_passes(self) -> None:
        calls = 0

        def condition() -> bool:
            nonlocal calls
            calls += 1
            return True

        wait_until(condition, timeout=0.0)
        # Evaluated exactly once, before any clock decision: the old
        # clock-first loop raised without ever looking at the predicate.
        self.assertEqual(1, calls)

    def test_already_true_with_near_zero_budget_passes(self) -> None:
        wait_until(lambda: True, timeout=1e-9)

    def test_false_at_exhausted_budget_fails_immediately(self) -> None:
        started = time.monotonic()
        with self.assertRaises(AssertionError) as ctx:
            wait_until(lambda: False, timeout=0.0, message="boundary exhausted")
        # The failure must not wait out a budget-sized interval.
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual("boundary exhausted", str(ctx.exception))

    def test_condition_becoming_true_within_budget_passes(self) -> None:
        calls = 0

        def condition() -> bool:
            nonlocal calls
            calls += 1
            return calls >= 3

        # Predicate-driven: true on the third call, far inside the budget
        # at the fastest poll granularity — covers the not-true-at-entry
        # path without depending on clock timing.
        wait_until(condition, timeout=5.0, interval=0.001)
        self.assertEqual(3, calls)


if __name__ == "__main__":
    _ = unittest.main()
