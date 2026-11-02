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

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "CodexSwitcher"; Flags: uninsdeletevalue

[Code]
function CsFindWindow(ClassName, WindowName: String): HWND;
  external 'FindWindowW@user32.dll stdcall';
function CsPostMessage(Window: HWND; Msg: Cardinal; WParam, LParam: Integer): Boolean;
  external 'PostMessageW@user32.dll stdcall';

function StopRunningSwitcher(): Boolean;
var
  DataDir, Contents, Tail, PidText: String;
  Raw: AnsiString;
  Offset, I, Attempt: Integer;
  Window: HWND;
begin
  Result := True;
  DataDir := GetEnv('CODEX_SWITCHER_DATA');
  if DataDir = '' then DataDir := ExpandConstant('{localappdata}\CodexSwitcher');
  if not LoadStringFromFile(AddBackslash(DataDir) + 'app-instance.json', Raw) then Exit;
  Contents := String(Raw);
  Offset := Pos('"pid":', Contents);
  if Offset = 0 then Exit;
  Tail := Trim(Copy(Contents, Offset + 6, 20));
  PidText := '';
  for I := 1 to Length(Tail) do
  begin
    if (Tail[I] < '0') or (Tail[I] > '9') then Break;
    PidText := PidText + Tail[I];
  end;
  if PidText = '' then Exit;
  Window := CsFindWindow('CodexSwitcherTray_' + PidText, 'Codex Switcher');
  if Window = 0 then Exit;
  Log('Requesting the running Switcher tray to exit.');
  CsPostMessage(Window, $0010, 0, 0);
  for Attempt := 1 to 200 do
  begin
    if not FileExists(AddBackslash(DataDir) + 'app-instance.json') then
    begin
      Sleep(250);
      Exit;
    end;
    Sleep(100);
  end;
  Result := False;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not StopRunningSwitcher() then
    Result := 'Please exit Codex Switcher from the tray and retry. / 请先从托盘退出 Codex Switcher 后重试。';
end;

function InitializeUninstall(): Boolean;
begin
  Result := StopRunningSwitcher();
end;
