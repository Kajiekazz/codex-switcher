@echo off
rem Restart the local Codex Switcher service with the dev env.
rem Usage:  restart-dev.bat            (goal-resume trace off)
rem         restart-dev.bat trace      (goal-resume trace on)
rem Note:   run it directly (double-click / bare console). If you pipe this
rem         script somewhere, the started service inherits the pipe handle and
rem         the pipe will not close until the service exits.
setlocal
cd /d "%~dp0"

set TRACE=
if /i "%~1"=="trace" set TRACE=1

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" >nul 2>&1
ping -n 3 127.0.0.1 >nul

set CODEX_SWITCHER_ENABLE_AUTO_SWITCH=1
set CODEX_SWITCHER_ENABLE_LOGIN=1
set CODEX_SWITCHER_LOGIN_DRIVER=playwright
set CODEX_SWITCHER_LOGIN_CONCURRENCY=2
set CODEX_SWITCHER_ENABLE_GOAL_RESUME=1
set CODEX_SWITCHER_RESTART_DESKTOP=1
if defined TRACE set CODEX_SWITCHER_GOAL_RESUME_TRACE=1

rem Start-Process gives the service its own handles and a hidden window, so this
rem script returns right away even when its own stdout is piped somewhere else.
rem (A plain `start cmd /c ...` keeps the inherited pipe open and callers hang.)
powershell -NoProfile -Command "Start-Process -FilePath (Get-Command python).Source -ArgumentList 'service.py' -WorkingDirectory '%CD%' -WindowStyle Hidden -RedirectStandardOutput '%CD%\service.out.log' -RedirectStandardError '%CD%\service.err.log'"

for /l %%i in (1,1,40) do (
    powershell -NoProfile -Command "try { Invoke-RestMethod 'http://127.0.0.1:8787/api/health' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" && goto ready
    ping -n 2 127.0.0.1 >nul
)
echo health check failed, see service.err.log
exit /b 1

:ready
echo service restarted (port 8787, trace=%TRACE%)
exit /b 0
