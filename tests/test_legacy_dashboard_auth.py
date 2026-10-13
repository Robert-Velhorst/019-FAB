from src.dashboard.app import create_app


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
