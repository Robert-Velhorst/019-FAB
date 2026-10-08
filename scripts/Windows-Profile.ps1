function Invoke-FabWithServiceCredentials {
    param(
        [Parameter(Mandatory = $true)][string]$ApiToken,
        [Parameter(Mandatory = $true)][string]$HaiApiToken,
        [string]$JwtSecret,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    # Resolve file credentials once, then pass identical values to every child.
    $values = @{
        FAB_LOCAL_API_TOKEN = $ApiToken
        FAB_HAI_API_TOKEN = $HaiApiToken
        FAB_LOCAL_API_TOKEN_FILE = $null
        FAB_HAI_API_TOKEN_FILE = $null
    }
    $previous = @{}
    if ($PSBoundParameters.ContainsKey("JwtSecret")) {
        $values["JWT_SECRET"] = $JwtSecret
        $values["JWT_SECRET_FILE"] = $null
    }
    foreach ($name in $values.Keys) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    try {
        foreach ($name in $values.Keys) {
            [Environment]::SetEnvironmentVariable($name, $values[$name], "Process")
        }
        & $Action
    }
    finally {
        foreach ($name in $values.Keys) {
            [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process")
        }
    }
}

function Write-FabRuntimeMetadata {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Runtime
    )

    $destination = [System.IO.Path]::GetFullPath($Path)
    $temporary = Join-Path ([System.IO.Path]::GetDirectoryName($destination)) (".fab-runtime-" + [Guid]::NewGuid().ToString("N") + ".tmp")
    $json = $Runtime | ConvertTo-Json -Depth 4
    try {
        [System.IO.File]::WriteAllText($temporary, $json, [System.Text.UTF8Encoding]::new($false))
        # Same-directory publication keeps readers on a complete old or new record.
        if ([System.IO.File]::Exists($destination)) {
            [System.IO.File]::Replace($temporary, $destination, [System.Management.Automation.Language.NullString]::Value)
        }
        else {
            [System.IO.File]::Move($temporary, $destination)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}
