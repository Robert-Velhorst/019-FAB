from unittest.mock import patch

import pytest

import src.run_dashboard as run_dashboard


def _config(threads=4):
    return {
        "app": {"dashboard_access_token": "test-token"},
        "dashboard": {"host": "127.0.0.1", "port": "5001", "threads": str(threads)},
        "database": {"database_path": "data/dashboard.sqlite3"},
    }


def test_legacy_dashboard_runner_uses_waitress_with_configured_listener():
    app = object()
    with (
        patch.object(run_dashboard, "ConfigLoader") as loader,
        patch.object(run_dashboard, "create_app", return_value=app),
        patch.object(run_dashboard, "serve") as serve,
    ):
        loader.return_value.get_all_config.return_value = _config(threads=6)

        run_dashboard.main()

    serve.assert_called_once_with(app, host="127.0.0.1", port=5001, threads=6)


@pytest.mark.parametrize("threads", (0, -1, 65, "invalid"))
def test_legacy_dashboard_runner_rejects_invalid_thread_counts(threads):
    with (
        patch.object(run_dashboard, "ConfigLoader") as loader,
        patch.object(run_dashboard, "create_app") as create_app,
        patch.object(run_dashboard, "serve") as serve,
    ):
        loader.return_value.get_all_config.return_value = _config(threads=threads)

        with pytest.raises(ValueError):
            run_dashboard.main()

    create_app.assert_not_called()
    serve.assert_not_called()
