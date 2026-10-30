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
;   ISCC.exe /DAppVersion=1.8.6 /DSourceDir=..\dist\onedir\CRFS_IQ_Recorder packaging\CRFS_IQ_Recorder.iss
; Add /DSign /S"crfs=<command> $f" to sign Setup and the uninstaller.
;
; Repackaging is not a trust mechanism: an unsigned installer gets the same
; SmartScreen / Smart App Control treatment as an unsigned EXE. Signing is what
; identifies the publisher; the installer only fixes the %TEMP% problem.

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
