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
; VC++ Runtime — ctranslate2 (faster-whisper) cần, may khach moi cai Win hay thieu.
; Chep vao thu muc tam, cai xong tu xoa.
Source: "vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\BOOM STUDIO"; Filename: "{app}\BoomStudio.exe"
Name: "{group}\Go cai dat BOOM STUDIO"; Filename: "{uninstallexe}"
Name: "{autodesktop}\BOOM STUDIO"; Filename: "{app}\BoomStudio.exe"; Tasks: desktopicon

[Run]
; Cai VC++ Runtime truoc (im lang, khong bat khoi dong lai). /norestart de khong
; ngat luong cai. Neu may da co san thi no tu bo qua rat nhanh.
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/quiet /norestart"; StatusMsg: "Dang cai thu vien he thong (VC++ Runtime)..."; Flags: waituntilterminated
Filename: "{app}\BoomStudio.exe"; Description: "Mo BOOM STUDIO ngay"; Flags: nowait postinstall skipifsilent
