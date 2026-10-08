import unittest
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestWindowsLauncher(unittest.TestCase):
    def test_production_runtime_cannot_reuse_an_unverified_local_ledger(self):
        script = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")
        self.assertIn('deploymentStorageId = $deploymentStorageId', script)
        self.assertIn('deploymentProfile = $deploymentProfile', script)
        self.assertIn('$env:FAB_DEPLOYMENT_PROFILE = $deploymentProfile', script)
        self.assertIn('production storage configuration changed', script)
        self.assertIn('cannot adopt an unverified running API', script)
        self.assertIn('cannot adopt an unverified running worker', script)

    def test_preflight_precedes_every_service_start(self):
        script = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")
        self.assertIn("src.run_deployment_preflight", script)
        self.assertLess(script.index("src.run_deployment_preflight"), script.index("$apiProcess ="))
        self.assertIn('FAB_DEPLOYMENT_PROFILE', script)
        self.assertIn('$deploymentProfile -ne "local"', script)
        for process in ("apiProcess", "workerProcess", "webProcess"):
            self.assertIn(f"${process} = Invoke-FabWithServiceCredentials", script)
        self.assertIn('if ($LASTEXITCODE -ne 0)', script[script.index('src.run_deployment_preflight'):])
        self.assertLess(script.index('The configured FAB API port is occupied'), script.index('$apiProcess = Invoke-FabWithServiceCredentials'))
        self.assertLess(script.index('The configured FAB dashboard port is occupied'), script.index('$apiProcess = Invoke-FabWithServiceCredentials'))

    def test_production_rejects_dashboard_port_changes_before_start(self):
        script = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")
        self.assertIn('The saved FAB dashboard uses a different production port', script)

    def test_all_windows_scripts_parse_without_running_them(self):
        shells = {path for name in ("pwsh", "powershell") if (path := shutil.which(name))}
        if not shells:
            self.skipTest("PowerShell is unavailable")
        for shell in shells:
            for name in ("Start-FAB.ps1", "Stop-FAB.ps1", "Start-FAB-Ngrok.ps1", "Test-FAB-Ngrok.ps1", "scripts/Windows-Profile.ps1", "scripts/Windows-Job.ps1", "scripts/Windows-Process.ps1"):
                path = str(ROOT / name).replace("'", "''")
                command = "$tokens=$null; $errors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('" + path + "',[ref]$tokens,[ref]$errors); if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }"
                result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_secret_file_resolution_is_used_by_start_and_stop(self):
        for filename in ("Start-FAB.ps1", "Stop-FAB.ps1"):
            script = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("c.get('fab_local_api_token'", script)

    def test_credential_scope_restores_environment_on_failure(self):
        helper = ROOT / "scripts" / "Windows-Profile.ps1"
        self.assertTrue(helper.is_file(), "Missing scoped Windows profile helper")
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is unavailable")
        command = r"""
        $ErrorActionPreference = 'Stop'
        . '__HELPER__'
        $env:FAB_LOCAL_API_TOKEN = 'original-api'
        $env:FAB_LOCAL_API_TOKEN_FILE = 'original-file'
        $env:FAB_HAI_API_TOKEN = 'original-hai'
        $env:FAB_HAI_API_TOKEN_FILE = 'original-hai-file'
        $env:JWT_SECRET = 'original-jwt'
        $env:JWT_SECRET_FILE = 'original-jwt-file'
        try {
            Invoke-FabWithServiceCredentials -ApiToken 'synthetic-api' -HaiApiToken 'synthetic-hai' -JwtSecret 'synthetic-jwt' -Action {
                if ($env:FAB_LOCAL_API_TOKEN -ne 'synthetic-api') { throw 'wrong-api' }
                if ($env:FAB_HAI_API_TOKEN -ne 'synthetic-hai') { throw 'wrong-hai' }
                if ($env:FAB_LOCAL_API_TOKEN_FILE -or $env:FAB_HAI_API_TOKEN_FILE) { throw 'file-conflict' }
                if ($env:JWT_SECRET -ne 'synthetic-jwt' -or $env:JWT_SECRET_FILE) { throw 'jwt-file-conflict' }
                throw 'expected-failure'
            }
        } catch { if ($_.Exception.Message -ne 'expected-failure') { throw } }
        if ($env:FAB_LOCAL_API_TOKEN -ne 'original-api') { throw 'leaked-api' }
        if ($env:FAB_LOCAL_API_TOKEN_FILE -ne 'original-file') { throw 'lost-file' }
        if ($env:FAB_HAI_API_TOKEN -ne 'original-hai') { throw 'leaked-hai' }
        if ($env:FAB_HAI_API_TOKEN_FILE -ne 'original-hai-file') { throw 'lost-hai-file' }
        if ($env:JWT_SECRET -ne 'original-jwt' -or $env:JWT_SECRET_FILE -ne 'original-jwt-file') { throw 'lost-jwt' }
        Remove-Item Env:JWT_SECRET, Env:JWT_SECRET_FILE
        Remove-Item Env:FAB_LOCAL_API_TOKEN, Env:FAB_LOCAL_API_TOKEN_FILE, Env:FAB_HAI_API_TOKEN, Env:FAB_HAI_API_TOKEN_FILE
        $result = Invoke-FabWithServiceCredentials -ApiToken 'synthetic-api' -HaiApiToken 'synthetic-hai' -JwtSecret 'synthetic-jwt' -Action { 'ok' }
        if ($result -ne 'ok' -or $env:FAB_LOCAL_API_TOKEN -or $env:FAB_HAI_API_TOKEN -or $env:JWT_SECRET) { throw 'scope-not-clean' }
        'verified'
        """.replace("__HELPER__", str(helper).replace("'", "''"))
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "verified")

    def test_launcher_provisions_and_scopes_dashboard_signing_secret(self):
        script = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")

        self.assertIn("get_or_create_runtime_secret('web_jwt_secret')", script)
        self.assertIn("get_or_create_runtime_secret('operator_api_token')", script)
        self.assertIn("get_or_create_runtime_secret('hai_api_token')", script)
        self.assertIn("$env:FAB_LOCAL_API_TOKEN = $apiToken", script)
        self.assertIn("$env:FAB_HAI_API_TOKEN = $haiApiToken", script)
        self.assertIn("$previousHaiApiToken = $env:FAB_HAI_API_TOKEN", script)
        self.assertIn("Remove-Item Env:FAB_HAI_API_TOKEN", script)
        self.assertIn("$env:JWT_SECRET = $webJwtSecret", script)
        self.assertIn("$previousJwtSecret = $env:JWT_SECRET", script)
        self.assertIn("Remove-Item Env:JWT_SECRET", script)
        self.assertIn("read_secret('JWT_SECRET')", script)
        self.assertIn('-JwtSecret $webJwtSecret', script)
        self.assertIn("$env:FAB_LOCAL_API_PUBLIC_URL = $apiBaseUrl", script)
        self.assertIn("$previousLocalApiPublicUrl = $env:FAB_LOCAL_API_PUBLIC_URL", script)
        self.assertIn("Remove-Item Env:FAB_LOCAL_API_PUBLIC_URL", script)
        self.assertIn("$env:FAB_INSTANCE_ROOT = $root", script)
        self.assertIn("$previousApiInstanceRoot = $env:FAB_INSTANCE_ROOT", script)
        self.assertIn("$previousWorkerInstanceRoot = $env:FAB_INSTANCE_ROOT", script)
        self.assertIn("$previousWebInstanceRoot = $env:FAB_INSTANCE_ROOT", script)
        self.assertIn("Remove-Item Env:FAB_INSTANCE_ROOT", script)
        self.assertIn('dist\\fab-standalone.js', script)
        self.assertIn('"dist/fab-standalone.js"', script)
        self.assertIn('$env:FAB_WEB_HOST = "127.0.0.1"', script)
        self.assertIn('$env:FAB_OPERATOR_LOCAL_MODE = "true"', script)
        self.assertIn("Remove-Item Env:FAB_WEB_HOST", script)
        self.assertIn("Remove-Item Env:FAB_OPERATOR_LOCAL_MODE", script)

    def test_stop_launcher_recognizes_the_lean_operator_server(self):
        script = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")

        self.assertIn('dist/fab-standalone.js', script)

    def test_dashboard_spawn_preserves_public_address_and_restores_environment(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is unavailable")
        script = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")
        start = script.index("    $previousWebPort = $env:PORT")
        end = script.index('\n}\nelse {\n    $dashboardUri', start)
        block = script[start:end]
        helper = str(ROOT / "scripts/Windows-Profile.ps1").replace("'", "''")
        command = r"""
        $ErrorActionPreference = 'Stop'
        Set-StrictMode -Version Latest
        . '__HELPER__'
        $root = (Get-Location).Path
        $webRoot = $root
        $logsRoot = $root
        $webPort = 3456
        $apiBaseUrl = 'http://127.0.0.1:5456'
        $apiToken = 'synthetic-api-0123456789-ABCDEFGHIJK'
        $haiApiToken = 'synthetic-hai-9876543210-LMNOPQRSTUV'
        $webJwtSecret = 'synthetic-jwt-0123456789-QWERTYUIOP'
        $webMode = 'production'
        $node = @{Source = 'node-not-started'}
        function Start-FabContainedProcess {
            if ($env:FAB_LOCAL_API_PUBLIC_URL -ne $expectedPublic) { throw 'wrong-public-address' }
            if ($env:FAB_LOCAL_API_URL -ne $apiBaseUrl) { throw 'wrong-internal-address' }
            if ($env:FAB_WEB_HOST -ne '127.0.0.1') { throw 'listener-exposed' }
            if ($failSpawn) { throw 'expected-spawn-failure' }
            [pscustomobject]@{Id=42}
        }
        foreach ($configured in @('', 'https://fab-ledger.test')) {
            foreach ($failSpawn in @($false, $true)) {
                $env:FAB_LOCAL_API_PUBLIC_URL = $configured
                $before = $env:FAB_LOCAL_API_PUBLIC_URL
                $expectedPublic = if ($configured) { $configured } else { $apiBaseUrl }
                try {
                    __BLOCK__
                    if ($failSpawn) { throw 'expected-failure-not-raised' }
                } catch {
                    if (-not $failSpawn -or $_.Exception.Message -ne 'expected-spawn-failure') { throw }
                }
                if ($env:FAB_LOCAL_API_PUBLIC_URL -cne $before) { throw 'public-address-not-restored' }
            }
        }
        'verified'
        """.replace("__HELPER__", helper).replace("__BLOCK__", block)
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith(("FAB_", "JWT_SECRET"))}
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "verified")

    def test_stop_launcher_uses_the_encrypted_operator_token_for_api_ownership(self):
        script = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")

        self.assertIn("from src.security.local_secret_store import LocalSecretStore", script)
        self.assertIn("LocalSecretStore(c).load()", script)
        self.assertIn("operator_api_token", script)
        self.assertIn("c.get('fab_local_api_token'", script)

    def test_launcher_reconciles_only_its_checksum_bound_virtual_environment(self):
        script = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")

        self.assertIn("requirements-local.txt", script)
        self.assertIn(".fab-requirements.sha256", script)
        self.assertIn("Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256", script)
        self.assertIn("[System.IO.FileAttributes]::ReparsePoint", script)
        self.assertIn('& (Join-Path $root "Stop-FAB.ps1")', script)
        self.assertIn("Remove-Item -LiteralPath $VenvPath -Recurse -Force", script)
        self.assertIn('Get-Command uv -ErrorAction SilentlyContinue', script)
        self.assertIn('@("venv", "--seed", "--python", "3.13", $venvRoot)', script)
        self.assertIn("-m pip check", script)
        self.assertIn("Set-Content -LiteralPath $venvRequirementsMarker", script)

    def test_maintenance_launcher_is_quiescent_local_and_argument_aware(self):
        script = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")
        start_cmd = (ROOT / "Start-FAB.cmd").read_text(encoding="utf-8")
        maintenance_cmd = (ROOT / "Start-FAB-Maintenance.cmd").read_text(encoding="utf-8")

        self.assertIn("[switch]$Maintenance", script)
        self.assertIn("FAB_MAINTENANCE_MODE", script)
        self.assertIn("ExpectedMaintenanceMode", script)
        self.assertIn("-not $requestedMaintenanceMode -and -not $workerPid", script)
        self.assertIn("maintenanceMode = $requestedMaintenanceMode", script)
        self.assertIn("Start-FAB.ps1", start_cmd)
        self.assertIn("%*", start_cmd)
        self.assertIn("-Maintenance", maintenance_cmd)
        self.assertIn("%*", maintenance_cmd)

    def test_launcher_rejects_wildcard_port_collisions_and_cleans_failed_starts(self):
        script = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")

        self.assertIn("GetActiveTcpListeners", script)
        self.assertIn("ExclusiveAddressUse = $true", script)
        self.assertIn("function Stop-FabSpawnedProcessTree", script)
        self.assertIn("$apiStartedThisRun", script)
        self.assertIn("$workerStartedThisRun", script)
        self.assertIn("$webStartedThisRun", script)
        self.assertIn("Stop-FabSpawnedProcessTree -Process $webProcess", script)


if __name__ == "__main__":
    unittest.main()


def test_real_startup_preflight_rejects_bad_windows_settings_without_services(tmp_path):
    """Run only the launcher's config/preflight block, never the service launcher."""
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        import pytest
        pytest.skip("PowerShell is unavailable")
    script = (ROOT / "Start-FAB.ps1").read_text(encoding="utf-8")
    segment = script[script.index("$startupSettingsJson ="):script.index("$mijngeldzakenExportDir =")]
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        import sys
        python = Path(sys.executable)
    command = "$ErrorActionPreference='Stop'; $Development=$false; $defaultWebPort=3000; $root='" + str(ROOT).replace("'", "''") + "'; "
    command += "$python=@{Source='" + str(python).replace("'", "''") + "'}; "
    command += ". (Join-Path $root 'scripts/Windows-Profile.ps1'); " + segment
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("FAB_", "APP_", "JWT_"))}
    environment.update(PYTHONPATH=str(ROOT), PORT="3000", FAB_DEPLOYMENT_PROFILE="windows",
                       FAB_LOCAL_LEDGER_PATH=str(tmp_path / "external" / "ledger.sqlite3"),
                       FAB_LOCAL_BACKUP_DIR=str(tmp_path / "external-backups"),
                       FAB_LOCAL_API_TOKEN="synthetic-A7b9C2d4E6f8G0h1J3k5L7m9",
                       FAB_HAI_API_TOKEN="synthetic-N8p0Q2r4S6t8U0v2W4x6Y8z0")
    environment["JWT_SECRET"] = "synthetic-R7e5W3q1T9y7U5i3O1p9A7s5"
    secret_file = tmp_path / "synthetic-api-token.txt"
    secret_file.write_text(environment["FAB_LOCAL_API_TOKEN"], encoding="utf-8")
    jwt_file = tmp_path / "synthetic-jwt.txt"
    jwt_file.write_text(environment["JWT_SECRET"], encoding="utf-8")
    hai_file = tmp_path / "synthetic-hai.txt"
    hai_file.write_text(environment["FAB_HAI_API_TOKEN"], encoding="utf-8")
    cases = [
        ({"FAB_LOCAL_API_TOKEN": "too-short"}, False),
        ({"FAB_LOCAL_API_TOKEN": "x" * 48}, False),
        ({"FAB_LOCAL_API_TOKEN": "replace-me-A7b9C2d4E6f8G0h1J3k5L7m9"}, False),
        ({"FAB_LOCAL_LEDGER_PATH": "relative.sqlite3"}, False),
        ({"FAB_LOCAL_BACKUP_DIR": str(ROOT / "backups")}, False),
        ({"FAB_LOCAL_API_HOST": "0.0.0.0"}, False),
        ({"FAB_LOCAL_API_PORT": "65536"}, False),
        ({"PORT": "5001"}, False),
        ({"FAB_LOCAL_API_TOKEN_FILE": str(secret_file)}, False),
        ({"FAB_LOCAL_API_TOKEN": "", "FAB_LOCAL_API_TOKEN_FILE": str(secret_file)}, True),
        ({"FAB_HAI_API_TOKEN": "", "FAB_HAI_API_TOKEN_FILE": str(hai_file)}, True),
        ({"JWT_SECRET_FILE": str(jwt_file)}, False),
        ({"JWT_SECRET": "", "JWT_SECRET_FILE": str(jwt_file)}, True),
        ({"JWT_SECRET": "x" * 48}, False),
        ({}, True),
    ]
    for overrides, success in cases:
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                cwd=tmp_path, env=dict(environment, **overrides),
                                capture_output=True, text=True, timeout=30)
        assert (result.returncode == 0) is success, result.stdout + result.stderr
        assert environment["FAB_LOCAL_API_TOKEN"] not in result.stdout + result.stderr
        assert environment["FAB_HAI_API_TOKEN"] not in result.stdout + result.stderr
        assert environment["JWT_SECRET"] not in result.stdout + result.stderr
    assert not (tmp_path / "external").exists()
    assert not (tmp_path / "external-backups").exists()
