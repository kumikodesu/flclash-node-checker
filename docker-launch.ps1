param(
    [string]$ConfigPath = '',
    [ValidateSet('', 'chromium', 'cloakbrowser')][string]$BrowserMode = '',
    [switch]$PrepareOnly,
    [switch]$NoSettings,
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$logPath = Join-Path $PSScriptRoot 'docker-startup.log'
"Startup: $(Get-Date -Format o)" | Set-Content -LiteralPath $logPath -Encoding UTF8
try {
    . (Join-Path $PSScriptRoot 'docker-settings.ps1')
    $settingsPath = Join-Path $PSScriptRoot 'launcher-settings.json'
    $settings = Get-LauncherSettings $settingsPath
    if ($ConfigPath) {
        if ([IO.Path]::GetFileName($ConfigPath) -ne 'config.yaml') { throw 'Select the active FlClash config.yaml.' }
        $settings.dataDirectory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($ConfigPath))
    }
    if ($BrowserMode) { $settings.browserMode = $BrowserMode }
    if (-not $NoSettings -and -not $PrepareOnly) {
        $settings = Show-LauncherSettings $settings
        if ($null -eq $settings) { Write-Host 'Startup cancelled.'; exit 0 }
    }
    if (Test-RunningBatch) { throw 'A node measurement is running. Stop it in the web page or wait for completion before restarting or changing startup settings.' }
    Save-LauncherSettings $settingsPath $settings
    $env:FLCLASH_DIR = $settings.dataDirectory
    # Set an explicit empty value when opted out, overriding inherited CDP settings.
    $env:IPLARK_CDP_URL = Get-IPLarkCdpUrl $settings.browserMode
    $composeEnvPath = Join-Path $PSScriptRoot 'docker.env'
    $mountDirectory = $env:FLCLASH_DIR.Replace('\', '/').Replace("'", "\'")
    "FLCLASH_DIR='$mountDirectory'" | Set-Content -LiteralPath $composeEnvPath -Encoding UTF8
    "IPLARK_CDP_URL='$env:IPLARK_CDP_URL'" | Add-Content -LiteralPath $composeEnvPath -Encoding UTF8
    $dataDir = Join-Path $PSScriptRoot 'docker-data'
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    $cacheSource = Join-Path $PSScriptRoot 'iplark-cache.json'
    $cacheTarget = Join-Path $dataDir 'iplark-cache.json'
    if ((Test-Path -LiteralPath $cacheSource) -and -not (Test-Path -LiteralPath $cacheTarget)) {
        Copy-Item -LiteralPath $cacheSource -Destination $cacheTarget
    }
    "Read-only config directory: $env:FLCLASH_DIR" | Add-Content -LiteralPath $logPath -Encoding UTF8
    $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $dockerCommand) { throw 'Docker Desktop is not installed or docker.exe is not on PATH.' }
    & $dockerCommand.Source compose --env-file $composeEnvPath config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose configuration validation failed.' }
    if ($PrepareOnly) { Write-Host 'Docker configuration prepared and validated.'; exit 0 }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $engineCheck = & $dockerCommand.Source info --format '{{.OSType}}' 2>&1
    $engineCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    $engineCheck | Add-Content -LiteralPath $logPath -Encoding UTF8
    if ($engineCode -ne 0) { throw 'Docker engine is unavailable. Open Docker Desktop and resolve its startup error, then run start-docker.cmd again.' }
    if (($engineCheck -join '').Trim() -ne 'linux') { throw 'Switch Docker Desktop to Linux containers.' }
    & (Join-Path $PSScriptRoot 'prepare-assets.ps1')
    if ($settings.browserMode -eq 'cloakbrowser' -and -not (Test-LocalCdp)) {
        $ErrorActionPreference = 'Continue'
        & $dockerCommand.Source compose --env-file $composeEnvPath --profile cloakbrowser up --detach --wait --wait-timeout 120 cloakbrowser 2>&1 | ForEach-Object {
            $line = $_.ToString()
            [IO.File]::AppendAllText($logPath, $line + [Environment]::NewLine, [Text.Encoding]::UTF8)
            Write-Host $line
        }
        $cloakCode = $LASTEXITCODE
        $ErrorActionPreference = $previousPreference
        if ($cloakCode -ne 0 -or -not (Test-LocalCdp)) { throw 'CloakBrowser is unavailable. Check port 9222 and Docker logs, or select Chromium in startup settings.' }
    }
    "Browser mode: $($settings.browserMode)" | Add-Content -LiteralPath $logPath -Encoding UTF8
    $ErrorActionPreference = 'Continue'
    & $dockerCommand.Source compose --env-file $composeEnvPath up --build --detach checker 2>&1 | ForEach-Object {
        $line = $_.ToString()
        [IO.File]::AppendAllText($logPath, $line + [Environment]::NewLine, [Text.Encoding]::UTF8)
        Write-Host $line
    }
    $composeCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($composeCode -ne 0) { throw 'Container build or startup failed. See docker-startup.log.' }
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $null = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8080/health' -TimeoutSec 2
            $ready = $true
            break
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw 'Container did not become ready. Run docker compose --env-file docker.env logs checker.' }
    if (-not $NoOpen) { Start-Process 'http://127.0.0.1:8080' }
    Write-Host 'Ready: http://127.0.0.1:8080'
} catch {
    Write-Host $_.Exception.Message
    $_.Exception.Message | Add-Content -LiteralPath $logPath -Encoding UTF8
    exit 1
}
