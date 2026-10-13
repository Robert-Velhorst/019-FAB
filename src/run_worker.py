import os
import sys
from pathlib import Path
from typing import Optional

from src.config_loader import ConfigLoader
from src.operations.deployment_preflight import require_deployment_ready
from src.worker.scheduler import FabWorker
from src.worker.runtime import WorkerAlreadyRunningError, managed_worker_runtime


def run_worker(
    *,
    project_root: Optional[Path] = None,
    run_once: Optional[bool] = None,
) -> None:
    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    previous_directory = Path.cwd()
    os.chdir(root)
    try:
        config = ConfigLoader(config_file="config/config.ini").get_all_config()
        require_deployment_ready(config)
        with managed_worker_runtime(root):
            if run_once is not None:
                config = {**config, "worker_run_once": bool(run_once)}
            FabWorker(config).run()
    finally:
        os.chdir(previous_directory)


def main(*, run_once: Optional[bool] = None) -> int:
    try:
        run_worker(run_once=run_once)
    except WorkerAlreadyRunningError as exc:
        print(f"FAB worker not started: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
