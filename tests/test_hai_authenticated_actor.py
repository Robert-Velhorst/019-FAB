from unittest.mock import patch

import pytest

from src.operations.local_api import create_app
from src.operations.local_ledger import LocalOperationsLedger


def fixture(tmp_path, mode):
    path = str(tmp_path / "ledger.sqlite3")
    config = {"fab_local_ledger_path": path, "fab_hai_connector_enabled": True,
              "fab_hai_allowed_commands": "refresh_notifications"}
    if mode != "loopback":
        config.update(fab_local_api_token="synthetic-operator", fab_hai_api_token="synthetic-hai")
    client = create_app(config).test_client()
    headers = {}
    if mode in {"hai", "operator"}:
        headers["Authorization"] = "Bearer synthetic-" + mode
    if mode in {"session", "hai_with_session"}:
        with client.session_transaction() as session:
            session["fab_local_api_authenticated"] = True
            session["fab_local_api_auth_method"] = "token"
        if mode == "hai_with_session":
            headers["Authorization"] = "Bearer synthetic-hai"
    principal = "hai" if mode.startswith("hai") else "loopback" if mode == "loopback" else "operator"
    return client, headers, LocalOperationsLedger(path), "fab_hai_api:" + principal


@pytest.mark.parametrize("mode", ["hai", "operator", "session", "hai_with_session", "loopback"])
@pytest.mark.parametrize("fail", [False, True], ids=["completed", "failed"])
def test_http_actor_comes_from_verified_credential_class_not_body(tmp_path, mode, fail):
    client, headers, ledger, expected = fixture(tmp_path, mode)
    actors = []

    def refresh(*, actor):
        actors.append(actor)
        if fail:
            raise RuntimeError("Synthetic executor failure")
        return {"status": "refreshed"}

    body = {"requestId": "synthetic-actor", "commandId": "refresh_notifications",
            "actor": "fab_dashboard:admin:impersonated"}
    with patch("src.operations.local_api.LocalNotificationService.refresh", side_effect=refresh):
        result = client.post("/api/hai/commands/execute", headers=headers, json=body)
        assert result.status_code == (500 if fail else 200)
        for action in ("requested", "failed" if fail else "completed"):
            event = ledger.find_audit_event("hai.command." + action, "hai_command_request", "synthetic-actor")
            assert event["details"]["actor"] == expected
        body["actor"] = {"role": "administrator"}
        replay = client.post("/api/hai/commands/execute", headers=headers, json=body)
        assert replay.status_code == (409 if fail else 200)
        assert replay.get_json()["status"] == ("previously_failed" if fail else "already_executed")
    assert actors == [expected]


def test_unverified_actor_body_cannot_authenticate_or_create_command_audit(tmp_path):
    client, _, ledger, _ = fixture(tmp_path, "operator")
    with patch("src.operations.local_api.LocalNotificationService.refresh") as refresh:
        result = client.post("/api/hai/commands/execute", json={"requestId": "synthetic-unverified",
            "commandId": "refresh_notifications", "actor": "fab_hai_api:operator"})
    assert result.status_code == 401
    refresh.assert_not_called()
    assert ledger.find_audit_event("hai.command.requested", "hai_command_request", "synthetic-unverified") is None
