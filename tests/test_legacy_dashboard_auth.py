import json
from unittest.mock import patch

from src.dashboard.app import create_app
from src.storage.database import Database


def _client(tmp_path, token="test-dashboard-token", session_secret="test-session-secret-that-is-long-enough"):
    app = create_app({
        "database_path": str(tmp_path / "dashboard.sqlite3"),
        "dashboard_access_token": token,
        "dashboard_session_secret": session_secret,
    })
    app.config.update(TESTING=True)
    return app.test_client()


def test_dashboard_token_is_never_accepted_or_rendered_in_a_url(tmp_path):
    client = _client(tmp_path)

    assert client.get("/documents?token=test-dashboard-token").status_code == 400
    assert client.get("/documents").status_code == 401

    login_page = client.get("/ui")
    assert login_page.status_code == 200
    assert b'action="/login"' in login_page.data
    assert b"test-dashboard-token" not in login_page.data

    login = client.post("/login", data={"token": "test-dashboard-token"})
    assert login.status_code == 303
    assert login.headers["Location"] == "/ui"

    dashboard = client.get("/ui")
    assert dashboard.status_code == 200
    assert b"?token=" not in dashboard.data
    assert b"test-dashboard-token" not in dashboard.data
    assert b"/documents" in dashboard.data

    documents = client.get("/documents")
    assert documents.status_code == 200
    assert documents.headers["Referrer-Policy"] == "no-referrer"
    assert documents.headers["Cache-Control"] == "no-store"


def test_every_nonpublic_dashboard_get_route_requires_authentication(tmp_path):
    app = create_app({
        "database_path": str(tmp_path / "route-auth.sqlite3"),
        "dashboard_access_token": "test-dashboard-token",
        "dashboard_session_secret": "test-session-secret-that-is-long-enough",
    })
    app.config.update(TESTING=True)
    client = app.test_client()
    public_endpoints = {"health", "operator_ui", "static"}
    checked = []

    for rule in app.url_map.iter_rules():
        if "GET" not in (rule.methods or ()) or rule.endpoint in public_endpoints:
            continue
        values = {}
        for name, converter in rule._converters.items():
            converter_name = type(converter).__name__.lower()
            values[name] = 1 if "integer" in converter_name or "float" in converter_name else "auth-probe"
        _, path = rule.build(values)

        response = client.get(path)
        assert response.status_code == 401, f"Unauthenticated GET was not denied: {path}"
        assert response.headers["Cache-Control"] == "no-store"
        checked.append(path)

    assert checked
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json() == {"service": "FAB local dashboard", "status": "ok"}


def test_session_auth_requires_csrf_for_writes_but_header_token_does_not(tmp_path):
    client = _client(tmp_path)
    client.post("/login", data={"token": "test-dashboard-token"})

    assert client.post("/category-rules", json={}).status_code == 403

    with client.session_transaction() as session:
        csrf_token = session["dashboard_csrf_token"]
    session_write = client.post(
        "/category-rules",
        json={"rule_name": "test rule", "category": "office", "pattern": "fixture"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert session_write.status_code < 500

    header_client = _client(tmp_path / "header")
    api_write = header_client.post(
        "/category-rules",
        json={"rule_name": "api rule", "category": "office", "pattern": "fixture"},
        headers={"X-FAB-Token": "test-dashboard-token"},
    )
    assert api_write.status_code < 500


def test_dashboard_cookie_is_http_only_and_same_site(tmp_path):
    client = _client(tmp_path)
    login = client.post("/login", data={"token": "test-dashboard-token"})

    cookie = login.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_secure_cookie_setting_parses_boolean_config_strings(tmp_path):
    app = create_app({
        "database_path": str(tmp_path / "secure-dashboard.sqlite3"),
        "dashboard_access_token": "test-dashboard-token",
        "dashboard_cookie_secure": "true",
    })

    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_rotating_dashboard_token_revokes_existing_browser_sessions(tmp_path):
    secret = "stable-session-secret-for-token-rotation"
    client = _client(tmp_path, session_secret=secret)
    client.post("/login", data={"token": "test-dashboard-token"})
    session_cookie = client.get_cookie("session")
    assert session_cookie is not None

    rotated_client = _client(
        tmp_path,
        token="rotated-dashboard-token",
        session_secret=secret,
    )
    rotated_client.set_cookie("session", session_cookie.value)

    assert rotated_client.get("/documents").status_code == 401


def test_logout_revokes_a_copied_session_cookie_across_app_restarts(tmp_path):
    secret = "stable-session-secret-for-logout-revocation"
    original = _client(tmp_path, session_secret=secret)
    login = original.post("/login", data={"token": "test-dashboard-token"})
    assert login.status_code == 303
    copied_cookie = original.get_cookie("session")
    assert copied_cookie is not None
    with original.session_transaction() as session:
        csrf_token = session["dashboard_csrf_token"]

    replay = _client(tmp_path, session_secret=secret)
    replay.set_cookie("session", copied_cookie.value)
    assert replay.get("/documents").status_code == 200

    logout = original.post("/logout", data={"csrf_token": csrf_token})
    assert logout.status_code == 303
    assert replay.get("/documents").status_code == 401

    after_restart = _client(tmp_path, session_secret=secret)
    after_restart.set_cookie("session", copied_cookie.value)
    assert after_restart.get("/documents").status_code == 401


def test_dashboard_login_throttles_peer_and_ignores_forwarded_ip_headers(tmp_path):
    client = _client(tmp_path)
    now = [100.0]

    with patch("src.dashboard.app.time.monotonic", side_effect=lambda: now[0]):
        for attempt in range(5):
            response = client.post(
                "/login",
                data={"token": "incorrect-token"},
                headers={"X-Forwarded-For": f"198.51.100.{attempt + 1}"},
            )
            assert response.status_code == 401

        blocked = client.post(
            "/login",
            data={"token": "test-dashboard-token"},
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) > 0

        now[0] += 15 * 60 + 1
        recovered = client.post("/login", data={"token": "test-dashboard-token"})
        assert recovered.status_code == 303


def test_backup_restore_requires_boolean_overwrite_flag(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/backups/restore",
        json={
            "backup_path": str(tmp_path / "backup.zip"),
            "restore_dir": "restore_preview",
            "allow_overwrite": "false",
        },
        headers={"X-FAB-Token": "test-dashboard-token"},
    )

    assert response.status_code == 400


def test_backup_routes_reject_non_object_json(tmp_path):
    client = _client(tmp_path)
    headers = {"X-FAB-Token": "test-dashboard-token"}

    assert client.post("/backups", json=["C:/private.txt"], headers=headers).status_code == 400
    assert client.post("/backups/restore", json=["archive.zip"], headers=headers).status_code == 400


def test_backup_audit_records_do_not_persist_filesystem_paths(tmp_path):
    source_dir = tmp_path / "private-sort-out"
    source_dir.mkdir()
    (source_dir / "receipt.pdf").write_bytes(b"synthetic receipt fixture")
    database_path = tmp_path / "dashboard.sqlite3"
    app = create_app({
        "database_path": str(database_path),
        "backup_base_dir": str(tmp_path / "backups"),
        "local_input_dir": str(source_dir),
        "dashboard_access_token": "test-dashboard-token",
        "dashboard_session_secret": "test-session-secret-that-is-long-enough",
    })
    app.config.update(TESTING=True)
    client = app.test_client()
    headers = {"X-FAB-Token": "test-dashboard-token"}

    created = client.post(
        "/backups",
        json={"paths": [str(source_dir)], "backup_config": {"type": "zip"}},
        headers=headers,
    )
    assert created.status_code == 200
    backup_path = created.get_json()["path"]

    restored = client.post(
        "/backups/restore",
        json={"backup_path": backup_path, "restore_dir": "restore_preview"},
        headers=headers,
    )
    assert restored.status_code == 200
    assert restored.get_json()["status"] == "success"
    restored_file = tmp_path / "backups" / "restore" / "restore_preview" / source_dir.name / "receipt.pdf"
    assert restored_file.read_bytes() == b"synthetic receipt fixture"

    with patch(
        "src.dashboard.app.BackupManager.perform_backup",
        return_value={"status": "failure", "message": str(source_dir)},
    ):
        failed = client.post("/backups", json={}, headers=headers)
    assert failed.status_code == 200
    assert failed.get_json()["message"] == str(source_dir)

    audit_rows = Database({"database_path": str(database_path)}).fetch_all(
        "SELECT entity_id, before_json, after_json, reason FROM audit_log WHERE entity_type = 'backup'"
    )
    assert len(audit_rows) == 3
    assert [row["entity_id"] for row in audit_rows] == ["archive", "restore", "archive"]
    assert json.loads(audit_rows[0]["after_json"]) == {
        "included_source_count": 1,
        "status": "success",
    }
    assert json.loads(audit_rows[1]["after_json"]) == {"status": "success"}
    assert json.loads(audit_rows[2]["after_json"]) == {"status": "failure"}

    def assert_no_paths(value):
        if isinstance(value, str):
            assert str(tmp_path) not in value
            assert backup_path not in value
            assert "receipt.pdf" not in value
        elif isinstance(value, dict):
            for nested in value.values():
                assert_no_paths(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_no_paths(nested)

    for row in audit_rows:
        assert_no_paths(row["entity_id"])
        assert_no_paths(json.loads(row["before_json"]))
        assert_no_paths(json.loads(row["after_json"]))
        assert_no_paths(row["reason"])


def test_write_routes_reject_falsy_non_object_json_without_mutating_state(tmp_path):
    client = _client(tmp_path)
    headers = {"X-FAB-Token": "test-dashboard-token"}

    for payload in (None, False, 0, [], ""):
        response = client.post(
            "/category-rules",
            data=json.dumps(payload),
            content_type="application/json",
            headers=headers,
        )
        assert response.status_code == 400

    assert client.get("/category-rules", headers=headers).get_json() == []


def test_manual_review_resolution_is_pending_only_and_audited(tmp_path):
    client = _client(tmp_path)
    headers = {"X-FAB-Token": "test-dashboard-token"}
    database = Database({"database_path": str(tmp_path / "dashboard.sqlite3")})
    database.add_manual_review_item("doc-1", "needs_review")
    review = database.fetch_one("SELECT * FROM manual_review_items WHERE document_id = ?", ("doc-1",))
    item_id = review["id"]

    invalid_transition = client.post(
        f"/manual-review/{item_id}/resolve",
        json={"status": "pending", "resolution": "try reset"},
        headers=headers,
    )
    assert invalid_transition.status_code == 400
    assert database.fetch_one("SELECT status FROM manual_review_items WHERE id = ?", (item_id,))["status"] == "pending"

    resolved = client.post(
        f"/manual-review/{item_id}/resolve",
        json={"resolution": "Reviewed and resolved"},
        headers=headers,
    )
    assert resolved.status_code == 200
    assert resolved.get_json() == {"status": "resolved", "item_id": item_id}
    assert database.fetch_one("SELECT status FROM manual_review_items WHERE id = ?", (item_id,))["status"] == "resolved"

    duplicate = client.post(f"/manual-review/{item_id}/resolve", json={}, headers=headers)
    assert duplicate.status_code == 409
    assert client.post("/manual-review/99999/resolve", json={}, headers=headers).status_code == 404


def test_dashboard_routes_validate_nested_corrections_rules_and_query_filters(tmp_path):
    client = _client(tmp_path)
    headers = {"X-FAB-Token": "test-dashboard-token"}

    assert client.post(
        "/documents/doc-1/corrections",
        json={"correction": {"extracted_data": []}},
        headers=headers,
    ).status_code == 400
    assert client.post("/category-rules", json={}, headers=headers).status_code == 400
    assert client.get("/category-rules", headers=headers).get_json() == []
    assert client.get("/exceptions?active=invalid", headers=headers).status_code == 400


def test_supervised_completion_rejects_malformed_evidence_before_state_change(tmp_path):
    client = _client(tmp_path)
    headers = {"X-FAB-Token": "test-dashboard-token"}
    database = Database({"database_path": str(tmp_path / "dashboard.sqlite3")})
    database.upsert_document({"id": "doc-1", "source": "manual", "original_filename": "receipt.pdf"})
    now = database.now()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO posting_attempts (document_id, target_system, status, idempotency_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("doc-1", "waveapps_business", "supervision_required", "attempt-1", now, now),
        )
    attempt_id = database.fetch_one("SELECT id FROM posting_attempts WHERE idempotency_key = ?", ("attempt-1",))["id"]
    payload = {
        "confirmation": "CONFIRM EXTERNAL SUBMISSION",
        "external_id": {"unexpected": "object"},
        "evidence": [],
    }

    response = client.post(
        f"/posting-attempts/{attempt_id}/complete-supervised",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 400
    assert database.fetch_one("SELECT status, external_id FROM posting_attempts WHERE id = ?", (attempt_id,)) == {
        "status": "supervision_required",
        "external_id": None,
    }


def test_legacy_dashboard_rejects_oversized_json_body(tmp_path):
    client = _client(tmp_path)
    response = client.post(
        "/category-rules",
        data=b'{"pattern":"' + b"x" * (2 * 1024 * 1024) + b'"}',
        content_type="application/json",
        headers={"X-FAB-Token": "test-dashboard-token"},
    )

    assert response.status_code == 413
    assert client.get("/category-rules", headers={"X-FAB-Token": "test-dashboard-token"}).get_json() == []
