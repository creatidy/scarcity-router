"""The ``make test`` entry point: plain unittest discovery, unchanged by
default, with optional per-test timing observability.

``SR_TEST_TIMING=1 make test`` appends a "Slowest tests" table and the
total test wall time to the run output (issue #111: answer "which tests
are slow?" without new tooling). Without the variable the behavior and
output are exactly ``python -m unittest discover -s tests`` unless CI opts
into isolated execution of the measured wait-heavy cases with
``SR_PARALLEL_WAIT_TESTS=1``. No new dependencies.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


_PARALLEL_WAIT_TESTS = frozenset(
    {
        "test_e2e_codex_acceptance.CodexCompositionTests.test_exact_configured_ownership_for_the_codex_resource",
        "test_e2e_codex_acceptance.CodexCompositionTests.test_auth_verdicts_drive_eligibility_through_snapshots",
        "test_e2e_codex_acceptance.ProductionCompatibilityMatrixTests.test_evidence_backed_m04_resource_gets_its_preset_cells",
        "test_e2e_codex_acceptance.ProductionCompatibilityMatrixTests.test_configuration_removal_rebuilds_the_matrix",
    }
)


def flatten_tests(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    cases: list[unittest.TestCase] = []
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            cases.extend(flatten_tests(test))
        else:
            cases.append(test)
    return cases


def partition_wait_tests(
    suite: unittest.TestSuite,
) -> tuple[unittest.TestSuite, list[str]]:
    def is_plain_suite(current: unittest.TestSuite) -> bool:
        return type(current) is unittest.TestSuite and all(
            is_plain_suite(test) for test in current if isinstance(test, unittest.TestSuite)
        )

    # Preserve custom suite execution behavior rather than flattening it.
    if not is_plain_suite(suite):
        return suite, []
    cases = flatten_tests(suite)
    selected = [test.id() for test in cases if test.id() in _PARALLEL_WAIT_TESTS]
    # A changed or duplicate test name must not silently reduce coverage.
    if len(selected) != len(_PARALLEL_WAIT_TESTS) or set(selected) != _PARALLEL_WAIT_TESTS:
        return suite, []
    for test in cases:
        if test.id() not in _PARALLEL_WAIT_TESTS:
            continue
        module = sys.modules.get(type(test).__module__)
        if module is None or any(hasattr(module, name) for name in ("setUpModule", "tearDownModule")):
            return suite, []
        if any(
            "setUpClass" in cls.__dict__ or "tearDownClass" in cls.__dict__
            for cls in type(test).__mro__ if cls is not unittest.TestCase
        ):
            return suite, []
    return unittest.TestSuite(test for test in cases if test.id() not in _PARALLEL_WAIT_TESTS), selected


def run_parallel_wait_tests(names: list[str]) -> tuple[bool, list[tuple[float, str]]]:
    if not names:
        return True, []
    with tempfile.TemporaryDirectory(prefix="scarcity-router-parallel-tests-") as root:
        def run(item: tuple[int, str]) -> tuple[bool, float, str, str]:
            index, name = item
            directory = Path(root) / str(index)
            directory.mkdir(mode=0o700)
            env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
            env.update({"HOME": str(directory), "TMPDIR": str(directory), "TEMP": str(directory), "TMP": str(directory), "PYTHONDONTWRITEBYTECODE": "1"})
            start = time.perf_counter()
            try:
                process = subprocess.run(
                    [sys.executable, "-m", "unittest", "-v", f"tests.{name}"],
                    env=env, capture_output=True, text=True, check=False,
                )
            except OSError as exc:
                return False, time.perf_counter() - start, name, f"Could not start test process: {type(exc).__name__}"
            return process.returncode == 0, time.perf_counter() - start, name, process.stdout + process.stderr

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, enumerate(names)))
    for passed, _elapsed, name, output in results:
        print(f"\nParallel test: {name}", flush=True)
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
        if not passed:
            print("Parallel test failed", flush=True)
    return all(passed for passed, _, _, _ in results), [(elapsed, name) for _, elapsed, name, _ in results]


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
    parallel: list[str] = []
    if len(sys.argv) == 1 and os.environ.get("SR_PARALLEL_WAIT_TESTS") == "1":
        suite, parallel = partition_wait_tests(suite)
    result_class = _TimingResult if timing else unittest.TextTestResult
    runner = unittest.TextTestRunner(resultclass=result_class)
    start = time.perf_counter()
    result = runner.run(suite)
    parallel_ok, parallel_timings = run_parallel_wait_tests(parallel)
    wall = time.perf_counter() - start
    if timing:
        timings = sorted([*result.timings, *parallel_timings], reverse=True)
        print("\nSlowest tests:")
        for elapsed, test_id in timings[:15]:
            print(f"  {elapsed:6.2f}s  {test_id}")
        print(f"\nTotal tests: {result.testsRun + len(parallel)}")
        print(f"Test wall time: {wall:.1f}s")
    return 0 if result.wasSuccessful() and parallel_ok else 1


if __name__ == "__main__":
    _ = sys.exit(main())
