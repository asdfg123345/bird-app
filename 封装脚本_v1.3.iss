; ============================================================
; 鸟类（原 iNaturalist 鸟类爬虫）v1.3 标准安装包封装脚本
; 工具：Inno Setup 7.1
; 作者签名：天咏（明月照今日，风摇过两川。）
; 说明：AppId 与历史版本保持一致，v1.0~v1.2 可直接原地升级
; ============================================================

#define MyAppName "鸟类"
#define MyAppVersion "1.3"
#define MyAppPublisher "天咏"
#define MyAppExeName "鸟类.exe"

[Setup]
; 固定应用标识：同一软件的所有版本必须保持不变（升级兼容）
AppId={{F48372BD-EC83-4434-B628-5B713BD36932}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppComments=鸟类：iNaturalist 鸟类数据采集、模型训练与视觉分析系统（免安装运行时，安装即用）

; 默认装到用户目录：程序运行需写入数据（数据库、照片、模型库），用户目录始终可写，无需管理员权限
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
OutputDir=d:\鸟类\iNaturalist\发布_鸟类应用_20261011_001
OutputBaseFilename=鸟类_安装包_v1.3
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} v{#MyAppVersion}

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
; 桌面快捷方式默认勾选
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: checkedonce

[Files]
; 整个发布单元原样装入（exe、运行库、训练脚本、模型库模块、两个 bat、使用说明）
; 预置数据库单独装入且标记卸载不删除：用户运行期间持续写入数据，卸载必须保留
Source: "d:\鸟类\iNaturalist\发布_鸟类应用_20261011_001\发布程序\鸟类\*"; DestDir: "{app}"; Excludes: "data\db\raw_data.db,data\db\bird_observations.db"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "d:\鸟类\iNaturalist\发布_鸟类应用_20261011_001\发布程序\鸟类\data\db\raw_data.db"; DestDir: "{app}\data\db"; Flags: ignoreversion uninsneveruninstall
Source: "d:\鸟类\iNaturalist\发布_鸟类应用_20261011_001\发布程序\鸟类\data\db\bird_observations.db"; DestDir: "{app}\data\db"; Flags: ignoreversion uninsneveruninstall

[UninstallDelete]
; 卸载时清理运行库目录壳（Inno 只删有记录的文件，纯空目录会残留；_internal 为纯程序文件，删除不影响用户数据）
Type: filesandordirs; Name: "{app}\_internal"

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
// 卸载一律保留用户数据（照片、数据库、模型库），防止静默卸载自动应答误删。
// 卸载完成后的提示仅在交互模式显示，静默模式下自动抑制且无副作用。
// 注意：提示框内换行必须使用 + #13 + #10 拼接，不能在函数结果后直接接 #13。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if DirExists(ExpandConstant('{app}\data')) then
      MsgBox('卸载完成。' + #13 + #10 + #13 + #10 +
        '您的数据（照片、数据库、模型库）已保留在：' + #13 + #10 +
        ExpandConstant('{app}\data') + #13 + #10 + #13 + #10 +
        '如确认不再需要，可手动删除该目录。',
        mbInformation, MB_OK);
  end;
end;
