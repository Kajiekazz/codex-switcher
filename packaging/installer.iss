#ifndef AppVersion
  #define AppVersion "0.7.0"
#endif
#ifndef AppSource
  #define AppSource "..\dist\CodexSwitcher"
#endif
#ifndef TargetArch
  #define TargetArch "x64"
#endif
#if TargetArch == "arm64"
  #define AllowedArch "arm64"
#else
  #define AllowedArch "x64compatible"
#endif

[Setup]
AppId={{5FF02B42-F086-411C-91AA-211FD77A948D}
AppName=Codex Switcher
AppVersion={#AppVersion}
AppPublisher=Kajiekazz
AppPublisherURL=https://github.com/Kajiekazz/codex-switcher/
DefaultDirName={localappdata}\Programs\Codex Switcher
DefaultGroupName=Codex Switcher
PrivilegesRequired=lowest
ArchitecturesAllowed={#AllowedArch}
ArchitecturesInstallIn64BitMode={#AllowedArch}
MinVersion=10.0
UninstallDisplayIcon={app}\CodexSwitcher.exe
OutputDir=..\dist
OutputBaseFilename=CodexSwitcher-{#AppVersion}-windows-{#TargetArch}-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Codex Switcher"; Filename: "{app}\CodexSwitcher.exe"
Name: "{autodesktop}\Codex Switcher"; Filename: "{app}\CodexSwitcher.exe"

[Run]
Filename: "{app}\CodexSwitcher.exe"; Description: "启动 Codex Switcher / Launch Codex Switcher"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\CodexSwitcher.exe"; Parameters: "--exit"; Flags: runhidden waituntilterminated; RunOnceId: "StopSwitcher"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "CodexSwitcher"; Flags: uninsdeletevalue

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\CodexSwitcher.exe')) then
  begin
    Exec(ExpandConstant('{app}\CodexSwitcher.exe'), '--exit', '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
    Sleep(1200);
  end;
end;
