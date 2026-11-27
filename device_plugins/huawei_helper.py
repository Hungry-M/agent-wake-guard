"""PowerShell source embedded in the portable build for the Huawei UAC helper."""

SCRIPT = r'''
# Invoked by the Guard in an Administrator PowerShell with a per-launch event name and owner identity.
$ErrorActionPreference = 'Stop'
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
$programFiles = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
$huaweiRoot = Join-Path $programFiles 'Huawei\Hiview'
$configPath = Join-Path $huaweiRoot 'updateDir\Dubai.xml'
$hiviewPath = Join-Path $huaweiRoot 'HiviewService.exe'
$supervisorPath = Join-Path $programFiles 'Huawei\PCManager\MateBookService.exe'
$recoveryPath = Join-Path $huaweiRoot '.CodexS4Guard-AutoTurnS4'
$utf8 = [Text.UTF8Encoding]::new($false, $true)
$recoveryText = 'CodexS4Guard:AutoTurnS4=1'
$flagPattern = [regex]'<Param\s+Key="AutoTurnS4"\s+Value="([01])"\s*/>'
$rule = '/DubaiConfig/Modules/Module[@ModuleName="AnomalyMs"]/Params[@MachineType="default"]/Param[@Key="AutoTurnS4"]'

function Assert-Regular([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Unexpected link in the Huawei installation.'
    }
}

function Get-S4Setting {
    Assert-Regular $configPath
    $text = $utf8.GetString([IO.File]::ReadAllBytes($configPath))
    $document = [xml]($text.TrimStart([char]0xfeff))
    $nodes = $document.SelectNodes($rule)
    $matches = $flagPattern.Matches($text)
    if ($nodes.Count -ne 1 -or $matches.Count -ne 1 -or $nodes[0].Value -ne $matches[0].Groups[1].Value) {
        throw 'The Huawei AutoTurnS4 configuration has an unexpected format.'
    }
    $match = $matches[0].Groups[1]
    return @{ Text = $text; Index = $match.Index; Value = $match.Value }
}

function Set-S4Setting([string]$Value) {
    $setting = Get-S4Setting
    if ($setting.Value -eq $Value) { return }
    $revised = $setting.Text.Remove($setting.Index, 1).Insert($setting.Index, $Value)
    $stage = Join-Path (Split-Path -Parent $configPath) ('.CodexS4Guard-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllBytes($stage, $utf8.GetBytes($revised))
        if ((Get-S4Setting).Text -cne $setting.Text) { throw 'Huawei changed the configuration during the update.' }
        [IO.File]::Replace($stage, $configPath, [NullString]::Value)
        if ((Get-S4Setting).Value -ne $Value) { throw 'Huawei did not retain the updated AutoTurnS4 value.' }
    } finally {
        [IO.File]::Delete($stage)
    }
}

function Get-SupervisedHiview {
    $supervisor = Get-CimInstance -ClassName Win32_Service -Filter "Name='MBAMainService'"
    if (-not $supervisor -or $supervisor.State -ne 'Running') { return }
    if ($supervisor.PathName.Trim('"') -ine $supervisorPath) { throw 'Unexpected Huawei supervisor path.' }
    $children = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='HiviewService.exe'" |
        Where-Object { $_.ParentProcessId -eq $supervisor.ProcessId -and $_.ExecutablePath -ieq $hiviewPath })
    if ($children.Count -gt 1) { throw 'Multiple supervised Hiview processes were found.' }
    if ($children.Count -eq 1) { return $children[0] }
}

function Reload-Hiview($Current) {
    $process = [Diagnostics.Process]::GetProcessById([int]$Current.ProcessId)
    try {
        if ($process.MainModule.FileName -ine $hiviewPath -or
            [Math]::Abs(($process.StartTime - $Current.CreationDate).TotalSeconds) -gt 1) {
            throw 'Hiview changed before its configuration could be reloaded.'
        }
        $process.Kill()
        if (-not $process.WaitForExit(8000)) { throw 'The old Hiview process did not exit.' }
    } finally {
        $process.Dispose()
    }

    # MateBookService supervises this exact child and relaunches it with its usual account and arguments.
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    do {
        $newProcess = Get-SupervisedHiview
        if ($newProcess -and $newProcess.CreationDate -gt $Current.CreationDate) { return }
        Start-Sleep -Milliseconds 500
    } while ($deadline.Elapsed.TotalSeconds -lt 25)
    throw 'Huawei PC Manager did not restart its Hiview child in time.'
}

function Enable-Protection {
    if ((Get-S4Setting).Value -eq '0') { return }
    $current = Get-SupervisedHiview
    if (-not $current) { throw 'The supported Huawei Hiview process is not running.' }
    $file = [IO.File]::Open($recoveryPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = $utf8.GetBytes($recoveryText)
        $file.Write($bytes, 0, $bytes.Length)
        $file.Flush($true)
    } finally {
        $file.Dispose()
    }
    Set-S4Setting '0'
    Reload-Hiview $current
}

function Restore-Protection {
    if (-not (Test-Path -LiteralPath $recoveryPath)) { return }
    Assert-Regular $recoveryPath
    if ([IO.File]::ReadAllText($recoveryPath) -cne $recoveryText) { throw 'Invalid Huawei recovery record.' }
    Set-S4Setting '1'
    $current = Get-SupervisedHiview
    if ($current) { Reload-Hiview $current }
    [IO.File]::Delete($recoveryPath)
}

$exitCode = 0
$owns = $false
$events = @{}
$mutex = [Threading.Mutex]::new($false, 'Global\CodexS4Guard.Huawei.AutoTurnS4')
try {
    foreach ($path in @((Join-Path $programFiles 'Huawei'), $huaweiRoot, (Split-Path -Parent $configPath), $hiviewPath)) {
        Assert-Regular $path
    }
    $owner = [Diagnostics.Process]::GetProcessById($ownerId)
    $start = [DateTimeOffset]::new($owner.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds()
    if ([Math]::Abs($start - $ownerStarted) -gt 1000 -or $owner.HasExited) { throw 'Guard owner changed.' }
    foreach ($name in @('disable', 'restore', 'stop', 'ack')) {
        $events[$name] = [Threading.EventWaitHandle]::OpenExisting($leaseName + '.' + $name)
    }
    try { $owns = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owns = $true }
    if (-not $owns) { throw 'Another Huawei S4 helper is still active.' }
    Restore-Protection
    [void]$events.ack.Set()
    $commands = [Threading.WaitHandle[]]@($events.stop, $events.disable, $events.restore)
    while (-not $owner.HasExited) {
        $command = [Threading.WaitHandle]::WaitAny($commands, 500)
        if ($command -eq 0 -or $owner.HasExited) { break }
        if ($command -eq 1) { Enable-Protection; [void]$events.ack.Set() }
        if ($command -eq 2) { Restore-Protection; [void]$events.ack.Set() }
    }
} catch {
    $exitCode = 11
    [Console]::Error.WriteLine($_.Exception.Message)
} finally {
    if ($owns) {
        try { Restore-Protection } catch { $exitCode = 12; [Console]::Error.WriteLine($_.Exception.Message) }
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
    foreach ($handle in $events.Values) { $handle.Dispose() }
}
exit $exitCode
'''
