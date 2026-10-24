import io
import os
import sys
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from scripts.run_stack_rehearsal import (
    _ProcessResourceSampler,
    _read_process_resources,
    main,
)


class ProcessResourceSamplerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process counters are platform-specific")
    def test_reads_real_windows_child_process_counters(self):
        metrics = _read_process_resources(os.getpid())

        self.assertGreaterEqual(metrics["cpuSeconds"], 0)
        self.assertGreater(metrics["workingSetBytes"], 0)

    def test_samples_process_cpu_and_working_set_deltas(self):
        reads = {101: 0, 202: 0}

        def reader(pid):
            reads[pid] += 1
            return {
                "cpuSeconds": reads[pid] / 4,
                "workingSetBytes": pid * 10 + reads[pid],
            }

        with _ProcessResourceSampler(
            {"api": 101, "dashboard": 202},
            interval_seconds=0.01,
            reader=reader,
        ) as sampler:
            time.sleep(0.035)

        result = sampler.result()

        self.assertGreaterEqual(result["sampleCount"], 2)
        self.assertGreater(result["peakSampledTotalWorkingSetBytes"], 0)
        self.assertEqual(result["unavailableProcesses"], [])
        self.assertTrue(result["processes"]["api"]["measured"])
        self.assertGreater(result["processes"]["api"]["cpuSeconds"], 0)
        self.assertGreater(
            result["processes"]["dashboard"]["peakSampledWorkingSetBytes"],
            result["processes"]["dashboard"]["baselineWorkingSetBytes"],
        )

    def test_reports_unavailable_process_without_failing_rehearsal(self):
        sampler = _ProcessResourceSampler(
            {"missing": 404}, reader=lambda _pid: (_ for _ in ()).throw(ProcessLookupError()),
        )

        with sampler:
            pass

        result = sampler.result()

        self.assertEqual(result["unavailableProcesses"], ["missing"])
        self.assertFalse(result["processes"]["missing"]["measured"])
        self.assertIsNone(result["processes"]["missing"]["cpuSeconds"])

    def test_combined_rehearsal_rejects_bursts_over_the_rate_limit_budget(self):
        with (
            patch.object(sys, "argv", ["run_stack_rehearsal.py", "--benchmark-requests", "31"]),
            self.assertRaises(SystemExit) as result,
        ):
            main()

        self.assertEqual(result.exception.code, 2)

    def test_combined_rehearsal_bounds_synthetic_ledger_size(self):
        error = io.StringIO()
        with (
            redirect_stderr(error),
            patch.object(
                sys,
                "argv",
                ["run_stack_rehearsal.py", "--synthetic-ledger-documents", "2001"],
            ),
            self.assertRaises(SystemExit) as result,
        ):
            main()

        self.assertEqual(result.exception.code, 2)
        self.assertIn("--synthetic-ledger-documents must be between 0 and 2000", error.getvalue())

    def test_large_synthetic_ledger_requires_a_dashboard_benchmark(self):
        error = io.StringIO()
        with (
            redirect_stderr(error),
            patch.object(
                sys,
                "argv",
                ["run_stack_rehearsal.py", "--synthetic-ledger-documents", "100"],
            ),
            self.assertRaises(SystemExit) as result,
        ):
            main()

        self.assertEqual(result.exception.code, 2)
        self.assertIn("--synthetic-ledger-documents requires --benchmark-requests", error.getvalue())


if __name__ == "__main__":
    unittest.main()
