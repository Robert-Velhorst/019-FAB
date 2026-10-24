param(
    [string]$ComposeFile = "docker-compose.yml",
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"
$ComposeFile = (Resolve-Path -LiteralPath $ComposeFile).Path
$composePrefix = @("compose")
if (Test-Path -LiteralPath $EnvFile) {
    $EnvFile = (Resolve-Path -LiteralPath $EnvFile).Path
    $composePrefix += @("--env-file", $EnvFile)
}
$composePrefix += @("-f", $ComposeFile)

function Invoke-Docker {
    param([string[]]$Arguments)
    $output = & docker @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed ($($Arguments -join ' ')): $($output -join [Environment]::NewLine)"
    }
    return $output
}

$containerIds = @(Invoke-Docker ($composePrefix + @("ps", "--all", "--quiet", "api")) | Where-Object { $_.Trim() })
if ($containerIds.Count -eq 0) {
    Write-Output "No existing Compose API container found; no legacy OAuth token migration is needed."
    exit 0
}
if ($containerIds.Count -ne 1) {
    throw "Expected one API container, found $($containerIds.Count). Stop and resolve the Compose project ambiguity first."
}

$containerId = $containerIds[0].Trim()
$stage = Join-Path $env:LOCALAPPDATA ("FAB\token-migration\" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stage | Out-Null

$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$systemSid = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-18")
foreach ($sid in @($userSid, $systemSid)) {
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $sid, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow"
    )
    [void]$acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $stage -AclObject $acl
$tokenStage = Join-Path $stage "tokens"
New-Item -ItemType Directory -Path $tokenStage | Out-Null

$migrationSucceeded = $false
try {
    # Stop writers before taking the token snapshot. The old container remains available to docker cp.
    Invoke-Docker ($composePrefix + @("stop", "api", "worker")) | Out-Null
    Invoke-Docker @("cp", "${containerId}:/app/tokens/.", $tokenStage) | Out-Null

    $reparsePoint = [System.IO.FileAttributes]::ReparsePoint
    $unsafePaths = @(Get-ChildItem -LiteralPath $tokenStage -Force -Recurse | Where-Object {
        ($_.Attributes -band $reparsePoint) -ne 0
    })
    if ($unsafePaths.Count -gt 0) {
        throw "Legacy token snapshot contains a link or reparse point. Staging was preserved at $stage."
    }

    $tokenStagePrefix = [System.IO.Path]::GetFullPath($tokenStage).TrimEnd('\') + '\'
    $expected = @(
        Get-ChildItem -LiteralPath $tokenStage -File -Recurse -Force | ForEach-Object {
            $relative = $_.FullName.Substring($tokenStagePrefix.Length).Replace("\", "/")
            [pscustomobject]@{
                path = $relative
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                bytes = [long]$_.Length
            }
        }
    ) | Sort-Object path

    Invoke-Docker ($composePrefix + @("build", "api")) | Out-Null
    $migrationOutput = Invoke-Docker ($composePrefix + @(
        "run", "--rm", "--no-deps", "--volume", "${tokenStage}:/fab-token-migration:ro",
        "--entrypoint", "python", "api", "-m", "src.security.compose_token_migration", "/fab-token-migration"
    ))
    $migrationRecords = @(
        $migrationOutput | ForEach-Object {
            try { $_ | ConvertFrom-Json -ErrorAction Stop } catch { $null }
        } | Where-Object { $_ -and $_.PSObject.Properties.Name -contains "files" }
    )
    if ($migrationRecords.Count -ne 1) {
        throw "Token migration did not return exactly one verification manifest. Staging was preserved at $stage."
    }
    $actual = $migrationRecords[0]
    $actualFiles = @($actual.files | Sort-Object path)
    $manifestMatches = $expected.Count -eq $actualFiles.Count
    for ($index = 0; $manifestMatches -and $index -lt $expected.Count; $index++) {
        $manifestMatches = (
            $expected[$index].path -ceq $actualFiles[$index].path -and
            $expected[$index].sha256 -ceq $actualFiles[$index].sha256 -and
            [long]$expected[$index].bytes -eq [long]$actualFiles[$index].bytes
        )
    }
    if (-not $manifestMatches) {
        throw "Token migration manifest differs from the stopped container snapshot. Staging was preserved at $stage."
    }
    $migrationSucceeded = $true
    Write-Output "Migrated and checksum-verified $($expected.Count) OAuth token files into the persistent Compose volume."
    Write-Output "The API and worker remain stopped. Start them with: docker compose -f $ComposeFile up -d"
}
finally {
    if ($migrationSucceeded -and (Test-Path -LiteralPath $stage)) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
