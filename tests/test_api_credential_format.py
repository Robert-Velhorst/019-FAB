import http.client
import threading

import pytest
from waitress import create_server

from src.operations.deployment_preflight import deployment_preflight
from src.operations.local_api import create_app


@pytest.fixture
def client(tmp_path):
    return create_app({"fab_local_ledger_path": str(tmp_path / "ledger.sqlite3"),
                       "fab_local_api_token": "synthetic-operator-token",
                       "fab_hai_api_token": "synthetic-hai-token"}).test_client()


@pytest.mark.parametrize("value", ["Bearer caf\u00e9", "Bearer \u2603", "Bearer " + "x" * 9000])
def test_malformed_authorization_is_denied_without_server_error(client, value):
    response = client.get("/api/live", headers={"Authorization": value})
    assert response.status_code == 401
    assert response.get_json()["error"] == "Unauthorized"


@pytest.mark.parametrize("value", ["caf\u00e9", "\u2603", "x" * 9000])
def test_malformed_form_token_is_denied_without_server_error(client, value):
    response = client.post("/login", data={"token": value})
    assert response.status_code == 401


@pytest.mark.parametrize("key", ["fab_local_api_token", "fab_hai_api_token"])
@pytest.mark.parametrize("value", ["caf\u00e9", "bad token", "x" * 8193])
def test_bad_configured_credentials_fail_before_ledger_creation(tmp_path, key, value):
    ledger = tmp_path / "ledger.sqlite3"
    config = {"fab_local_ledger_path": str(ledger), "fab_local_api_token": "synthetic-operator-token", key: value}
    report = deployment_preflight(config)
    assert report["status"] == "blocked"
    with pytest.raises(ValueError) as error:
        create_app(config)
    assert value not in str(error.value)
    assert not ledger.exists()


def test_real_waitress_http_preserves_denial_operator_and_hai_scope(client):
    server = create_server(client.application, host="127.0.0.1", port=0, threads=1, map={})
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        cases = [("/api/live", "Bearer caf\u00e9", 401),
                 ("/api/live", "Bearer synthetic-operator-token", 200),
                 ("/api/hai/manifest", "Bearer synthetic-hai-token", 200),
                 ("/api/health", "Bearer synthetic-hai-token", 403)]
        for path, authorization, status in cases:
            connection = http.client.HTTPConnection("127.0.0.1", int(server.effective_port), timeout=5)
            try:
                connection.request("GET", path, headers={"Authorization": authorization, "Connection": "close"})
                response = connection.getresponse()
                assert response.status == status
                response.read()
            finally:
                connection.close()
    finally:
        server.task_dispatcher.shutdown(cancel_pending=True, timeout=5)
        server.close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "Disposable Waitress HTTP server did not stop"


@pytest.mark.parametrize("size", [1, 8192])
def test_local_ascii_credentials_remain_valid_at_size_boundaries(tmp_path, size):
    token = "x" * size
    client = create_app({"fab_local_ledger_path": str(tmp_path / "ledger.sqlite3"),
                         "fab_local_api_token": token}).test_client()
    assert client.get("/api/live", headers={"Authorization": "Bearer " + token}).status_code == 200
