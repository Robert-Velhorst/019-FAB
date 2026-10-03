import base64
import io
import json

import pytest
from werkzeug.test import EnvironBuilder

from src.operations.local_api import create_app
from src.operations.local_ledger import LocalOperationsLedger


LIMIT = 5 * 1024 * 1024


class CountingInput(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.bytes_read = 0

    def read(self, size=-1):
        data = super().read(size)
        self.bytes_read += len(data)
        return data

    def readinto(self, buffer):
        count = super().readinto(buffer)
        self.bytes_read += count
        return count


@pytest.fixture
def app(tmp_path):
    return create_app({"fab_local_ledger_path": str(tmp_path / "ledger.sqlite3")})


def padded_body(size):
    prefix = b'{"bankTransactions":[],"padding":"'
    suffix = b'"}'
    return prefix + b'x' * (size - len(prefix) - len(suffix)) + suffix


def ledger_state(app):
    ledger = LocalOperationsLedger(app.config["FAB_LOCAL_LEDGER_PATH"])
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {row[0]: list(map(tuple, connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        ))) for row in tables}


@pytest.mark.parametrize("known_length", [True, False])
def test_oversize_reconciliation_rejected_before_json_decode(app, monkeypatch, known_length):
    before = ledger_state(app)
    stream = CountingInput(padded_body(LIMIT + 1))
    builder = EnvironBuilder(path="/api/reconciliation/run", method="POST",
                             input_stream=stream, content_type="application/json")
    environ = builder.get_environ()
    if not known_length:
        environ.pop("CONTENT_LENGTH", None)
        environ["wsgi.input_terminated"] = True
    calls = []
    original = app.json.loads

    def tracked_loads(data, **kwargs):
        # Flask also decodes its small signed session cookie through this provider.
        if len(data) > 1000:
            calls.append(len(data))
        return original(data, **kwargs)

    monkeypatch.setattr(app.json, "loads", tracked_loads)
    response = app.test_client().open(environ)
    assert response.status_code == 413
    assert calls == []
    assert stream.bytes_read <= (0 if known_length else LIMIT)
    assert ledger_state(app) == before


def test_exact_reconciliation_request_limit_is_allowed(app):
    response = app.test_client().post("/api/reconciliation/run", data=padded_body(LIMIT),
                                      content_type="application/json")
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/api/reconciliation/run", "/api/bank-transactions/import"])
def test_deep_json_is_client_error_without_exception_audit(app, path):
    before = ledger_state(app)
    payload = '{"bankTransactions":[],"nested":' + '[' * 3000 + '0' + ']' * 3000 + '}'
    response = app.test_client().post(path, data=payload, content_type="application/json")
    assert response.status_code == 400
    assert ledger_state(app) == before


def test_deep_form_json_returns_reviewable_error(app):
    before = ledger_state(app)
    client = app.test_client()
    response = client.post("/reconciliation/run", data={"bankTransactionsJson": '[' * 3000 + '0' + ']' * 3000})
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["fab_last_reconciliation_summary"]["error"]
    assert ledger_state(app) == before


def test_stricter_global_limit_is_preserved(tmp_path):
    app = create_app({"fab_local_ledger_path": str(tmp_path / "ledger.sqlite3"),
                      "fab_local_api_max_request_bytes": 1024})
    before = ledger_state(app)
    response = app.test_client().post("/api/reconciliation/run", data=padded_body(1025),
                                      content_type="application/json")
    assert response.status_code == 413
    assert ledger_state(app) == before


@pytest.mark.parametrize("headers,status", [({}, 401),
    ({"Authorization": "Bearer synthetic-hai-token"}, 403),
    ({"Authorization": "Bearer synthetic-operator-token", "Origin": "https://untrusted.example"}, 403)])
def test_authentication_and_origin_guards_run_before_body_reads(tmp_path, headers, status):
    app = create_app({"fab_local_ledger_path": str(tmp_path / "ledger.sqlite3"),
                      "fab_local_api_token": "synthetic-operator-token",
                      "fab_hai_api_token": "synthetic-hai-token"})
    stream = CountingInput(padded_body(LIMIT + 1))
    response = app.test_client().open(EnvironBuilder(path="/api/reconciliation/run", method="POST",
        input_stream=stream, content_type="application/json", headers=headers).get_environ())
    assert response.status_code == status
    assert stream.bytes_read == 0


def test_large_valid_bank_file_keeps_upload_allowance(app):
    raw = json.dumps([{"id": "synthetic", "date": "2026-09-30", "amount": -4.28}]).encode()
    raw += b' ' * (4 * 1024 * 1024 - len(raw))
    payload = {"filename": "synthetic.json", "format": "json", "accountIdentifier": "synthetic",
               "contentBase64": base64.b64encode(raw).decode("ascii")}
    assert len(json.dumps(payload)) > LIMIT
    response = app.test_client().post("/api/bank-transactions/import", json=payload)
    assert response.status_code == 200
    assert response.get_json()["rowsImported"] == 1
