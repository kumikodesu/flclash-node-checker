$ErrorActionPreference = 'Stop'
$taskLog = Join-Path $PSScriptRoot 'startup.log'
function Write-StartupLog([string]$taskText) {
    Write-Host $taskText
    Add-Content -LiteralPath $taskLog -Value $taskText -Encoding UTF8
}
try {
Set-Content -LiteralPath $taskLog -Value ('Startup: ' + (Get-Date -Format o)) -Encoding UTF8
Write-StartupLog ('Program folder: ' + $PSScriptRoot)
$taskCandidates = @(
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
)
$taskPythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($taskPythonCommand) { $taskCandidates += $taskPythonCommand.Source }
$taskPython = $null
foreach ($taskCandidate in $taskCandidates) {
    if (Test-Path -LiteralPath $taskCandidate -PathType Leaf) {
        # Windows PowerShell 5 turns native stderr into terminating errors under Stop.
        $ErrorActionPreference = 'Continue'
        $taskProbe = & $taskCandidate -E -s -X utf8 (Join-Path $PSScriptRoot 'bootstrap.py') --check-dependencies 2>&1
        $taskProbeExit = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($taskProbeExit -eq 0) {
            $taskPython = $taskCandidate
            foreach ($taskLine in $taskProbe) { Write-StartupLog ($taskLine.ToString()) }
            break
        }
        Write-StartupLog ('Python dependency check failed: ' + $taskCandidate)
        foreach ($taskLine in $taskProbe) { Write-StartupLog ($taskLine.ToString()) }
    }
}
if (-not $taskPython) {
    throw 'No compatible Python found. Fully extract the vendor folder and use Python 3.10+ with tkinter. See README.md.'
}
Write-StartupLog ('Using Python: ' + $taskPython)
[string[]]$taskArguments = @('--autorun')
if ($args.Count) { $taskArguments = $args }
$ErrorActionPreference = 'Continue'
& $taskPython -E -s -X utf8 -u (Join-Path $PSScriptRoot 'bootstrap.py') @taskArguments 2>&1 |
    ForEach-Object { Write-StartupLog ($_.ToString()) }
$taskExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
Write-StartupLog ('Program exit code: ' + $taskExit)
exit $taskExit
} catch {
    $taskMessage = $_.ToString()
    Write-Host $taskMessage
    try { Add-Content -LiteralPath $taskLog -Value $taskMessage -Encoding UTF8 } catch {}
    exit 1
}
