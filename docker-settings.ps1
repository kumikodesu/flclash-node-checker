function Get-LauncherSettings([string]$Path) {
    $settings = [pscustomobject]@{
        version = 1
        dataDirectory = (Join-Path $env:APPDATA 'com.follow\clash')
        browserMode = 'chromium'
    }
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $saved = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($saved.version -ne 1 -or $saved.browserMode -notin @('chromium', 'cloakbrowser')) {
            throw 'Unsupported launcher settings. Check launcher-settings.json.'
        }
        if ($saved.dataDirectory) { $settings.dataDirectory = [string]$saved.dataDirectory }
        $settings.browserMode = $saved.browserMode
    }
    return $settings
}

function Resolve-FlClashDirectory([string]$Directory) {
    $config = Join-Path $Directory 'config.yaml'
    if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
        throw 'The selected data directory must contain the active FlClash config.yaml.'
    }
    return (Resolve-Path -LiteralPath $Directory).Path
}

function Save-LauncherSettings([string]$Path, $Settings) {
    $Settings.dataDirectory = Resolve-FlClashDirectory $Settings.dataDirectory
    if ($Settings.browserMode -notin @('chromium', 'cloakbrowser')) { throw 'Invalid browser mode.' }
    $json = $Settings | ConvertTo-Json
    [IO.File]::WriteAllText($Path + '.tmp', $json, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath ($Path + '.tmp') -Destination $Path -Force
}

function Get-IPLarkCdpUrl([string]$BrowserMode) {
    if ($BrowserMode -eq 'cloakbrowser') { return 'http://host.docker.internal:9222' }
    return ''
}

function Test-LocalCdp {
    try {
        $info = Invoke-RestMethod -Uri 'http://127.0.0.1:9222/json/version' -TimeoutSec 2
        return ([string]$info.webSocketDebuggerUrl -match '^wss?://')
    } catch { return $false }
}

function Test-RunningBatch {
    try {
        $state = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/status' -TimeoutSec 2
        return ($state.running -eq $true)
    } catch { return $false }
}

function Show-LauncherSettings($Settings) {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()
    $form = New-Object Windows.Forms.Form
    $form.Text = 'FlClash Node Checker - 启动设置'
    $form.ClientSize = New-Object Drawing.Size(600, 275)
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $label = New-Object Windows.Forms.Label
    $label.Text = 'FlClash 数据目录（包含当前 config.yaml）：'
    $label.SetBounds(20, 20, 560, 25)
    $pathBox = New-Object Windows.Forms.TextBox
    $pathBox.Text = $Settings.dataDirectory
    $pathBox.SetBounds(20, 50, 465, 25)
    $browse = New-Object Windows.Forms.Button
    $browse.Text = '选择目录'
    $browse.SetBounds(495, 48, 85, 29)
    $browse.Add_Click({
        $folder = New-Object Windows.Forms.FolderBrowserDialog
        $folder.Description = '选择 FlClash 数据目录，而非安装目录'
        $folder.SelectedPath = $pathBox.Text
        if ($folder.ShowDialog($form) -eq 'OK') { $pathBox.Text = $folder.SelectedPath }
        $folder.Dispose()
    })
    $cloak = New-Object Windows.Forms.CheckBox
    $cloak.Text = '使用 CloakBrowser（复用本机 CDP；没有服务时自动部署）'
    $cloak.Checked = ($Settings.browserMode -eq 'cloakbrowser')
    $cloak.SetBounds(20, 95, 560, 30)
    $hint = New-Object Windows.Forms.Label
    $hint.Text = "未勾选时使用镜像内 Chromium，无需安装 CloakBrowser。`r`n首次启用会下载官方镜像；网站验证或限流仍会如实写入报告。`r`n设置保存在此程序目录的 launcher-settings.json，仅保留在本机。"
    $hint.SetBounds(20, 135, 560, 70)
    $save = New-Object Windows.Forms.Button
    $save.Text = '保存并启动'
    $save.SetBounds(360, 225, 105, 30)
    $save.Add_Click({
        try {
            $Settings.dataDirectory = Resolve-FlClashDirectory $pathBox.Text.Trim()
            $Settings.browserMode = if ($cloak.Checked) { 'cloakbrowser' } else { 'chromium' }
            $form.DialogResult = [Windows.Forms.DialogResult]::OK
        } catch {
            [Windows.Forms.MessageBox]::Show($form, $_.Exception.Message, '目录无效') | Out-Null
        }
    })
    $cancel = New-Object Windows.Forms.Button
    $cancel.Text = '取消'
    $cancel.SetBounds(480, 225, 100, 30)
    $cancel.DialogResult = [Windows.Forms.DialogResult]::Cancel
    $form.CancelButton = $cancel
    $form.AcceptButton = $save
    $form.Controls.AddRange(@($label, $pathBox, $browse, $cloak, $hint, $save, $cancel))
    try {
        if ($form.ShowDialog() -eq 'OK') { return $Settings }
        return $null
    } finally { $form.Dispose() }
}
