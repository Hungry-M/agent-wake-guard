param(
    [ValidateSet('guard', 'huawei', 'idle-source')]
    [string] $Target = 'guard',
    [string] $OutputDirectory = (Join-Path $PSScriptRoot 'dist')
)

$ErrorActionPreference = 'Stop'
$output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
Push-Location -LiteralPath $PSScriptRoot
try {
    $releaseVersion = python -B -c 'from i18n import VERSION; print(VERSION)'
    if ($LASTEXITCODE -ne 0 -or $releaseVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw 'Could not read the version from i18n.py.'
    }
    $options = @(
        '--mode=onefile', '--mingw64', '--assume-yes-for-downloads', '--lto=no',
        '--experimental=force-dependencies-pefile', '--onefile-cache-mode=temporary',
        '--onefile-tempdir-spec={TEMP}/AgentWakeGuard-{PID}-{TIME}',
        '--nofollow-import-to=win32evtlogutil,win32evtlog,win32con,winerror',
        "--file-version=${releaseVersion}.0", "--product-version=${releaseVersion}.0",
        '--product-name=Agent Wake Guard'
    )
    $entry = switch ($Target) {
        'guard' {
            $options += @('--windows-console-mode=attach', '--windows-icon-from-ico=guard.ico',
                          '--include-data-files=deepseek_reader.cjs=deepseek_reader.cjs',
                          '--include-data-files=guard.ico=guard.ico')
            @{ File = 'guard.py'; Name = 'Agent-Wake-Guard.exe'; Directory = $output }
        }
        'huawei' {
            $options += '--windows-console-mode=force'
            @{ File = 'huawei_plugin.py'; Name = 'huawei-hiview.guard-plugin.exe'
               Directory = (Join-Path $output 'Agent-Wake-Guard.plugins') }
        }
        'idle-source' {
            $options += '--windows-console-mode=force'
            @{ File = 'examples/idle_task_source.py'; Name = 'idle-example.source-plugin.exe'
               Directory = (Join-Path $output 'Agent-Wake-Guard.sources') }
        }
    }
    python -B -m nuitka @options "--output-dir=$($entry.Directory)" "--output-filename=$($entry.Name)" $entry.File
    if ($LASTEXITCODE -ne 0) { throw "Build failed for $Target." }
    Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $entry.Directory $entry.Name)
} finally {
    Pop-Location
}
