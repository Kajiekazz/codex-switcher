param(
    [string]$Python = 'python',
    [string]$Iscc = '',
    [ValidateSet('x64', 'arm64')][string]$Architecture = '',
    [switch]$SkipInstaller
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $nativeArch = (& $Python -c 'import sysconfig; print("arm64" if "arm64" in sysconfig.get_platform() else "x64")').Trim()
    if (-not $Architecture) { $Architecture = $nativeArch }
    if ($Architecture -ne $nativeArch) { throw 'Use a native Python interpreter and runner for the target architecture.' }
    & $Python packaging\build_metadata.py
    if ($LASTEXITCODE -ne 0) { throw 'Build metadata failed.' }
    & $Python -m PyInstaller --noconfirm --distpath dist --workpath build packaging\CodexSwitcher.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    if (-not $SkipInstaller) {
        if (-not $Iscc) {
            $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
            $candidates = @("${env:ProgramFiles}\Inno Setup 7\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe")
            if ($command) { $Iscc = $command.Source }
            else { $Iscc = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1 }
        }
        if (-not $Iscc) { throw 'Install Inno Setup or pass -Iscc <path-to-ISCC.exe>.' }
        $version = (& $Python -c 'from codex_switcher import __version__; print(__version__)').Trim()
        & $Iscc "/DAppVersion=$version" "/DTargetArch=$Architecture" packaging\installer.iss
        if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
        $installer = Join-Path $root "dist\CodexSwitcher-$version-windows-$Architecture-Setup.exe"
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
        "$hash  $(Split-Path -Leaf $installer)" | Set-Content -LiteralPath "$installer.sha256" -Encoding ascii
        Write-Output $installer
    }
} finally { Pop-Location }
