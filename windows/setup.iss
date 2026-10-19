; Inno Setup script for Blink on Windows. Built by windows/build, which passes the version:
;   ISCC.exe /DAppVersion=7.1.5 windows\setup.iss
; Paths are relative to this file: the PyInstaller output is ..\dist\blink, the installer
; goes to ..\dist\Blink-Installer-<version>.exe

#ifndef AppVersion
  #error AppVersion is not defined: build with windows/build, or pass /DAppVersion=X.Y.Z
#endif

[Setup]
AppId={{AA4328C3-006F-49F0-94F4-0BA659FCB6A5}
AppName=Blink
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher=AG Projects
AppPublisherURL=https://ag-projects.com
AppSupportURL=https://icanblink.com
AppUpdatesURL=https://icanblink.com
DefaultDirName={autopf}\Blink
DefaultGroupName=Blink
AllowNoIcons=yes
LicenseFile=distribution\license.txt
InfoBeforeFile=distribution\pre_setup.txt
OutputDir=..\dist
OutputBaseFilename=Blink-Installer-{#AppVersion}
SetupIconFile=..\resources\icons\blink.ico
UninstallDisplayIcon={app}\blink.exe
WizardImageFile=distribution\blink_setup_panel.bmp, distribution\blink_setup_panel1.bmp
WizardSmallImageFile=distribution\blink_setup_top_icon.bmp
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\blink\blink.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\blink\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "distribution\bonjoursdksetup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: CheckBonjour
Source: "distribution\psvince.dll"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; an update replaces the whole program folder (user data is in %APPDATA%\Blink)
Type: filesandordirs; Name: "{app}\*.*"

[Icons]
Name: "{group}\Blink"; Filename: "{app}\blink.exe"; WorkingDir: "{app}"
Name: "{group}\Website"; Filename: "https://icanblink.com"
Name: "{group}\{cm:UninstallProgram,Blink}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Blink"; Filename: "{app}\blink.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\bonjoursdksetup.exe"; Check: CheckBonjour
Filename: "{app}\blink.exe"; Description: "{cm:LaunchProgram,Blink}"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKCU; Subkey: "Software\AG Projects\Blink"; Flags: uninsdeletekey

[Code]
function IsModuleLoaded(modulename: String ): Boolean;
external 'IsModuleLoaded@files:psvince.dll stdcall setuponly';

function IsModuleLoadedU(modulename: String ): Boolean;
external 'IsModuleLoaded@{app}\psvince.dll stdcall uninstallonly';

{ the Bonjour SDK installs system wide: offered only to administrators who do not have it }
function CheckBonjour: Boolean;
begin
  if IsAdminLoggedOn() or IsPowerUserLoggedOn() then
    Result := not RegKeyExists(HKEY_LOCAL_MACHINE, 'Software\Apple Inc.\Bonjour')
  else
    Result := False;
end;

function InitializeSetup(): Boolean;
begin
  while IsModuleLoaded('blink.exe') do
  begin
    if MsgBox('Blink is currently running. To proceed with the update, close Blink and then press OK.', mbConfirmation, MB_OKCANCEL) <> IDOK then
    begin
      Result := False;
      Exit;
    end
  end;
  Result := True;
end;

function InitializeUninstall(): Boolean;
begin
  if IsModuleLoadedU('blink.exe') then
  begin
    MsgBox('Blink is currently running, please close it and run uninstall again.', mbError, MB_OK);
    Result := False;
  end else
    Result := True;
  UnloadDLL(ExpandConstant('{app}\psvince.dll'));
end;
