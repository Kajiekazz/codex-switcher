param(
    [switch]$EnableLogin,
    [switch]$EnableSwitch, # Legacy argument, manual switching is always available.
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
. (Join-Path $root 'service-launch.ps1')

Write-Host "Starting Codex Switcher v0.6..."
Write-Host "Switching: AVAILABLE / backup + atomic write + verify + rollback"
$healthUrl = Get-CsServiceHealthUrl
$running = $false
try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    $running = $health.ok -and $health.switching_implemented
} catch { }
if ($running -and -not $Tray) {
    Write-Host "Service already running at $healthUrl; its process and logs are unchanged."
    Write-Host 'Use .\stop.ps1 before starting with different options.'
    return
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
    if (-not $running) {
        Write-Host "Starting service in background..."
        Start-CsServiceProcess -PythonPath $python -ProjectRoot $root | Out-Null
        $ready = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 500
            try {
                Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2 | Out-Null
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
        Write-Host "Run .\stop.ps1 first if you changed -EnableAutoSwitch/-EnableAutoReset."
    }
    & "$root\tray.ps1"
    exit
}

$python = (Get-Command python -ErrorAction Stop).Source
$service = Start-CsServiceProcess -PythonPath $python -ProjectRoot $root
Write-Host "Service process: $($service.Id) (independent of this console)"
Write-Host "Logs: $root\service.out.log / service.err.log / service-launcher.log"
Write-Host 'Use .\stop.ps1 to stop the service. Closing this window leaves it running.'
$serviceExitCode = Wait-CsServiceProcess -Process $service -LogDirectory $root
if ($serviceExitCode -ne 0 -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
    $null = Read-Host 'Press Enter to close the launcher'
}
