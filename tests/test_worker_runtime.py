import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import run_worker as worker_entrypoint
from src.utils.runtime_identity import local_instance_id
from src.worker.runtime import (
    WorkerAlreadyRunningError,
    managed_worker_maintenance,
    managed_worker_runtime,
)
from src.worker.scheduler import FabWorker


class TestWorkerRuntime(unittest.TestCase):
    def test_invalid_worker_interval_cannot_create_a_hot_loop(self):
        worker = FabWorker({
            "worker_interval_seconds": 0,
            "fab_local_ledger_enabled": False,
            "worker_process_legacy_postings": False,
        })

        self.assertEqual(worker.interval_seconds, 1)
        with patch("src.worker.scheduler.time.sleep") as sleep:
            worker._sleep_until_next_cycle()

        sleep.assert_called_once_with(1)

    def test_worker_interval_is_bounded_and_nonfinite_values_fall_back(self):
        config = {
            "fab_local_ledger_enabled": False,
            "worker_process_legacy_postings": False,
        }
        for value, expected in (
            (-5, 1),
            (1000000, 86400),
            ("invalid", 300),
            (float("inf"), 300),
            (float("nan"), 300),
        ):
            with self.subTest(value=value):
                worker = FabWorker({**config, "worker_interval_seconds": value})
                self.assertEqual(worker.interval_seconds, expected)

    def test_runtime_descriptor_tracks_the_owned_process_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = root / "data" / "fab-worker-runtime.json"

            with managed_worker_runtime(root) as runtime:
                saved = json.loads(runtime_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["service"], "fab-autonomous-worker")
                self.assertEqual(saved["pid"], os.getpid())
                self.assertEqual(saved["instanceRoot"], str(root.resolve()))
                self.assertEqual(saved["instanceId"], local_instance_id(root))
                self.assertEqual(saved, runtime)

            self.assertFalse(runtime_path.exists())

    def test_runtime_lock_rejects_a_second_worker_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from src.worker.runtime import managed_worker_runtime\n"
                "with managed_worker_runtime(Path(sys.argv[1])):\n"
                " print('ready', flush=True)\n"
                " time.sleep(30)\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", script, temp_dir],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaises(WorkerAlreadyRunningError):
                    with managed_worker_runtime(Path(temp_dir)):
                        self.fail("A second worker unexpectedly acquired the runtime lock.")
            finally:
                child.terminate()
                child.wait(timeout=10)
                if child.stdout:
                    child.stdout.close()
                if child.stderr:
                    child.stderr.close()

    def test_maintenance_lock_rejects_an_active_worker_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from src.worker.runtime import managed_worker_runtime\n"
                "with managed_worker_runtime(Path(sys.argv[1])):\n"
                " print('ready', flush=True)\n"
                " time.sleep(30)\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", script, temp_dir],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaisesRegex(WorkerAlreadyRunningError, "stop it before maintenance"):
                    with managed_worker_maintenance(Path(temp_dir)):
                        self.fail("Maintenance unexpectedly acquired an active worker runtime.")
            finally:
                child.terminate()
                child.wait(timeout=10)
                if child.stdout:
                    child.stdout.close()
                if child.stderr:
                    child.stderr.close()

    def test_worker_process_rejects_an_active_maintenance_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_path = root / "data" / "fab-worker-runtime.json"
            script = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from src.worker.runtime import managed_worker_maintenance\n"
                "with managed_worker_maintenance(Path(sys.argv[1])):\n"
                " print('ready', flush=True)\n"
                " time.sleep(30)\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", script, temp_dir],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                self.assertFalse(runtime_path.exists())
                with self.assertRaises(WorkerAlreadyRunningError):
                    with managed_worker_runtime(root):
                        self.fail("Worker unexpectedly acquired the maintenance runtime.")
            finally:
                child.terminate()
                child.wait(timeout=10)
                if child.stdout:
                    child.stdout.close()
                if child.stderr:
                    child.stderr.close()

    def test_one_shot_runner_uses_owned_runtime_and_preserves_loaded_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_directory = Path.cwd()
            loaded_config = {"worker_run_once": False}
            worker = MagicMock()

            with patch.object(
                worker_entrypoint.ConfigLoader,
                "get_all_config",
                return_value=loaded_config,
            ), patch.object(worker_entrypoint, "FabWorker", return_value=worker) as worker_type:
                worker_entrypoint.run_worker(project_root=root, run_once=True)

            configured = worker_type.call_args.args[0]
            self.assertTrue(configured["worker_run_once"])
            self.assertFalse(loaded_config["worker_run_once"])
            worker.run.assert_called_once_with()
            self.assertEqual(Path.cwd(), original_directory)
            self.assertFalse((root / "data" / "fab-worker-runtime.json").exists())

    def test_cli_returns_failure_without_traceback_when_worker_is_owned(self):
        stderr = StringIO()
        with patch.object(
            worker_entrypoint,
            "run_worker",
            side_effect=WorkerAlreadyRunningError("already owned"),
        ), redirect_stderr(stderr):
            exit_code = worker_entrypoint.main(run_once=True)

        self.assertEqual(exit_code, 1)
        self.assertIn("FAB worker not started: already owned", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
