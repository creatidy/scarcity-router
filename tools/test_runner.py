"""The ``make test`` entry point: plain unittest discovery, unchanged by
default, with optional per-test timing observability.

``SR_TEST_TIMING=1 make test`` appends a "Slowest tests" table and the
total test wall time to the run output (issue #111: answer "which tests
are slow?" without new tooling). Without the variable the behavior and
output are exactly ``python -m unittest discover -s tests``. No new
dependencies; the exit-code contract is unittest's own.
"""

from __future__ import annotations

import os
import sys
import time
import unittest


class _TimingResult(unittest.TextTestResult):
    """TextTestResult that records per-test wall time (no output change)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.timings: list[tuple[float, str]] = []
        self._started: float = 0.0

    def startTest(self, test: unittest.TestCase) -> None:
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:
        self.timings.append((time.perf_counter() - self._started, test.id()))
        super().stopTest(test)


def main() -> int:
    timing = os.environ.get("SR_TEST_TIMING", "") == "1"
    loader = unittest.TestLoader()
    if len(sys.argv) > 1:
        # Same ergonomics as `python -m unittest tests.some_module.Test`: a
        # focused run names its tests; no names means full discovery.
        suite = unittest.TestSuite(
            [loader.loadTestsFromName(name) for name in sys.argv[1:]]
        )
    else:
        suite = loader.discover("tests", pattern="test_*.py")
    result_class = _TimingResult if timing else unittest.TextTestResult
    runner = unittest.TextTestRunner(resultclass=result_class)
    start = time.perf_counter()
    result = runner.run(suite)
    wall = time.perf_counter() - start
    if timing:
        timings = sorted(result.timings, reverse=True)
        print("\nSlowest tests:")
        for elapsed, test_id in timings[:15]:
            print(f"  {elapsed:6.2f}s  {test_id}")
        print(f"\nTotal tests: {result.testsRun}")
        print(f"Test wall time: {wall:.1f}s")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    _ = sys.exit(main())
