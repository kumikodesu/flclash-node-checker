param([string]$AssetDirectory = (Join-Path $PSScriptRoot 'docker-assets'))
$ErrorActionPreference = 'Stop'
$expectedHash = '135da95baaf41f077521b98c0170554da432348b2e3aee73d6f4f050119e7917'
$assetPath = Join-Path $AssetDirectory 'flclash-amd64.deb'
if (Test-Path -LiteralPath $assetPath -PathType Leaf) {
    if ((Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
        throw 'Existing FlClash asset checksum mismatch. Download the official v0.8.93 asset before continuing.'
    }
} else {
    New-Item -ItemType Directory -Path $AssetDirectory -Force | Out-Null
    $downloadPath = $assetPath + '.download'
    Write-Host 'Downloading official FlClash v0.8.93 Linux amd64 core package...'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/chen08209/FlClash/releases/download/v0.8.93/FlClash-0.8.93-linux-amd64.deb' -OutFile $downloadPath
    if ((Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
        throw 'Downloaded FlClash asset checksum mismatch; it will not be used.'
    }
    Move-Item -LiteralPath $downloadPath -Destination $assetPath
}
Write-Host 'Official FlClash asset verified.'
