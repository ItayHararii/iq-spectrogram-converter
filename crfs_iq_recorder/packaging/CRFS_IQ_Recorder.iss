; Inno Setup script for CRFS IQ Recorder (installer for managed PCs).
;
; Why an installer in addition to the portable one-file EXE:
;   - The one-file EXE unpacks ~90 DLL/PYD files into %TEMP%\_MEIxxxx at every
;     launch. AppLocker and App Control (WDAC) path rules on company PCs often
;     forbid running code from %TEMP%, so the portable EXE "cannot run at all"
;     there even when signed. This installer puts a folder build into
;     Program Files, which those rules allow, and gives IT a fixed path and a
;     signed installer/uninstaller to approve.
;   - Start-menu shortcut, clean uninstall, standard silent switches
;     (/VERYSILENT /SUPPRESSMSGBOXES /NORESTART) for Intune/SCCM deployment.
;
; Built by build_exe.bat when CRFS_BUILD_INSTALLER=1 (needs Inno Setup 6,
; "winget install JRSoftware.InnoSetup"). Compile manually with:
;   ISCC.exe /DAppVersion=1.8.10 /DSourceDir=..\dist\onedir\CRFS_IQ_Recorder packaging\CRFS_IQ_Recorder.iss
; Add /DSign /S"crfs=<command> $f" to sign Setup and the uninstaller.
;
; Repackaging is not a trust mechanism: an unsigned installer gets the same
; SmartScreen / Smart App Control treatment as an unsigned EXE. Signing is what
; identifies the publisher; the installer only fixes the %TEMP% problem.
;
; Install location: Defender's "Controlled folder access" (Ransomware
; protection) lets only programs Microsoft already trusts change files in
; Documents, Desktop, Pictures, Videos, Music and Favorites. The uninstaller is
; a new unsigned program every release, so an installation inside one of those
; folders can end up impossible to uninstall (Defender event 1123, "Some
; elements could not be removed"). Program Files and %LOCALAPPDATA%\Programs
; are not protected. The [Code] section warns before installing into a
; protected folder and explains a blocked uninstall instead of leaving the
; user guessing.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\onedir\CRFS_IQ_Recorder"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#define AppName "CRFS IQ Recorder"
#define AppPublisher "Sensorz"
#define AppExeName "CRFS_IQ_Recorder.exe"

[Setup]
; Never change AppId: it is how Windows and future installers recognise this app.
AppId={{412D456B-4D20-4833-9B9D-1CA45E84B2DB}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Admin install goes to Program Files; a standard user may still install for
; themselves under %LOCALAPPDATA%\Programs (the dialog offers both).
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=CRFS_IQ_Recorder_Setup
SetupIconFile=..\assets\sensorz_icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no
#ifdef Sign
SignTool=crfs
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; AppUserModelID: "Sensorz.CRFSIQRecorder"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon; AppUserModelID: "Sensorz.CRFSIQRecorder"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; Settings, recording history and the Excel queue live in %LOCALAPPDATA%\CRFS IQ Recorder
; and are deliberately kept on uninstall.

[Code]
const
  CSIDL_PERSONAL = $0005;                 // Documents
  CSIDL_FAVORITES = $0006;
  CSIDL_MYMUSIC = $000D;
  CSIDL_MYVIDEO = $000E;
  CSIDL_DESKTOPDIRECTORY = $0010;
  CSIDL_COMMON_DESKTOPDIRECTORY = $0019;
  CSIDL_MYPICTURES = $0027;
  CSIDL_COMMON_DOCUMENTS = $002E;
  CSIDL_COMMON_MUSIC = $0035;
  CSIDL_COMMON_PICTURES = $0036;
  CSIDL_COMMON_VIDEO = $0037;
  CFA_KEY = 'SOFTWARE\Microsoft\Windows Defender\Windows Defender Exploit Guard\Controlled Folder Access';
  CFA_POLICY_KEY = 'SOFTWARE\Policies\Microsoft\Windows Defender\Windows Defender Exploit Guard\Controlled Folder Access';

function IsInsideFolder(const Path, Folder: String): Boolean;
var
  P, F: String;
begin
  P := AddBackslash(Lowercase(Path));
  F := AddBackslash(Lowercase(Folder));
  Result := (Length(F) > 3) and (Copy(P, 1, Length(F)) = F);
end;

function CsidlFolder(Index: Integer): String;
var
  Id: Integer;
begin
  case Index of
    0: Id := CSIDL_PERSONAL;
    1: Id := CSIDL_DESKTOPDIRECTORY;
    2: Id := CSIDL_MYPICTURES;
    3: Id := CSIDL_MYVIDEO;
    4: Id := CSIDL_MYMUSIC;
    5: Id := CSIDL_FAVORITES;
    6: Id := CSIDL_COMMON_DOCUMENTS;
    7: Id := CSIDL_COMMON_DESKTOPDIRECTORY;
    8: Id := CSIDL_COMMON_PICTURES;
    9: Id := CSIDL_COMMON_VIDEO;
  else
    Id := CSIDL_COMMON_MUSIC;
  end;
  Result := GetShellFolderByCSIDL(Id, False);
end;

{ Returns the Controlled-folder-access protected folder that contains Dir,
  or '' when Dir is outside all of them. Covers the Windows defaults (which
  follow OneDrive known-folder redirection) plus folders added by the user or
  by IT policy, when the registry allows reading them. }
function GuardedFolderContaining(const Dir: String): String;
var
  I: Integer;
  Folder: String;
  Names: TArrayOfString;
begin
  Result := '';
  for I := 0 to 10 do
  begin
    Folder := CsidlFolder(I);
    if (Folder <> '') and IsInsideFolder(Dir, Folder) then
    begin
      Result := Folder;
      exit;
    end;
  end;
  if RegGetValueNames(HKLM, CFA_KEY + '\ProtectedFolders', Names) then
    for I := 0 to GetArrayLength(Names) - 1 do
      if IsInsideFolder(Dir, Names[I]) then
      begin
        Result := Names[I];
        exit;
      end;
  if RegGetValueNames(HKLM, CFA_POLICY_KEY + '\ProtectedFolders', Names) then
    for I := 0 to GetArrayLength(Names) - 1 do
      if IsInsideFolder(Dir, Names[I]) then
      begin
        Result := Names[I];
        exit;
      end;
end;

{ 1 = block, 2 = audit, 0 = off; -1 when the value cannot be read. }
function ControlledFolderAccessState: Integer;
var
  V: Cardinal;
begin
  Result := -1;
  if RegQueryDWordValue(HKLM, CFA_POLICY_KEY, 'EnableControlledFolderAccess', V) then
    Result := V
  else if RegQueryDWordValue(HKLM, CFA_KEY, 'EnableControlledFolderAccess', V) then
    Result := V;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Guarded, State, Msg: String;
begin
  Result := True;
  if CurPageID <> wpSelectDir then
    exit;
  Guarded := GuardedFolderContaining(WizardDirValue);
  if Guarded = '' then
    exit;
  case ControlledFolderAccessState of
    1: State := 'Ransomware protection (Controlled folder access) is turned on for this PC.';
    0: State := 'Ransomware protection (Controlled folder access) is currently off on this PC, but it may be turned on later.';
  else
    State := 'Windows Defender ransomware protection (Controlled folder access) guards this location when it is turned on.';
  end;
  Log(Format('Chosen folder "%s" is inside protected folder "%s" (Controlled folder access state %d).', [WizardDirValue, Guarded, ControlledFolderAccessState]));
  if WizardSilent then
    exit;
  Msg := 'The folder you chose is inside:' + #13#10#13#10 + Guarded + #13#10#13#10 + State + ' '
    + 'Windows can then stop this program from installing or later uninstalling here, '
    + 'and the uninstaller would leave the files behind.' + #13#10#13#10
    + 'Recommended: click No and keep the default folder (Program Files, or '
    + ExpandConstant('{localappdata}\Programs') + ' for a personal install).' + #13#10#13#10
    + 'Install here anyway?';
  Result := MsgBox(Msg, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir, Guarded, Msg: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  AppDir := ExpandConstant('{app}');
  if not FileExists(AddBackslash(AppDir) + '{#AppExeName}') then
    exit;
  Guarded := GuardedFolderContaining(AppDir);
  if Guarded <> '' then
  begin
    Log(Format('Program files in "%s" were not removed; folder is under protected "%s" (Controlled folder access).', [AppDir, Guarded]));
    Msg := 'Windows did not let the uninstaller remove the program files in:' + #13#10#13#10 + AppDir + #13#10#13#10
      + 'This is Windows Defender ransomware protection (Controlled folder access) blocking an unknown program '
      + 'from changing files under ' + Guarded + '.' + #13#10#13#10
      + 'Everything else has been uninstalled. Delete that folder in File Explorer to finish. '
      + 'Next time install to the default folder, which is not protected.';
  end
  else
  begin
    Log(Format('Program files in "%s" were not removed (file in use or access denied).', [AppDir]));
    Msg := 'The uninstaller could not remove the program files in:' + #13#10#13#10 + AppDir + #13#10#13#10
      + 'Close CRFS IQ Recorder if it is still running, then delete that folder in File Explorer.';
  end;
  if not UninstallSilent then
    MsgBox(Msg, mbInformation, MB_OK);
end;
