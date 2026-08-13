; PolymarketMarketMaker.iss — Inno Setup 安装脚本
; 用途：把 PyInstaller 打出的 dist\MarketMaker 文件夹封装成一个安装程序。
; 构建：见 build_installer.ps1，或手动运行
;       "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" PolymarketMarketMaker.iss
; 特点：按用户安装（不需要管理员/不弹 UAC），自动建桌面和开始菜单快捷方式，可正常卸载。
; 注意：用户数据（数据库、日志）由程序写到 %LOCALAPPDATA%\PolymarketMarketMaker，
;       不在安装目录内，因此卸载不会动用户数据，安装包里也绝不含任何私钥或历史。

#define MyAppName "Polymarket 做市助手"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppPublisher "Polymarket 做市助手"
#define MyAppExeName "MarketMaker.exe"

[Setup]
; AppId 唯一标识本程序，升级/卸载靠它识别，不要随意改动。
AppId={{A7E3C9D2-4B1F-4E8A-9C6D-1F2E3A4B5C6D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\PolymarketMarketMaker
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer
OutputBaseFilename=PolymarketMarketMaker_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes

[Languages]
Name: "default"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式:"; Flags: checkedonce

[Files]
Source: "dist\MarketMaker\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 交互式首装:勾选框"立即启动"
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动程序"; Flags: nowait postinstall skipifsilent
