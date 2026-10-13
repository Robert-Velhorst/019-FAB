function Enter-FabLifecycleLock {
    param([Parameter(Mandatory = $true)][string]$Root)

    $normalized = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/').Replace('\', '/').ToLowerInvariant()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $identity = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalized)))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    $mutex = [Threading.Mutex]::new($false, "Local\FABLifecycle-$identity")
    $acquired = $false
    try {
        try { $acquired = $mutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { throw 'Another FAB start or stop operation is active for this checkout. Retry when it finishes.' }
        return $mutex
    }
    finally { if (-not $acquired) { $mutex.Dispose() } }
}

function Exit-FabLifecycleLock {
    param([Parameter(Mandatory = $true)][Threading.Mutex]$Lock)

    try { $Lock.ReleaseMutex() }
    finally { $Lock.Dispose() }
}

function Get-FabProcessReference {
    param([Parameter(Mandatory = $true)][object]$Row)

    $process = $null
    try {
        $process = [System.Diagnostics.Process]::GetProcessById([int]$Row.ProcessId)
        [void]$process.Handle
        $createdAt = ([DateTime]$Row.CreationDate).ToUniversalTime()
        if ([Math]::Abs($process.StartTime.ToUniversalTime().Ticks - $createdAt.Ticks) -ge 10) {
            throw 'FAB process identity changed during discovery. Runtime state was retained.'
        }
        $captured = $process
        $process = $null
        return $captured
    }
    finally { if ($null -ne $process) { $process.Dispose() } }
}

function Stop-FabOwnedProcessTree {
    param(
        [AllowNull()][System.Diagnostics.Process]$Process,
        [ValidateRange(0, 32)][int]$Depth = 0
    )

    if ($null -eq $Process) { return }
    $jobProperty = $Process.PSObject.Properties['FabJob']
    if ($null -ne $jobProperty) {
        $jobProperty.Value.Terminate()
        return
    }
    [void]$Process.Handle
    $startedAt = $Process.StartTime.ToUniversalTime()
    $children = @{}
    $incomplete = $false
    try {
        foreach ($phase in @(0, 1)) {
            if ($phase -eq 1) {
                try {
                    if (-not $Process.HasExited) { $Process.Kill() }
                    if (-not $Process.WaitForExit(5000)) { $incomplete = $true }
                }
                catch { $incomplete = $true }
            }
            try {
                $endedAt = if ($Process.HasExited) { $Process.ExitTime.ToUniversalTime() } else { [DateTime]::MaxValue }
                $rows = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($Process.Id)" -Property ProcessId, ParentProcessId, CreationDate -OperationTimeoutSec 2 -ErrorAction Stop)
                foreach ($row in $rows) {
                    $createdAt = ([DateTime]$row.CreationDate).ToUniversalTime()
                    if ($createdAt -lt $startedAt -or $createdAt -gt $endedAt) { continue }
                    $childId = [int]$row.ProcessId
                    if ($children.ContainsKey($childId)) { continue }
                    try { $children[$childId] = Get-FabProcessReference -Row $row }
                    catch { $incomplete = $true }
                }
            }
            catch { $incomplete = $true }
        }
        foreach ($child in $children.Values) {
            try { Stop-FabOwnedProcessTree -Process $child -Depth ($Depth + 1) }
            catch { $incomplete = $true }
        }
    }
    finally { foreach ($child in $children.Values) { $child.Dispose() } }
    if ($incomplete) {
        throw 'FAB could not verify complete cleanup of its process tree. Runtime state was retained.'
    }
}
