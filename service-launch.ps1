# Shared launcher: the Python service owns its console and log handles.
# Dot-source this file; loading it does not start or stop any process.

function Get-CsServiceHealthUrl {
    $listenHost = if ($env:CODEX_SWITCHER_HOST) { $env:CODEX_SWITCHER_HOST } else { '127.0.0.1' }
    $listenPort = if ($env:CODEX_SWITCHER_PORT) { $env:CODEX_SWITCHER_PORT } else { '8787' }
    $dataPath = if ($env:CODEX_SWITCHER_DATA) { $env:CODEX_SWITCHER_DATA } else { Join-Path $env:LOCALAPPDATA 'CodexSwitcher' }
    $settingsFile = Join-Path $dataPath 'settings.json'
    if (Test-Path -LiteralPath $settingsFile) {
        try {
            $values = (Get-Content -LiteralPath $settingsFile -Raw | ConvertFrom-Json).values
            if ($values.host) { $listenHost = $values.host }
            if ($values.port) { $listenPort = $values.port }
        } catch { }
    }
    if ($listenHost -in @('', '0.0.0.0', '::')) { $listenHost = '127.0.0.1' }
    if ($listenHost.Contains(':')) { $listenHost = '[' + $listenHost.Trim('[', ']') + ']' }
    return "http://${listenHost}:${listenPort}/api/health"
}

function Write-CsLaunchEvent {
    param([string]$LogDirectory, [hashtable]$EventData)
    $EventData.time = (Get-Date).ToUniversalTime().ToString('o')
    $EventData.launcher_pid = $PID
    $line = $EventData | ConvertTo-Json -Compress
    Add-Content -LiteralPath (Join-Path $LogDirectory 'service-launcher.log') -Value $line -Encoding UTF8
}

function Start-CsServiceProcess {
    param([string]$PythonPath, [string]$ProjectRoot, [string]$LogDirectory = $ProjectRoot)
    $entry = Join-Path $ProjectRoot 'service.py'
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw "Service entry point not found: $entry"
    }
    $env:PYTHONIOENCODING = 'utf-8'
    $process = Start-Process -FilePath $PythonPath -ArgumentList @('-u', ('"' + $entry + '"')) `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDirectory 'service.out.log') `
        -RedirectStandardError (Join-Path $LogDirectory 'service.err.log')
    # Windows PowerShell 5 otherwise may lose the exit code of a short-lived
    # child before WaitForExit opens its process handle.
    $null = $process.Handle
    try {
        Write-CsLaunchEvent -LogDirectory $LogDirectory -EventData @{event='started'; service_pid=$process.Id}
    } catch {
        Write-Warning "Could not record service launch: $_"
    }
    return $process
}

function Wait-CsServiceProcess {
    param([System.Diagnostics.Process]$Process, [string]$LogDirectory)
    try {
        while (-not $Process.HasExited) {
            Start-Sleep -Milliseconds 500
        }
        $Process.WaitForExit()
        $code = $Process.ExitCode
        Write-CsLaunchEvent -LogDirectory $LogDirectory -EventData @{event='exited'; service_pid=$Process.Id; exit_code=$code}
        if ($code -ne 0) {
            Write-Host "Service exited (code $code). See service.err.log and service-launcher.log." -ForegroundColor Red
        } else {
            Write-Host 'Service stopped.'
        }
        return $code
    } finally {
        # Closing/cancelling the launcher must not terminate the service.
        if (-not $Process.HasExited) {
            try {
                Write-CsLaunchEvent -LogDirectory $LogDirectory -EventData @{event='launcher_detached'; service_pid=$Process.Id}
            } catch { }
        }
    }
}
