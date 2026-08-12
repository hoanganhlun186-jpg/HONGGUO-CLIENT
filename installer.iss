; ============================================================
;  installer.iss — Script cài đặt BOOM STUDIO (Inno Setup)
;  Build tự thay {#AppVer} bằng số phiên bản qua tham số /D.
; ============================================================
#ifndef AppVer
  #define AppVer "1.0.0"
#endif

[Setup]
AppName=BOOM STUDIO
AppVersion={#AppVer}
AppPublisher=BOOM STUDIO
DefaultDirName={autopf}\BoomStudio
DefaultGroupName=BOOM STUDIO
DisableProgramGroupPage=yes
OutputBaseFilename=BoomStudio_Setup
OutputDir=.
Compression=lzma2
SolidCompression=yes
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\BoomStudio.exe
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; lowest = cài vào %LocalAppData%\Programs\BoomStudio, KHÔNG cần quyền admin,
; để cơ chế auto-update (ghi đè thư mục) hoạt động được.
PrivilegesRequired=lowest

[Languages]
Name: "vi"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Tao shortcut ngoai man hinh Desktop"; GroupDescription: "Tuy chon them:"

[Files]
Source: "BoomStudio\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\BOOM STUDIO"; Filename: "{app}\BoomStudio.exe"
Name: "{group}\Go cai dat BOOM STUDIO"; Filename: "{uninstallexe}"
Name: "{autodesktop}\BOOM STUDIO"; Filename: "{app}\BoomStudio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\BoomStudio.exe"; Description: "Mo BOOM STUDIO ngay"; Flags: nowait postinstall skipifsilent
