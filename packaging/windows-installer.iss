; Inno Setup script for the Windows installer.
; Compiled in CI: ISCC.exe /DAppVersion=<version> packaging\windows-installer.iss
; Input:  build\windows\  (flet build output — exe + DLLs + runtime)
; Output: email-export-import-windows-setup.exe (repo root)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Fixed AppId so a newer setup upgrades the existing install in place.
AppId={{7C4A9E52-1B8D-4F36-9D2E-A0C5B61F84D3}
AppName=Email Export Import Tool
AppVersion={#AppVersion}
AppPublisher=cKurultayKalkan
AppPublisherURL=https://github.com/cKurultayKalkan/email-export-import
AppSupportURL=https://github.com/cKurultayKalkan/email-export-import/issues
DefaultDirName={autopf}\Email Export Import Tool
DefaultGroupName=Email Export Import Tool
DisableProgramGroupPage=yes
; Per-user install: no admin prompt, and {autopf} resolves to the user's
; local Programs folder.
PrivilegesRequired=lowest
OutputDir=..
OutputBaseFilename=email-export-import-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\email-export-import.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\build\windows\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Email Export Import Tool"; Filename: "{app}\email-export-import.exe"
Name: "{autodesktop}\Email Export Import Tool"; Filename: "{app}\email-export-import.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\email-export-import.exe"; Description: "{cm:LaunchProgram,Email Export Import Tool}"; Flags: nowait postinstall skipifsilent
