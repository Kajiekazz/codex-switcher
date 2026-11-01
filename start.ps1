param(
    [switch]$EnableLogin,
    [switch]$EnableSwitch,
    [switch]$EnableAutoSwitch,
    [switch]$EnableAutoReset,
    [switch]$EnableGoalResume,
    [switch]$RestartDesktop,
    [switch]$Tray,
    [ValidateSet("manual", "playwright")]
    [string]$Driver = "manual"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "Starting Codex Switcher v0.6..."
if ($EnableSwitch) {
    $env:CODEX_SWITCHER_ENABLE_SWITCH = "1"
    Write-Host "Switching: ENABLED / backup + atomic write + verify + rollback"
} else {
    $env:CODEX_SWITCHER_ENABLE_SWITCH = "0"
    Write-Host "Switching: DISABLED (use -EnableSwitch to opt in)"
}

if ($EnableAutoSwitch) {
    $env:CODEX_SWITCHER_ENABLE_AUTO_SWITCH = "1"
    Write-Host "Auto switch: ENABLED / quota probe + cooldown + circuit breaker"
} else {
    $env:CODEX_SWITCHER_ENABLE_AUTO_SWITCH = "0"
    Write-Host "Auto switch: DISABLED (use -EnableAutoSwitch to opt in)"
}

if ($EnableAutoReset) {
    $env:CODEX_SWITCHER_ENABLE_AUTO_RESET = "1"
    Write-Host "Auto reset: ENABLED / only after the pool has no usable account (spends a real credit)"
} else {
    $env:CODEX_SWITCHER_ENABLE_AUTO_RESET = "0"
    Write-Host "Auto reset: DISABLED (use -EnableAutoReset to opt in)"
}

if ($EnableGoalResume) {
    $env:CODEX_SWITCHER_ENABLE_GOAL_RESUME = "1"
    Write-Host "Goal resume: ENABLED (selected usage_limited goals; dry-run by default)"
} else {
    $env:CODEX_SWITCHER_ENABLE_GOAL_RESUME = "0"
    Write-Host "Goal resume: DISABLED (use -EnableGoalResume to opt in)"
}

if ($EnableLogin) {
    $env:CODEX_SWITCHER_ENABLE_LOGIN = "1"
    $env:CODEX_SWITCHER_LOGIN_DRIVER = $Driver
    Write-Host "Login assistant: ENABLED / driver=$Driver / isolated CODEX_HOME"
    if ($Driver -eq "playwright") {
        Write-Host "Playwright mode: keep the automation Chrome window visible."
    }
} else {
    $env:CODEX_SWITCHER_ENABLE_LOGIN = "0"
    Write-Host "Login assistant: DISABLED (use -EnableLogin to opt in)"
}

if ($RestartDesktop) {
    $env:CODEX_SWITCHER_RESTART_DESKTOP = "1"
    Write-Host "Desktop restart: ENABLED after switch (ChatGPT process tree only)"
} else {
    $env:CODEX_SWITCHER_RESTART_DESKTOP = "0"
    Write-Host "Desktop restart: DISABLED for auto-switch (dashboard can force per switch)"
}

if ($Tray) {
    $python = (Get-Command python -ErrorAction Stop).Source
    $running = $false
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:8787/api/health" -TimeoutSec 2 | Out-Null
        $running = $true
    } catch { }

    if (-not $running) {
        Write-Host "Starting service in background..."
        Start-Process -FilePath $python -ArgumentList "service.py" -WorkingDirectory $root -WindowStyle Hidden | Out-Null
        $ready = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 500
            try {
                Invoke-RestMethod -Uri "http://127.0.0.1:8787/api/health" -TimeoutSec 2 | Out-Null
                $ready = $true
                break
            } catch { }
        }
        if (-not $ready) {
            Write-Host "Service did not become ready on 127.0.0.1:8787."
            exit 1
        }
    } else {
        Write-Host "Service already running; existing env flags are unchanged."
        Write-Host "Run .\stop.ps1 first if you changed -EnableSwitch/-EnableAutoSwitch/-EnableAutoReset."
    }
    & "$root\tray.ps1"
    exit
}

python service.py
