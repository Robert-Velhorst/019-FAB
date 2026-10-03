from src import run_health_probe


def test_api_probe_reads_secret_file_and_disables_proxy(monkeypatch, tmp_path):
    secret = tmp_path / "synthetic-token"
    secret.write_text("synthetic-probe-token\n", encoding="utf-8")
    monkeypatch.delenv("FAB_LOCAL_API_TOKEN", raising=False)
    monkeypatch.setenv("FAB_LOCAL_API_TOKEN_FILE", str(secret))
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Opener:
        def open(self, request, timeout):
            seen["url"] = request.full_url
            seen["authorization"] = request.get_header("Authorization")
            seen["timeout"] = timeout
            return Response()

    def opener(handler):
        seen["proxies"] = handler.proxies
        return Opener()

    monkeypatch.setattr(run_health_probe, "build_opener", opener)
    assert run_health_probe.probe_api()
    assert seen == {"url": "http://127.0.0.1:5001/api/live",
                    "authorization": "Bearer synthetic-probe-token", "timeout": 5, "proxies": {}}


def test_probe_failure_does_not_print_secrets(monkeypatch, capsys):
    monkeypatch.setattr(run_health_probe.sys, "argv", ["probe", "api"])

    def failure():
        raise ValueError("synthetic-private-secret")

    monkeypatch.setattr(run_health_probe, "probe_api", failure)
    assert run_health_probe.main() == 1
    output = capsys.readouterr()
    assert "synthetic-private-secret" not in output.err + output.out
    assert "FAB health probe failed" in output.err
