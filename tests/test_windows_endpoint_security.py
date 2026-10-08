import json
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELLS = sorted({path for name in ("pwsh", "powershell") if (path := shutil.which(name))})


@pytest.mark.parametrize("filename", ["Start-FAB.ps1", "Stop-FAB.ps1"])
@pytest.mark.parametrize("shell", SHELLS or [None])
def test_identity_probes_are_loopback_only_and_never_follow_redirects(tmp_path, filename, shell):
    if not shell:
        pytest.skip("PowerShell unavailable")
    source = (ROOT / filename).read_text(encoding="utf-8")
    start = source.index("function Test-FabEndpoint {")
    end = source.index("\nfunction ", start + 1)
    cases = [
        ("http://127.0.0.1:5001/api/live", True),
        ("http://localhost:5001/api/live", True),
        ("http://[::1]:5001/api/live", True),
        ("https://127.0.0.1:5001/api/live", True),
        ("https://example.invalid/api/live", False),
        ("http://192.168.1.50/api/live", False),
        ("http://127.0.0.1.example.invalid/api/live", False),
        ("http://127.0.0.1@example.invalid/api/live", False),
        ("http://user:password@127.0.0.1:5001/api/live", False),
        ("ftp://127.0.0.1:5001/api/live", False),
        ("/api/live", False),
    ]
    script = r"""
    $ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
    __FUNCTION__
    $script:calls=0; $script:redirection=$null; $script:token=$null
    function Invoke-RestMethod {
        param($Uri,$UseBasicParsing,$TimeoutSec,$Headers,$MaximumRedirection)
        $script:calls++; $script:redirection=$MaximumRedirection
        $script:token=$Headers.Authorization
        [pscustomobject]@{service='synthetic-ledger'}
    }
    $cases=ConvertFrom-Json '__CASES__'
    $results=@(foreach ($case in $cases) {
        $script:calls=0; $script:redirection=$null; $script:token=$null
        $valid=Test-FabEndpoint -Url $case[0] -ExpectedService 'synthetic-ledger' -ApiToken 'synthetic-token'
        @{url=$case[0]; valid=$valid; calls=$script:calls; redirection=$script:redirection; token=$script:token}
    })
    ConvertTo-Json -InputObject $results -Compress
    """.replace("__FUNCTION__", source[start:end]).replace("__CASES__", json.dumps(cases))
    fixture = tmp_path / "probe.ps1"
    fixture.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(fixture)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    for (url, expected), actual in zip(cases, observed, strict=True):
        assert actual["valid"] is expected, url
        assert actual["calls"] == int(expected), url
        if expected:
            assert actual["redirection"] == 0, url
            assert actual["token"] == "Bearer synthetic-token"
        else:
            assert actual["token"] is None, url


@pytest.mark.parametrize("filename", ["Start-FAB.ps1", "Stop-FAB.ps1"])
@pytest.mark.parametrize("shell", SHELLS or [None])
def test_real_http_redirect_is_not_followed(tmp_path, filename, shell):
    if not shell:
        pytest.skip("PowerShell unavailable")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/target")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                body = b'{"service":"synthetic-ledger"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    source = (ROOT / filename).read_text(encoding="utf-8")
    start = source.index("function Test-FabEndpoint {")
    end = source.index("\nfunction ", start + 1)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        script = "$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest\n" + source[start:end]
        script += f"\n$direct=Test-FabEndpoint -Url '{url}/direct' -ExpectedService 'synthetic-ledger' -ApiToken 'synthetic-token'\n"
        script += f"$redirect=Test-FabEndpoint -Url '{url}/redirect' -ExpectedService 'synthetic-ledger' -ApiToken 'synthetic-token'\n"
        script += "@{direct=$direct; redirect=$redirect} | ConvertTo-Json -Compress\n"
        fixture = tmp_path / "real-probe.ps1"
        fixture.write_text(script, encoding="utf-8")
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(fixture)],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip().splitlines()[-1]) == {"direct": True, "redirect": False}
        assert requests == [("/direct", "Bearer synthetic-token"), ("/redirect", "Bearer synthetic-token")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
