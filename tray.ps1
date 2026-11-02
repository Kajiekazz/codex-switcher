param(
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

if (-not ("CsTrayKeyState" -as [type])) {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class CsTrayKeyState {
    [DllImport("user32.dll")]
    public static extern short GetAsyncKeyState(int vKey);
}
"@
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$base = "http://127.0.0.1:8787"
$script:lastEmail = ""
$script:nextRefresh = [DateTime]::MinValue
$script:hotDown = $false

function Test-CsService {
    try {
        Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Open-CsDashboard {
    Start-Process "$base/"
}

function Refresh-CsStatus {
    try {
        $status = Invoke-RestMethod -Uri "$base/api/status" -TimeoutSec 3
        $quota = $null
        try {
            $quota = Invoke-RestMethod -Uri "$base/api/quota" -TimeoutSec 5
        } catch { }

        $email = $status.auth.email
        if (-not $email) { $email = "未登录" }
        $script:lastEmail = $email

        $text = "Codex: $email"
        if ($quota -and $quota.quota -and $quota.quota.ok -and $quota.quota.primary_window) {
            $pct = $quota.quota.primary_window.used_percent
            if ($null -ne $pct) {
                $text += " | 5h $pct%"
            }
        }
        if ($text.Length -gt 63) { $text = $text.Substring(0, 63) }
        $notify.Text = $text
    } catch {
        $notify.Text = "Codex Switcher: 服务未启动"
        $script:lastEmail = ""
    }
}

function Start-CsService {
    if (Test-CsService) { return }
    $python = (Get-Command python -ErrorAction Stop).Source
    . (Join-Path $root 'service-launch.ps1')
    Start-CsServiceProcess -PythonPath $python -ProjectRoot $root | Out-Null
    Start-Sleep -Seconds 2
    Refresh-CsStatus
}

function Stop-CsService {
    & "$root\stop.ps1"
    Start-Sleep -Milliseconds 500
    Refresh-CsStatus
}

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Application
$notify.Text = "Codex Switcher"
$notify.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$openItem = $menu.Items.Add("打开 Dashboard")
$copyItem = $menu.Items.Add("复制当前邮箱")
$refreshItem = $menu.Items.Add("刷新状态")
$menu.Items.Add("-") | Out-Null
$startItem = $menu.Items.Add("启动服务")
$stopItem = $menu.Items.Add("停止服务")
$menu.Items.Add("-") | Out-Null
$exitItem = $menu.Items.Add("退出托盘")
$stopExitItem = $menu.Items.Add("停止服务并退出")
$notify.ContextMenuStrip = $menu

$openItem.add_Click({ Open-CsDashboard })
$copyItem.add_Click({
    if ($script:lastEmail) {
        try {
            [System.Windows.Forms.Clipboard]::SetText($script:lastEmail)
            $notify.ShowBalloonTip(1500, "Codex Switcher", "已复制：$($script:lastEmail)", [System.Windows.Forms.ToolTipIcon]::Info)
        } catch { }
    }
})
$refreshItem.add_Click({ Refresh-CsStatus })
$startItem.add_Click({ Start-CsService })
$stopItem.add_Click({ Stop-CsService })
$exitItem.add_Click({
    $notify.Visible = $false
    [System.Windows.Forms.Application]::Exit()
})
$stopExitItem.add_Click({
    Stop-CsService
    $notify.Visible = $false
    [System.Windows.Forms.Application]::Exit()
})
$notify.add_DoubleClick({ Open-CsDashboard })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 100
$timer.add_Tick({
    $ctrl = ([int][CsTrayKeyState]::GetAsyncKeyState(0x11) -band 0x8000) -ne 0
    $alt = ([int][CsTrayKeyState]::GetAsyncKeyState(0x12) -band 0x8000) -ne 0
    $c = ([int][CsTrayKeyState]::GetAsyncKeyState(0x43) -band 0x8000) -ne 0
    $hot = $ctrl -and $alt -and $c
    if ($hot -and -not $script:hotDown) {
        Open-CsDashboard
    }
    $script:hotDown = $hot

    if ((Get-Date) -ge $script:nextRefresh) {
        Refresh-CsStatus
        $script:nextRefresh = (Get-Date).AddSeconds(10)
    }
})

if ($SelfTest) {
    $timer.Stop()
    $notify.Visible = $false
    $notify.Dispose()
    Write-Output "tray self-test ok"
    exit 0
}

Refresh-CsStatus
$timer.Start()
try {
    [System.Windows.Forms.Application]::Run()
} finally {
    $timer.Stop()
    $notify.Visible = $false
    $notify.Dispose()
}
