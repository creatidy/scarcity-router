from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import override
from unittest.mock import patch

from tools import test_runner


class _SubprocessFixture(unittest.TestCase):
    def _expected_success(self) -> None:
        self.assertEqual(os.environ["HOME"], os.environ["TMPDIR"])
        self.assertNotIn("SR_RUNNER_SYNTHETIC_SECRET", os.environ)

    def _expected_failure(self) -> None:
        self.fail("synthetic child failure")


class ParallelWaitRunnerTests(unittest.TestCase):
    def test_partition_preserves_every_test_once(self) -> None:
        suite = unittest.TestSuite(
            [_SubprocessFixture("_expected_success"), _SubprocessFixture("_expected_failure")]
        )
        selected_id = _SubprocessFixture("_expected_success").id()
        with patch.object(test_runner, "_PARALLEL_WAIT_TESTS", frozenset({selected_id})):
            serial, selected = test_runner.partition_wait_tests(suite)
        self.assertEqual([selected_id], selected)
        self.assertEqual([_SubprocessFixture("_expected_failure").id()], [test.id() for test in test_runner.flatten_tests(serial)])

    def test_partition_falls_back_if_a_name_is_missing_or_duplicated(self) -> None:
        case = _SubprocessFixture("_expected_success")
        with patch.object(test_runner, "_PARALLEL_WAIT_TESTS", frozenset({case.id(), "missing"})):
            serial, selected = test_runner.partition_wait_tests(unittest.TestSuite([case]))
        self.assertEqual([], selected)
        self.assertEqual([case.id()], [test.id() for test in test_runner.flatten_tests(serial)])
        with patch.object(test_runner, "_PARALLEL_WAIT_TESTS", frozenset({case.id()})):
            serial, selected = test_runner.partition_wait_tests(unittest.TestSuite([case, case]))
        self.assertEqual([], selected)
        self.assertEqual(2, len(test_runner.flatten_tests(serial)))

    def test_partition_preserves_class_fixture_semantics(self) -> None:
        class WithClassFixture(unittest.TestCase):
            @classmethod
            @override
            def setUpClass(cls) -> None:
                pass

            def runTest(self) -> None:
                pass

        case = WithClassFixture()
        with patch.object(test_runner, "_PARALLEL_WAIT_TESTS", frozenset({case.id()})):
            serial, selected = test_runner.partition_wait_tests(unittest.TestSuite([case]))
        self.assertEqual([], selected)
        self.assertEqual([case.id()], [test.id() for test in test_runner.flatten_tests(serial)])

    def test_partition_preserves_custom_suite_behavior(self) -> None:
        class CustomSuite(unittest.TestSuite):
            pass

        case = _SubprocessFixture("_expected_success")
        custom = CustomSuite([case])
        suite = unittest.TestSuite([custom])
        with patch.object(test_runner, "_PARALLEL_WAIT_TESTS", frozenset({case.id()})):
            serial, selected = test_runner.partition_wait_tests(suite)
        self.assertEqual([], selected)
        self.assertIs(suite, serial)
        self.assertIs(custom, list(serial)[0])

    def test_child_failures_are_reported_and_not_accepted(self) -> None:
        output = io.StringIO()
        names = [
            "test_test_runner._SubprocessFixture._expected_success",
            "test_test_runner._SubprocessFixture._expected_failure",
        ]
        with patch.dict(os.environ, {"SR_RUNNER_SYNTHETIC_SECRET": "synthetic"}), redirect_stdout(output):
            passed, timings = test_runner.run_parallel_wait_tests(names)
        self.assertFalse(passed)
        self.assertEqual(names, [name for _, name in timings])
        self.assertIn("synthetic child failure", output.getvalue())
        self.assertIn("Parallel test failed", output.getvalue())

    def test_process_start_failure_is_not_accepted(self) -> None:
        output = io.StringIO()
        with patch("tools.test_runner.subprocess.run", side_effect=OSError("synthetic detail")), redirect_stdout(output):
            passed, _ = test_runner.run_parallel_wait_tests(["test_test_runner._SubprocessFixture._expected_success"])
        self.assertFalse(passed)
        self.assertIn("Could not start test process: OSError", output.getvalue())
        self.assertNotIn("synthetic detail", output.getvalue())

    def test_main_propagates_a_parallel_failure(self) -> None:
        case = _SubprocessFixture("_expected_success")
        suite = unittest.TestSuite([case])
        with (
            patch.object(test_runner, "_PARALLEL_WAIT_TESTS", frozenset({case.id()})),
            patch.object(unittest.TestLoader, "discover", return_value=suite),
            patch.object(test_runner, "run_parallel_wait_tests", return_value=(False, [])),
            patch("sys.argv", ["test_runner.py"]),
            patch.dict(os.environ, {"SR_PARALLEL_WAIT_TESTS": "1", "SR_TEST_TIMING": "1"}),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(1, test_runner.main())
