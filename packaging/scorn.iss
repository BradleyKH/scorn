; Compile with: ISCC /DAppVersion=x.y.z /DBundleDir=<abs bundle folder> /DOutputDir=<abs output folder> scorn.iss
; scripts/build-installer.sh passes all three.

#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z
#endif
#ifndef BundleDir
  #error Pass /DBundleDir=<folder produced by scripts/build-bundle.sh>
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
#ifndef MaxRelPath
  #error Pass /DMaxRelPath=<length of the longest file path inside the bundle>
#endif

[Setup]
; Upgrades and uninstall key on AppId, so it must never change.
AppId={{A1F2A9B5-3DB9-42AB-AAE1-C30382E6CA35}
AppName=Scorn
AppVersion={#AppVersion}
AppPublisher=Bradley Harris
AppPublisherURL=https://github.com/BradleyKH/scorn
AppSupportURL=https://github.com/BradleyKH/scorn/issues
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Scorn
DisableProgramGroupPage=yes
DisableDirPage=auto
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile={#BundleDir}\scorn.ico
UninstallDisplayIcon={app}\scorn.ico
Compression=lzma2/ultra64
SolidCompression=yes
OutputDir={#OutputDir}
OutputBaseFilename=Scorn-Setup-{#AppVersion}
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[InstallDelete]
; An upgrade must not leave a previous version's packages behind.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\audiveris"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Scorn"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m scorn.app"; WorkingDir: "{app}"; IconFilename: "{app}\scorn.ico"
Name: "{autodesktop}\Scorn"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m scorn.app"; WorkingDir: "{app}"; IconFilename: "{app}\scorn.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m scorn.app"; WorkingDir: "{app}"; Description: "Launch Scorn"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; {app} leftovers are runtime __pycache__ folders. Results in Documents\Scorn are deliberately never touched.
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{localappdata}\Scorn"

[Code]
// Restart Manager cannot close pythonw.exe (no window) or Audiveris's java.exe, which would lock
// files under {app}. Only processes whose image lives inside the install folder are ended.
procedure EndProcessesUnderApp;
var
  ResultCode: Integer;
  Cmd: String;
begin
  Cmd := '-NoProfile -NonInteractive -Command "$d = ''' + RemoveBackslash(ExpandConstant('{app}')) +
    '\''; Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and ' +
    '$_.ExecutablePath.StartsWith($d, [System.StringComparison]::OrdinalIgnoreCase) } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; ' +
    'Start-Sleep -Milliseconds 500"';
  Exec('powershell.exe', Cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// Python and Java do not opt in to long paths, so every bundled file must stay within MAX_PATH
// (259 characters plus the terminator) once installed.
function MaxInstallDirLength: Integer;
begin
  Result := 259 - 1 - {#MaxRelPath};
end;

function InstallDirTooLongMessage(Dir: String): String;
begin
  Result := '';
  if Length(RemoveBackslash(Dir)) > MaxInstallDirLength then
    Result := 'The install folder path is too long for Windows. Choose a folder whose full path is ' +
      IntToStr(MaxInstallDirLength) + ' characters or fewer.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Msg: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Msg := InstallDirTooLongMessage(WizardDirValue);
    if Msg <> '' then
    begin
      MsgBox(Msg, mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  // Catches /DIR= on silent installs, which skip the folder page.
  Result := InstallDirTooLongMessage(ExpandConstant('{app}'));
  if Result <> '' then
    exit;
  if DirExists(ExpandConstant('{app}')) then
    EndProcessesUnderApp;
  Result := '';
end;

function InitializeUninstall: Boolean;
begin
  EndProcessesUnderApp;
  Result := True;
end;
