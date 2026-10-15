import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestNgrokWindowsScripts(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "process inspection is Windows-specific")
    def test_private_inspector_must_belong_to_managed_ngrok_process(self):
        script = (ROOT / "Start-FAB-Ngrok.ps1").read_text(encoding="utf-8")
        start = script.index("function Assert-FabNgrokInspectorOwned {")
        end = script.index("\nfunction Test-CleanHttpsOrigin", start)
        helper = script[start:end]
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is unavailable")

        command = f"""
        $ErrorActionPreference = 'Stop'
        {helper}
        $script:connection = [pscustomobject]@{{ LocalAddress = '127.0.0.1'; OwningProcess = 2468 }}
        function Get-NetTCPConnection {{ return $script:connection }}
        Assert-FabNgrokInspectorOwned -Port 4041 -ProcessId 2468
        $script:connection = [pscustomobject]@{{ LocalAddress = '127.0.0.1'; OwningProcess = 1357 }}
        try {{
            Assert-FabNgrokInspectorOwned -Port 4041 -ProcessId 2468
            throw 'A foreign inspector process was accepted.'
        }} catch {{
            if ($_.Exception.Message -notlike '*not owned by the managed ngrok process*') {{ throw }}
        }}
        $script:connection = [pscustomobject]@{{ LocalAddress = '::1'; OwningProcess = 2468 }}
        try {{
            Assert-FabNgrokInspectorOwned -Port 4041 -ProcessId 2468
            throw 'An IPv6 or non-loopback listener was accepted.'
        }} catch {{
            if ($_.Exception.Message -notlike '*not owned by the managed ngrok process*') {{ throw }}
        }}
        'verified'
        """
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "verified")

    @unittest.skipUnless(os.name == "nt", "process launch flags are Windows-specific")
    def test_failed_startup_uses_process_handle_and_confirms_exit(self):
        script = (ROOT / "Start-FAB-Ngrok.ps1").read_text(encoding="utf-8")
        start = script.index("function Stop-FabStartedNgrokProcess {")
        end = script.index("\nif (-not (Test-Path -LiteralPath $fabRuntimePath))", start)
        helper = script[start:end]
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is unavailable")

        command = f"""
        $ErrorActionPreference = 'Stop'
        {helper}
        $shellPath = Join-Path $PSHOME ((Get-Process -Id $PID).ProcessName + '.exe')
        $child = Start-Process -FilePath $shellPath -ArgumentList @('-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds 30') -WindowStyle Hidden -PassThru
        if (-not (Stop-FabStartedNgrokProcess -Process $child)) {{ throw 'The started process was not confirmed stopped.' }}
        if (-not $child.HasExited) {{ throw 'The process handle still reports a live process.' }}
        $child.Dispose()
        'verified'
        """
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "verified")

    def test_verification_cleanup_retains_files_when_process_stop_is_unconfirmed(self):
        script = (ROOT / "Test-FAB-Ngrok.ps1").read_text(encoding="utf-8")
        start = script.index("function Stop-FabStartedNgrokProcess {")
        end = script.index("\n$inspectionPort = Find-AvailableLoopbackPort", start)
        helpers = script[start:end]
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is unavailable")

        command = f"""
        $ErrorActionPreference = 'Stop'
        {helpers}
        function Stop-FabStartedNgrokProcess {{ return $false }}
        $paths = @('verification-overlay.yml', 'verification.out.log', 'verification.err.log')
        foreach ($path in $paths) {{ [System.IO.File]::WriteAllText($path, 'retained') }}
        $child = [System.Diagnostics.Process]::new()
        $result = Complete-FabNgrokVerificationCleanup -Process $child -Paths $paths
        if ($result) {{ throw 'Cleanup must report an unconfirmed stop.' }}
        if (@($paths | Where-Object {{ -not (Test-Path -LiteralPath $_) }}).Count -ne 0) {{ throw 'Cleanup removed files before confirming process exit.' }}
        foreach ($path in $paths) {{ Remove-Item -LiteralPath $path -Force }}
        'verified'
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                   cwd=temp_dir, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "verified")

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
        $overlay = [System.IO.Path]::GetFullPath('fab-managed-overlay.yml')
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
        if (-not (Test-FabManagedNgrokProcess -Process $script:fakeProcess -OverlayPath $overlay)) {{
            throw "Synthetic process did not match managed ownership: $($script:fakeProcess.CommandLine)"
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

    def test_stop_retains_ownership_files_when_ngrok_survives_termination(self):
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("Windows PowerShell is unavailable")

        with tempfile.TemporaryDirectory(prefix="fab-ngrok-stop-") as directory:
            root = Path(directory)
            local_app_data = root / "user-data"
            managed = local_app_data / "FAB" / "ngrok"
            data = root / "data"
            managed.mkdir(parents=True)
            data.mkdir()
            script = root / "Stop-FAB-Ngrok.ps1"
            shutil.copy2(ROOT / "Stop-FAB-Ngrok.ps1", script)
            overlay = managed / "fab-managed.yml"
            stdout = managed / "ngrok.out.log"
            stderr = managed / "ngrok.err.log"
            for path in (overlay, stdout, stderr):
                path.write_text("owned fixture", encoding="utf-8")

            normalized_root = str(root.resolve()).replace("\\", "/").lower()
            instance_id = hashlib.sha256(normalized_root.encode("utf-8")).hexdigest()
            runtime_path = data / "fab-ngrok-runtime.json"
            runtime_path.write_text(json.dumps({
                "instanceRoot": str(root),
                "instanceId": instance_id,
                "service": "fab-ngrok-tunnel",
                "version": 1,
                "authRequired": True,
                "processId": 52341,
                "overlayPath": str(overlay),
                "stdoutPath": str(stdout),
                "stderrPath": str(stderr),
            }), encoding="utf-8")

            escaped_script = str(script).replace("'", "''")
            escaped_overlay = str(overlay).replace("'", "''")
            harness = f"""
$ErrorActionPreference = 'Stop'
function Get-CimInstance {{
    [CmdletBinding()]
    param([string]$ClassName, [string]$Filter, [string[]]$Property)
    [pscustomobject]@{{
        ProcessId = 52341
        Name = 'ngrok.exe'
        CommandLine = 'ngrok.exe start fab-managed --config "{escaped_overlay}"'
    }}
}}
function Stop-Process {{ [CmdletBinding()] param([int]$Id, [switch]$Force) }}
function Wait-Process {{ [CmdletBinding()] param([int]$Id, [int]$Timeout) }}
$failedAsExpected = $false
try {{ & '{escaped_script}' -Quiet }}
catch {{ $failedAsExpected = $_.Exception.Message -match 'still running|did not exit|retained' }}
if (-not $failedAsExpected) {{ throw 'Stop-FAB-Ngrok did not retain state after the process survived termination.' }}
if (-not (Test-Path -LiteralPath '{runtime_path}')) {{ throw 'Runtime ownership metadata was removed.' }}
if (-not (Test-Path -LiteralPath '{overlay}')) {{ throw 'The ngrok overlay was removed.' }}
"""
            environment = os.environ.copy()
            environment["LOCALAPPDATA"] = str(local_app_data)
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", harness],
                capture_output=True,
                text=True,
                timeout=30,
                env=environment,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_managed_launcher_is_authenticated_isolated_and_non_pooling(self):
        script = (ROOT / "Start-FAB-Ngrok.ps1").read_text(encoding="utf-8")

        self.assertIn(".venv\\Scripts\\python.exe", script)
        self.assertIn("Another ngrok endpoint is already active", script)
        self.assertIn("FAB will not stop or pool the existing endpoint", script)
        self.assertIn('"start", "fab-managed"', script)
        self.assertIn('[string]$DashboardUrl = ""', script)
        self.assertIn('$apiEndpointUrlYaml = ""', script)
        self.assertIn('$apiEndpointUrlYaml = "    url: $(ConvertTo-YamlScalar -Value $Url)"', script)
        self.assertNotIn('else { \'"https://"\' }', script)
        self.assertIn('name: fab-managed-dashboard', script)
        self.assertIn('type: basic-auth', script)
        self.assertIn('enforce: true', script)
        self.assertIn('type: remove-headers', script)
        self.assertIn('headers:\n                  - authorization', script)
        self.assertIn("$dashboardRuntimePort = $dashboardPort", script)
        self.assertIn("dashboardPort = $dashboardRuntimePort", script)
        self.assertIn("$dashboardHeaders.Clear()", script)
        self.assertIn("Protect-ManagedNgrokDirectory -Path $managedRoot", script)
        self.assertIn('"*S-1-5-18:(OI)(CI)F"', script)
        self.assertIn('"*S-1-5-32-544:(OI)(CI)F"', script)
        self.assertIn("/inheritance:r /grant:r", script)
        self.assertNotIn("/T /C", script)
        self.assertLess(
            script.index("Protect-ManagedNgrokDirectory -Path $managedRoot"),
            script.index("$overlayPath,\n    $overlay,"),
        )
        self.assertIn('remote dashboard did not reject an unauthenticated request', script)
        self.assertIn('authenticated remote dashboard could not be reached', script)
        self.assertIn("/api/live", script)
        self.assertIn("/api/hai/manifest", script)
        self.assertIn("-m src.security.windows_runtime_credentials", script)
        self.assertIn("--require-strong", script)
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
        self.assertIn("$remainingProcess = Get-CimInstance", script)
        self.assertIn("Ownership metadata and agent files are being retained", script)
        self.assertLess(script.index("$remainingProcess = Get-CimInstance"), script.index("foreach ($path in @($runtime.overlayPath"))

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
