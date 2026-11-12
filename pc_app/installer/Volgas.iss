; Volgas installer (Inno Setup 6.3+). Installs Volgas.exe for the current user,
; registers it with Windows (Start menu, search, Settings -> Apps with an
; Uninstall button) and needs no admin rights.
;
; Per-user on purpose: the in-app updater swaps Volgas.exe in place
; (_install_update in volgas.py), which only works where the user can write.
; Under Program Files every update would need admin rights.
;
; Build after PyInstaller, from pc_app:
;   iscc /DAppVersion=0.3.0 installer\Volgas.iss
; Output: dist\VolgasSetup.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Never change AppId: Windows uses it to recognise later installs as upgrades
; of this one. volgas.py (INSTALLER_APP_ID) reads the same uninstall entry.
AppId={{A20B26BF-623F-447E-8E63-8A219892D06C}
AppName=Volgas
AppVersion={#AppVersion}
AppVerName=Volgas {#AppVersion}
AppPublisher=rhyss08
AppPublisherURL=https://github.com/rhyss08/Volgas
AppSupportURL=https://github.com/rhyss08/Volgas/issues
AppUpdatesURL=https://github.com/rhyss08/Volgas/releases
DefaultDirName={localappdata}\Programs\Volgas
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=VolgasSetup
SetupIconFile=..\assets\volgas.ico
UninstallDisplayIcon={app}\Volgas.exe
UninstallDisplayName=Volgas
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
; Close a running Volgas before replacing it, instead of failing on a locked file.
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\Volgas.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Volgas"; Filename: "{app}\Volgas.exe"
Name: "{autodesktop}\Volgas"; Filename: "{app}\Volgas.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Volgas.exe"; Description: "{cm:LaunchProgram,Volgas}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Left behind by the in-app updater. Settings and logs in %APPDATA%\Volgas are
; kept on purpose, so a reinstall picks up where the user left off.
Type: files; Name: "{app}\Volgas.exe.old"
Type: files; Name: "{app}\Volgas.exe.new"
