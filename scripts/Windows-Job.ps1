function Initialize-FabWindowsJob {
    $sourcePath = Join-Path $PSScriptRoot 'Windows-Job.cs'
    $source = [IO.File]::ReadAllText($sourcePath)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($source))).Replace('-', '') }
    finally { $sha.Dispose() }
    $type = 'Fab.Windows.JobLease' -as [type]
    if ($null -eq $type) {
        Add-Type -TypeDefinition $source.Replace('__FAB_JOB_HASH__', $hash) -ErrorAction Stop
    } elseif ($type::SourceHash -ne $hash) {
        throw 'FAB containment code changed. Start FAB from a fresh PowerShell process.'
    }
}

function Start-FabContainedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [AllowEmptyCollection()][AllowEmptyString()][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$RedirectStandardOutput,
        [Parameter(Mandatory = $true)][string]$RedirectStandardError
    )

    Initialize-FabWindowsJob
    $lease = [Fab.Windows.JobLease]::Start(
        $FilePath, $ArgumentList, $WorkingDirectory,
        $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($RedirectStandardOutput),
        $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($RedirectStandardError))
    try {
        $process = $lease.Process
        Add-Member -InputObject $process -MemberType NoteProperty -Name FabJob -Value $lease
        return $process
    } catch {
        $lease.Dispose()
        $lease.Process.Dispose()
        throw
    }
}
