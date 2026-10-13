import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestNgrokWindowsScripts(unittest.TestCase):
    def test_managed_stop_retains_ownership_when_tunnel_is_live_or_unverifiable(self):
        script = (ROOT / "Stop-FAB-Ngrok.ps1").read_text(encoding="utf-8")
        start = script.index("function Test-FabManagedNgrokProcess {")
        end = script.index("\nif (-not (Test-Path -LiteralPath $runtimePath))", start)
        helper = script[start:end]
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is unavailable")

        command = f"""
        $ErrorActionPreference = 'Stop'
        {helper}
        $overlay = Join-Path $env:TEMP 'fab-managed-overlay.yml'
        $script:fakeProcess = [pscustomobject]@{{
            Name = 'ngrok.exe'
            CommandLine = 'ngrok.exe http 5001 --name fab-managed --config ' + $overlay
        }}
        $script:inspectionFails = $false
        function Get-CimInstance {{
            param($ClassName, $Filter, $ErrorAction)
            if ($script:inspectionFails) {{ throw 'inspection unavailable' }}
            return $script:fakeProcess
        }}
        try {{
            Assert-FabNgrokProcessStopped -ProcessId 4321 -OverlayPath $overlay
            throw 'Expected a live managed tunnel to retain ownership metadata.'
        }} catch {{
            if ($_.Exception.Message -notlike '*still running*') {{ throw }}
        }}
        $script:fakeProcess = $null
        Assert-FabNgrokProcessStopped -ProcessId 4321 -OverlayPath $overlay
        $script:fakeProcess = [pscustomobject]@{{ Name = 'ngrok.exe'; CommandLine = 'ngrok.exe http 5001' }}
        Assert-FabNgrokProcessStopped -ProcessId 4321 -OverlayPath $overlay
        $script:inspectionFails = $true
        try {{
            Assert-FabNgrokProcessStopped -ProcessId 4321 -OverlayPath $overlay
            throw 'Expected process inspection failure to retain ownership metadata.'
        }} catch {{
            if ($_.Exception.Message -ne 'inspection unavailable') {{ throw }}
        }}
        'verified'
        """
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "verified")
        self.assertLess(
            script.index("Assert-FabNgrokProcessStopped -ProcessId"),
            script.index("foreach ($path in @($runtime.overlayPath"),
        )

    def test_managed_launcher_is_authenticated_isolated_and_non_pooling(self):
        script = (ROOT / "Start-FAB-Ngrok.ps1").read_text(encoding="utf-8")

        self.assertIn(".venv\\Scripts\\python.exe", script)
        self.assertIn("Another ngrok endpoint is already active", script)
        self.assertIn("FAB will not stop or pool the existing endpoint", script)
        self.assertIn('"--name", "fab-managed"', script)
        self.assertIn("/api/live", script)
        self.assertIn("/api/hai/manifest", script)
        self.assertIn("-m src.security.windows_runtime_credentials", script)
        self.assertNotIn("get_or_create_runtime_secret", script)
        self.assertIn('$haiHeaders = @{ Authorization = "Bearer $haiApiToken" }', script)
        self.assertIn('$publicUrl/api/health', script)
        self.assertIn('HAI credential escaped its route scope', script)
        self.assertIn("authRequired", script)
        self.assertIn("maintenanceMode", script)
        self.assertIn("Test-CleanHttpsOrigin -Uri $publicUri", script)
        self.assertIn("Write-JsonAtomic", script)
        self.assertNotIn("pooling-enabled", script.lower())

    def test_managed_stop_requires_checkout_process_and_overlay_ownership(self):
        script = (ROOT / "Stop-FAB-Ngrok.ps1").read_text(encoding="utf-8")

        self.assertIn("instanceId", script)
        self.assertIn("fab-ngrok-tunnel", script)
        self.assertIn("ngrok.exe", script)
        self.assertIn("fab-managed", script)
        self.assertIn("overlayMarker", script)
        self.assertIn("Test-OwnedManagedPath", script)
        self.assertIn("Assert-FabNgrokProcessStopped", script)
        self.assertIn("Refusing to stop", script)

    def test_main_shutdown_stops_cloud_before_api_and_uses_isolated_python(self):
        script = (ROOT / "Stop-FAB.ps1").read_text(encoding="utf-8")

        ngrok_stop = script.index("Stop-FAB-Ngrok.ps1")
        api_stop = script.index(
            'Stop-FabProcessTree -ProcessId $ownedApiPid -CommandMarker "src.operations.local_api"'
        )
        self.assertLess(ngrok_stop, api_stop)
        self.assertLess(ngrok_stop, script.index("Stop-FabRecordedProcess -Identity $entry.Value"))
        self.assertIn(".venv\\Scripts\\python.exe", script)
        cleanup = script.index("force_release_stopped_runtime_leases(actor='Stop-FAB.ps1')")
        self.assertLess(api_stop, cleanup)
        self.assertIn("with managed_worker_maintenance(Path.cwd()):", script)
        self.assertNotIn("list_runtime_leases", script)
        self.assertNotIn("Get-Command python -ErrorAction Stop", script)

    def test_cmd_launchers_forward_operator_arguments(self):
        start_cmd = (ROOT / "Start-FAB-Ngrok.cmd").read_text(encoding="utf-8")
        stop_cmd = (ROOT / "Stop-FAB-Ngrok.cmd").read_text(encoding="utf-8")

        self.assertIn("Start-FAB-Ngrok.ps1", start_cmd)
        self.assertIn("Stop-FAB-Ngrok.ps1", stop_cmd)
        self.assertIn("%*", start_cmd)
        self.assertIn("%*", stop_cmd)

    def test_managed_and_verification_tunnels_refuse_maintenance_mode(self):
        managed = (ROOT / "Start-FAB-Ngrok.ps1").read_text(encoding="utf-8")
        verification = (ROOT / "Test-FAB-Ngrok.ps1").read_text(encoding="utf-8")

        self.assertIn("disabled during maintenance", managed)
        self.assertIn("localLive.maintenanceMode", managed)
        self.assertIn("disabled during maintenance", verification)
        self.assertIn("localLive.maintenanceMode", verification)
        self.assertIn("-m src.security.windows_runtime_credentials", verification)
        self.assertNotIn("get_or_create_runtime_secret", verification)
        self.assertIn('Authorization = "Bearer $haiApiToken"', verification)


if __name__ == "__main__":
    unittest.main()
