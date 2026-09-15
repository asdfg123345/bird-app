; ============================================================
; iNaturalist 鸟类爬虫 标准安装包封装脚本
; 工具：Inno Setup 7.1.0
; 作者签名：天咏（明月照今日，风摇过两川。）
; ============================================================

#define MyAppName "iNaturalist鸟类爬虫"
#define MyAppVersion "1.2"
#define MyAppPublisher "天咏"
#define MyAppExeName "iNaturalist鸟类爬虫.exe"

[Setup]
; 固定应用标识：同一软件的所有版本必须保持不变
AppId={{F48372BD-EC83-4434-B628-5B713BD36932}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppComments=iNaturalist 鸟类数据采集与图像下载系统（免安装运行时，安装即用）

; 默认装到用户目录：程序运行需写入数据（数据库、照片），用户目录始终可写，无需管理员权限
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

; 仅 64 位 Windows（程序为 64 位）
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest

; 安装包外观与压缩
WizardStyle=modern
WizardSizePercent=110
Compression=lzma2
SolidCompression=yes
OutputDir=d:\鸟类\iNaturalist\发布_安装包_20260915_001
OutputBaseFilename=iNaturalist鸟类爬虫_安装包_v1.2
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
; 桌面快捷方式默认勾选
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: checkedonce

[Files]
; 整个发布单元原样装入（exe、运行库、两个 bat、使用说明、初始空数据库）
Source: "d:\鸟类\iNaturalist\发布_EXE版_20260914_001\发布程序\iNaturalist鸟类爬虫\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 开始菜单：主程序 + 拖入爬取 + 使用说明 + 卸载
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\拖入文件自动爬取"; Filename: "{app}\拖入文件自动爬取.bat"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"
Name: "{group}\卸载"; Filename: "{uninstallexe}"
; 桌面快捷方式（可选任务）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 安装完成后可选立即运行（静默安装时跳过）
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// 卸载一律保留用户数据（照片、数据库），防止静默卸载自动应答误删。
// 卸载完成后的提示仅在交互模式显示，静默模式下自动抑制且无副作用。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if DirExists(ExpandConstant('{app}\data')) then
      MsgBox('卸载完成。' + #13 + #10 + #13 + #10 +
        '您的数据（照片、数据库）已保留在：' + #13 + #10 +
        ExpandConstant('{app}\data') + #13 + #10 + #13 + #10 +
        '如确认不再需要，可手动删除该目录。',
        mbInformation, MB_OK);
  end;
end;
