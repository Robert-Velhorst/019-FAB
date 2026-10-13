import json
import base64
import hashlib
import hmac
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from src.operations.parent_operator_session import parent_session_active, valid_validation_url


@pytest.fixture
def validator():
    state = {"calls": [], "body": {"active": True, "exp": int(time.time()) + 600}, "status": 200, "consumed": set()}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["calls"].append((self.path, self.headers.get("Authorization"), json.loads(body)))
            payload = json.loads(body)
            response = state["body"]
            if payload.get("nonce"):
                key = (payload["id"], payload["nonce"])
                if key in state["consumed"]:
                    response = {"active": False}
                else:
                    state["consumed"].add(key)
            if state.get("drip"):
                data = json.dumps(response).encode()
                headers = b"HTTP/1.0 200 OK\r\nX-Padding: " + b"a" * 120 + b"\r\n\r\n"
                first, slow = (b"", headers + data) if state["drip"] == "headers" else (headers, b" " * 120 + data)
                try:
                    self.wfile.write(first)
                    for byte in slow:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.04)
                except OSError:
                    pass
                return
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {"fab_operator_session_validation_url": f"http://127.0.0.1:{server.server_port}/api/fab/operator-session/status"}
    parent = {"id": "a" * 32, "exp": state["body"]["exp"]}
    try:
        yield config, parent, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_live_validation_tracks_revocation_without_a_positive_cache(validator):
    config, parent, state = validator
    assert parent_session_active(parent, config, "synthetic-api-token")
    state["body"] = {"active": False}
    assert not parent_session_active(parent, config, "synthetic-api-token")
    credential = base64.urlsafe_b64encode(hmac.new(b"synthetic-api-token", b"fab-managed-parent-status:v1", hashlib.sha256).digest()).decode().rstrip("=")
    assert state["calls"] == [("/api/fab/operator-session/status", f"Bearer {credential}", {"id": parent["id"]})] * 2


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_slow_stream_cannot_extend_validation_deadline(validator, phase):
    config, parent, state = validator
    state["drip"] = phase
    started = time.monotonic()
    assert not parent_session_active(parent, config, "token")
    assert time.monotonic() - started < 4.5


def test_small_clock_skew_accepts_handoff_without_extending_expiry(validator):
    config, parent, state = validator
    now = int(time.time())
    parent["exp"] = now + 905
    state["body"]["exp"] = parent["exp"]
    with patch("src.operations.parent_operator_session.time.time", return_value=now):
        assert parent_session_active(parent, config, "token", nonce="clock_skew_nonce_1234", ticket_expiry=now + 50)
        assert not parent_session_active({**parent, "exp": now + 906}, config, "token")
        assert not parent_session_active(parent, config, "token", nonce="clock_skew_nonce_5678", ticket_expiry=now + 51)
        assert not parent_session_active({**parent, "exp": now}, config, "token")


@pytest.mark.parametrize("response", [{"active": 1}, {"active": "true"}, [], {"active": True, "exp": 1}, "x" * 5_000])
def test_untrusted_response_is_not_authorization(validator, response):
    config, parent, state = validator
    state["body"] = response
    assert not parent_session_active(parent, config, "token")


def test_redirect_timeout_and_expiry_fail_closed(validator):
    config, parent, state = validator
    state["status"] = 302
    assert not parent_session_active(parent, config, "token")
    assert len(state["calls"]) == 1
    with patch("http.client.HTTPConnection.connect", side_effect=TimeoutError("private detail")):
        assert not parent_session_active(parent, config, "token")
    with patch("http.client.HTTPConnection.connect") as post:
        assert not parent_session_active({**parent, "exp": int(time.time()) - 1}, config, "token")
        assert not parent_session_active(parent, config, "")
        post.assert_not_called()


@pytest.mark.parametrize("url", ["", "http://evil.example/api/fab/operator-session/status", "https://user:secret@host/api/fab/operator-session/status", "https://host/other", "https://host/api/fab/operator-session/status?token=x", "https://host/api/fab/operator-session/status#fragment"])
def test_invalid_destination_is_rejected(url):
    assert not valid_validation_url(url, "local")


def test_only_explicit_vm_web_service_can_use_internal_plain_http():
    url = "http://web:3000/api/fab/operator-session/status"
    assert valid_validation_url(url, "vm")
    assert not valid_validation_url(url, "windows")
    assert valid_validation_url("https://fab.example/api/fab/operator-session/status", "windows")


def test_flask_browser_checks_real_http_authority_and_preserves_hai_boundary(validator, tmp_path):
    from src.operations.local_api import create_app

    config, parent, state = validator
    token = "D7pH8sAk3C5nZ9wL2tB6eQ0rV1mF4yUj"
    config = {**config, "fab_local_ledger_path": str(tmp_path / "ledger.sqlite3"),
              "fab_local_api_token": token, "fab_hai_api_token": "different-hai-secret"}
    now = int(time.time())
    payload = {"v": 2, "parent": parent, "iat": now, "exp": now + 45,
               "nonce": "restart_protected_nonce_1234", "aud": "fab-local-operator-session",
               "actor": "fab_dashboard:admin:test", "next": "/api/live"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(token.encode(), encoded.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    target = f"/operator/session/bootstrap?ticket={encoded}.{signature}"
    client = create_app(config).test_client()
    assert client.get(target).status_code == 302
    assert client.get("/api/live").status_code == 200
    assert create_app(config).test_client().get(target).status_code == 401
    before = len(state["calls"])
    headers = {"Authorization": "Bearer different-hai-secret"}
    assert client.get("/api/hai/manifest", headers=headers).status_code == 200
    assert client.get("/api/audit", headers=headers).status_code == 403
    assert len(state["calls"]) == before
    state["body"] = {"active": False}
    assert client.get("/api/live").status_code == 401
    assert client.get("/api/live", headers={"Authorization": f"Bearer {token}"}).status_code == 200
