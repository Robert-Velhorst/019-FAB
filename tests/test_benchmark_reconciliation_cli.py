import json
from pathlib import Path
import subprocess
import sys
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_reconciliation.py"


class ReconciliationBenchmarkCliTests(unittest.TestCase):
    def test_help_exits_without_running_the_benchmark(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--samples", result.stdout)
        self.assertIn("--sparse-bank-rows", result.stdout)
        self.assertIn("--sparse-document-rows", result.stdout)

    def test_rejects_unbounded_workload_before_benchmarking(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--sparse-bank-rows",
                "100000",
                "--sparse-document-rows",
                "100000",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("benchmark workload exceeds the supported ceiling", result.stderr)

    def test_custom_workload_and_sample_count_are_reflected_in_report(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--samples",
                "1",
                "--sparse-bank-rows",
                "25",
                "--sparse-document-rows",
                "10",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["samples"], 1)
        sparse_case = next(case for case in report["cases"] if case["case"] == "sparse_large")
        self.assertEqual((sparse_case["bankRows"], sparse_case["documentRows"]), (25, 10))
        self.assertEqual(len(sparse_case["default"]["samplesSeconds"]), 1)


if __name__ == "__main__":
    unittest.main()
