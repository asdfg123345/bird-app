; ============================================================
; 项目：鸟类（iNaturalist 鸟类数据采集与图像下载系统）安装包脚本
; 打包对象：发布_鸟类应用_20261015_002\打包源码（含自带数据）
; 作者签名：天咏（明月照今日，风摇过两川。）
; ============================================================

[Setup]
; 应用基本信息（版本与 main.py 中 APP_VERSION 保持一致）
AppName=鸟类
AppVersion=1.5.1
AppVerName=鸟类 v1.5.1（含数据版）
AppPublisher=天咏
AppCopyright=© 2026 天咏 版权所有（明月照今日，风摇过两川。）
AppComments=iNaturalist 鸟类数据采集与图像下载系统（20261015_002 含数据版）

; 安装默认目录：与程序版本共用同一 AppId，支持覆盖升级
AppId={{8F3B6C1A-52D4-4E7A-9C0B-BIRD2026APP01}
DefaultDirName={autopf}\鸟类
DefaultGroupName=鸟类
OutputDir=安装输出
OutputBaseFilename=鸟类_安装包_20261015_002_含数据
; 安装包自身的版本资源（资源管理器属性页可见版权）
VersionInfoCompany=天咏
VersionInfoCopyright=© 2026 天咏 版权所有（明月照今日，风摇过两川。）
VersionInfoDescription=鸟类安装包（含数据版，iNaturalist 鸟类数据采集与图像下载系统）
VersionInfoVersion=1.5.1
VersionInfoProductName=鸟类

; 安装向导封面：左侧星云竖幅大图 + 右上角星系核心小图
WizardImageFile=安装封面_星云竖幅_20261017_001.png
WizardSmallImageFile=安装封面_星云小图_20261017_001.png

; 数据目录以照片为主（已压缩格式），采用快速压缩避免打包耗时过长
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
; 磁盘跨越：单文件安装包
DiskSpanning=no

; 界面语言：简体中文
[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Messages]
chinesesimplified.WelcomeLabel2=这将安装 [name/ver] 到您的计算机。%n%n本安装包含自带数据目录（照片与数据库），安装后即可直接使用。%n%n安装前请确保已安装 Python 3.10 及以上（并加入 PATH）。%n%n建议关闭其他应用程序后单击“下一步”继续。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
; 全部程序文件与数据目录（排除缓存/exe形态旧附件/开发机本地配置，递归打包）
Source: "打包源码\*"; DestDir: "{app}"; Excludes: "__pycache__,*.pyc,vision_system\__pycache__,vision_system\gallery_index_cache,发布附件,本地环境路径.local.json"; Flags: ignoreversion recursesubdirs createallsubdirs
; 源码形态启动脚本/使用说明/星云图标已位于打包源码根目录，随递归打包进入安装根目录

[Icons]
; 桌面与开始菜单快捷方式：通过启动脚本拉起桌面控制台
; （脚本内部优先 pythonw 无黑窗运行，未找到时回退 python）
Name: "{autodesktop}\鸟类"; Filename: "{app}\启动爬虫界面.bat"; WorkingDir: "{app}"; IconFilename: "{app}\星云图标.ico"; Tasks: desktopicon
Name: "{group}\鸟类"; Filename: "{app}\启动爬虫界面.bat"; WorkingDir: "{app}"; IconFilename: "{app}\星云图标.ico"
Name: "{group}\卸载 鸟类"; Filename: "{uninstallexe}"

[Run]
; 安装完成后可选择直接启动桌面控制台
Filename: "{app}\启动爬虫界面.bat"; WorkingDir: "{app}"; Description: "立即启动 鸟类 桌面控制台"; Flags: nowait postinstall skipifsilent unchecked
