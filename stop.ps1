$ErrorActionPreference = "Stop"
$conn = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
if (-not $conn) {
    Write-Host "Codex Switcher is not running on 127.0.0.1:8787."
    exit 0
}
$ownerPid = $conn.OwningProcess
Stop-Process -Id $ownerPid -Force
Write-Host "Stopped Codex Switcher (PID $ownerPid)."
