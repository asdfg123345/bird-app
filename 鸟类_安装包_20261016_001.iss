; ============================================================
; 项目：鸟类（iNaturalist 鸟类数据采集与图像下载系统）安装包脚本
; 作者签名：天咏（明月照今日，风摇过两川。）
; 说明：打包对象为混淆加密发布版源码（含数字水印与源码构建指纹）
; ============================================================

[Setup]
; 应用基本信息（版本与 main.py 中 APP_VERSION 保持一致）
AppName=鸟类
AppVersion=1.5.1
AppVerName=鸟类 v1.5.1
AppPublisher=天咏
AppPublisherURL=https://www.inaturalist.org/
; 版权行（含 © 字符，Inno 安装器界面使用 Unicode，可正常显示）
AppCopyright=© 2026 天咏 版权所有（明月照今日，风摇过两川。）
AppComments=iNaturalist 鸟类数据采集与图像下载系统（内嵌数字水印 TYWM-BIRD-v1.5.1-f370869eed682eac）

; 安装默认目录与 GUID
DefaultDirName={autopf}\鸟类
DefaultGroupName=鸟类
; 每个版本的 AppId 保持一致以支持覆盖升级
AppId={{8F3B6C1A-52D4-4E7A-9C0B-BIRD2026APP01}
OutputDir=安装输出
OutputBaseFilename=鸟类_安装包_v1.5.1_20261016_001
; 安装包自身的版本资源（资源管理器属性页可见版权与水印）
VersionInfoCompany=天咏
VersionInfoCopyright=© 2026 天咏 版权所有 数字水印 TYWM-BIRD-v1.5.1-f370869eed682eac
VersionInfoDescription=鸟类安装包（iNaturalist 鸟类数据采集与图像下载系统）
VersionInfoVersion=1.5.1
VersionInfoProductName=鸟类

; 压缩与加密外观（LZMA2 最大压缩，配合源码混淆实现防拆包）
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 卸载时保留用户数据（照片与数据库），仅移除程序文件
UninstallFilesDir={app}

; 界面语言：简体中文
[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Messages]
chinesesimplified.WelcomeLabel2=这将安装 [name/ver] 到您的计算机。%n%n程序源码经混淆加密处理并内嵌数字水印（TYWM-BIRD-v1.5.1-f370869eed682eac），未经授权不得反向工程、去除水印或二次分发。%n%n安装前请确保已安装 Python 3.10 及以上（并加入 PATH）。%n%n建议关闭其他应用程序后单击“下一步”继续。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
; 混淆加密后的全部程序文件（不含数据目录，安装后首次运行自动建库）
Source: "程序\main.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\download_inaturalist.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\batch_download_birds.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\inaturalist_db_initializer.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\inaturalist_db_manager.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\inaturalist_db_migrator.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\bird_db_panel.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\bird_crawler_gui.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\drop_crawl_interface.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\db_health_check.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\dataset_exporter.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\dataset_registry.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\model_library.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\model_trainer.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\training_dataset_importer.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\cleanup_misimported_observations.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\启动爬虫界面.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\拖入文件自动爬取.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\鸟类.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "程序\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion; DestName: "使用说明.txt"

[Icons]
; 桌面与开始菜单快捷方式：通过启动脚本拉起桌面控制台
; （脚本内部优先 pythonw 无黑窗运行，未找到时回退 python）
Name: "{autodesktop}\鸟类"; Filename: "{app}\启动爬虫界面.bat"; WorkingDir: "{app}"; IconFilename: "{app}\鸟类.ico"; Tasks: desktopicon
Name: "{group}\鸟类"; Filename: "{app}\启动爬虫界面.bat"; WorkingDir: "{app}"; IconFilename: "{app}\鸟类.ico"
Name: "{group}\卸载 鸟类"; Filename: "{uninstallexe}"

[Run]
; 安装完成后可选择直接启动桌面控制台
Filename: "{app}\启动爬虫界面.bat"; WorkingDir: "{app}"; Description: "立即启动 鸟类 桌面控制台"; Flags: nowait postinstall skipifsilent unchecked
