#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：鸟类（iNaturalist 鸟类数据采集与图像下载系统）
# 文件：packaging/build_installer.py —— v1.7.1 安装包构建流水线
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

"""鸟类 v1.7.1 安装包构建流水线（PyInstaller onedir + Inno Setup）。

设计原则（与 v1.5.1 已验证的发布链路一致）：
    1. 自包含：主程序冻结为 鸟类.exe（内含 Python 3.11 运行时、tkinter、
       requests、Pillow），目标机无需安装 Python；
    2. 冻结适配：把各模块「基于 __file__ 的路径常量」改写为
       _frozen_base(__file__)，保证 exe 形态下数据目录 = exe 所在目录；
    3. 数字水印：源码指纹（14 个运行模块）→ 水印标识 TYWM-BIRD-v1.7.1-….，
       同时嵌入 exe 常量、exe PE 版本资源、安装包 PE 版本资源、使用说明；
    4. 训练/视觉大依赖（torch）不进主程序：model_trainer.py、
       part_model_training\、vision_system\ 以源码形态部署在 exe 旁，
       由用户本机含 torch 的解释器运行（与历史版本行为一致）；
    5. 安装位置 %LOCALAPPDATA%\\Programs\\鸟类：程序运行需要写 data\，
       用户目录始终可写，安装与运行都不需要管理员权限。

用法（在仓库根目录执行）：
    python packaging/build_installer.py              # 完整：准备→冻结→部署→编译→冒烟
    python packaging/build_installer.py --skip-build # 复用已有 发布程序\
    python packaging/build_installer.py --skip-iscc  # 只产出发布程序，不编译安装包
    python packaging/build_installer.py --skip-smoke # 跳过安装前冒烟测试

    # 带代码签名（消除 SmartScreen「无法识别的应用」提示；需已装证书）：
    python packaging/build_installer.py --sign-thumbprint <证书指纹>
    python packaging/build_installer.py --sign-pfx D:\cert\bird.pfx --sign-password ****

产物：
    release/鸟类_v1.7.1/发布程序/鸟类/              发布单元（可直接运行）
    release/鸟类_v1.7.1/安装输出/鸟类_安装包_v1.7.1.exe  交付安装包
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
from datetime import datetime
from pathlib import Path

# ============================================================
# 常量（版本号必须与 src/main.py 的 APP_VERSION 一致，脚本会校验）
# ============================================================
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
RELEASE_ROOT = REPO_ROOT / "release"
APP_NAME = "鸟类"
APP_VERSION = "1.7.1"
APP_EXE = f"{APP_NAME}.exe"
APP_AUTHOR = "天咏"
APP_SIGNATURE = "明月照今日，风摇过两川。"
APP_COPYRIGHT = f"© 2026 {APP_AUTHOR} 版权所有（{APP_SIGNATURE}）"

RELEASE_DIR = RELEASE_ROOT / f"{APP_NAME}_v{APP_VERSION}"
PKG_DIR = RELEASE_DIR / "打包源码"
ATTACH_DIR = PKG_DIR / "发布附件"
DIST_ROOT = RELEASE_DIR / "发布程序"
DIST_DIR = DIST_ROOT / APP_NAME
OUT_DIR = RELEASE_DIR / "安装输出"
SETUP_BASENAME = f"{APP_NAME}_安装包_v{APP_VERSION}"
SETUP_EXE = OUT_DIR / f"{SETUP_BASENAME}.exe"
FINGERPRINT_TXT = RELEASE_DIR / f"构建指纹_{APP_NAME}_v{APP_VERSION}.txt"
SPEC_NAME = f"{APP_NAME}_v{APP_VERSION}.spec"
ISS_NAME = f"封装脚本_{APP_NAME}_v{APP_VERSION}.iss"
VERSION_INFO_NAME = f"版本信息_{APP_NAME}_v{APP_VERSION}.txt"

# 冻结进 exe 的运行模块（main.py 的依赖闭包，逐模块注入冻结适配）
FROZEN_MODULES = [
    "main.py",
    "batch_download_birds.py",
    "bird_crawler_gui.py",
    "bird_db_panel.py",
    "dataset_exporter.py",
    "dataset_registry.py",
    "db_health_check.py",
    "download_inaturalist.py",
    "drop_crawl_interface.py",
    "inaturalist_db_initializer.py",
    "inaturalist_db_manager.py",
    "inaturalist_db_migrator.py",
    "model_library.py",
]
# 以源码形态部署在 exe 旁、由外部 torch 解释器运行的脚本
DATAFILE_MODULES = ["model_trainer.py"]
# 参与构建指纹计算的模块全集
FINGERPRINT_MODULES = FROZEN_MODULES + DATAFILE_MODULES

# 内置视觉分析系统最小运行集（与 vision_system 子目录零相互导入，按文件名加载）
VISION_MODULES = [
    "annotator.py",
    "config.py",
    "feature_describer.py",
    "gui_app.py",
    "model_adapters.py",
    "model_backends.py",
    "part_locator.py",
    "qa_server.py",
    "question_parser.py",
    "species_classifier.py",
    "vqa_engine.py",
]

# 冻结环境适配函数（注入到每个模块 from __future__ 行之后）
FROZEN_HELPER = '''
# ============================================================
# 冻结环境适配（构建流水线自动注入，请勿手工修改）：
# 打包为 exe 后数据目录固定到 exe 所在目录，源码形态仍按脚本目录解析
# ============================================================
def _frozen_base(src_file):
    """打包后返回 exe 所在目录；源码运行返回脚本所在目录。"""
    import sys as _sys
    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(src_file).resolve().parent
'''

FROZEN_PATH_LITERAL = "Path(__file__).resolve().parent"
FROZEN_PATH_REPLACEMENT = "_frozen_base(__file__)"
FUTURE_ANCHOR = "from __future__ import annotations\n"

# 安装包脚本模板（占位符显式替换；含大量 Inno 花括号常量，勿改为 f-string）
INNO_TEMPLATE = r"""; ============================================================
; 鸟类 v@@VERSION@@ 标准安装包封装脚本（含数字水印）
; 由 packaging/build_installer.py 生成，请勿手工编辑
; 作者签名：@@AUTHOR@@（@@SIGNATURE@@）
; 说明：AppId 与历史版本（v1.0~v1.7）保持一致，可直接原地升级
;       水印位置：向导版权行 / 安装包 PE 版本资源 / 卸载备注
; ============================================================

#define MyAppName "@@NAME@@"
#define MyAppVersion "@@VERSION@@"
#define MyAppPublisher "@@AUTHOR@@"
#define MyAppExeName "@@EXE@@"

[Setup]
; 固定应用标识：同一软件的所有版本必须保持不变（升级兼容）
AppId={{F48372BD-EC83-4434-B628-5B713BD36932}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppComments=版权所有 © 2026 @@AUTHOR@@；数字水印：@@WATERMARK@@；构建指纹：@@FINGERPRINT@@
AppCopyright=@@COPYRIGHT@@
; 安装包 PE 版本资源（安装包 exe 文件属性详细信息页水印）
VersionInfoVersion=@@VERSIONINFO@@
VersionInfoCompany=@@AUTHOR@@
VersionInfoCopyright=@@COPYRIGHT@@
VersionInfoDescription=鸟类 v@@VERSION@@ 安装程序（数字水印：@@WATERMARK@@）
VersionInfoProductName=鸟类

; 默认装到用户目录：程序运行需写入数据（数据库、照片、模型库），用户目录始终可写，无需管理员权限
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

; 仅 64 位 Windows（程序为 64 位，内含 Python 运行时不支持 32 位系统）
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest

; 安装包外观、图标与压缩
WizardStyle=modern
WizardSizePercent=110
SetupIconFile=@@ICON@@
Compression=lzma2/ultra64
SolidCompression=yes
OutputDir=@@OUTDIR@@
OutputBaseFilename=@@BASENAME@@
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} v{#MyAppVersion}

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
; 桌面快捷方式默认勾选
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: checkedonce

[Files]
; 整个发布单元原样装入（exe、_internal 运行库、训练脚本、视觉系统、启动 bat）
; 预置数据库单独装入且标记卸载不删除：用户运行期间持续写入数据，卸载必须保留；
; onlyifdoesntexist：升级安装时用户已有数据库原样保留，仅全新安装写入预置空库
Source: "@@DIST@@\*"; DestDir: "{app}"; Excludes: "data\db\raw_data.db,data\db\bird_observations.db,*.pyc,__pycache__"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "@@DIST@@\data\db\raw_data.db"; DestDir: "{app}\data\db"; Flags: ignoreversion onlyifdoesntexist uninsneveruninstall
Source: "@@DIST@@\data\db\bird_observations.db"; DestDir: "{app}\data\db"; Flags: ignoreversion onlyifdoesntexist uninsneveruninstall

[UninstallDelete]
; 卸载清理无安装记录的运行期目录（均为程序文件，不含用户数据）
; _internal：PyInstaller 运行库；vision_system / part_model_training：内置源码，
; 运行期会生成 __pycache__（无安装记录，不清理会残留）；data 不在清理范围内
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\vision_system"
Type: filesandordirs; Name: "{app}\part_model_training"

[Icons]
; 开始菜单：主程序 + 拖入爬取 + 使用说明 + 卸载
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\拖入文件自动爬取"; Filename: "{app}\拖入文件自动爬取.bat"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"
Name: "{group}\卸载 鸟类"; Filename: "{uninstallexe}"
; 桌面快捷方式（可选任务）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 安装完成后可选立即运行（静默安装时跳过）
Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// 卸载一律保留用户数据（照片、数据库、模型库），防止静默卸载自动应答误删。
// 卸载完成后的提示仅在交互模式显示，静默模式下自动抑制且无副作用。
// 注意：提示框内换行必须使用 + #13 + #10 拼接，不能在函数结果后直接接 #13。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // 静默卸载（/VERYSILENT）不弹窗，避免安装包验证与批量部署被阻塞
    if (not UninstallSilent) and DirExists(ExpandConstant('{app}\data')) then
      MsgBox('卸载完成。' + #13 + #10 + #13 + #10 +
        '您的数据（照片、数据库、模型库）已保留在：' + #13 + #10 +
        ExpandConstant('{app}\data') + #13 + #10 + #13 + #10 +
        '如确认不再需要，可手动删除该目录。',
        mbInformation, MB_OK);
  end;
end;
"""


# ============================================================
# 基础工具
# ============================================================
def log(message: str) -> None:
    print(message, flush=True)


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 1800) -> subprocess.CompletedProcess:
    """执行外部命令：回显命令行，失败时抛出带输出的异常。"""
    log(f"  $ {' '.join(str(c) for c in cmd)}")
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"命令失败（退出码 {proc.returncode}）：{' '.join(str(c) for c in cmd)}\n"
            f"--- stdout ---\n{proc.stdout[-4000:]}\n--- stderr ---\n{proc.stderr[-4000:]}")
    return proc


def read_src_version() -> str:
    """读取 src/main.py 的 APP_VERSION（防止版本号漂移）。"""
    text = (SRC_DIR / "main.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION = "([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("src/main.py 中未找到 APP_VERSION 定义")
    return match.group(1)


def check_environment(build_python: str) -> None:
    """校验版本一致性与构建工具可用性。"""
    src_version = read_src_version()
    if src_version != APP_VERSION:
        raise RuntimeError(
            f"版本号不一致：脚本 APP_VERSION={APP_VERSION}，"
            f"src/main.py APP_VERSION={src_version}（请先同步升级版本号）")
    proc = subprocess.run([build_python, "-m", "PyInstaller", "--version"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"构建解释器缺少 PyInstaller：{build_python}\n{proc.stderr[-1000:]}")
    log(f"  PyInstaller 版本：{proc.stdout.strip()}")


def find_iscc() -> Path | None:
    """定位 Inno Setup 编译器 ISCC.exe。"""
    candidates = [
        os.environ.get("ISCC", ""),
        r"C:\Program Files\Inno Setup 7\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    ]
    for item in candidates:
        if item and Path(item).is_file():
            return Path(item)
    return None


# ============================================================
# 源码指纹与水印
# ============================================================
def compute_build_fingerprint() -> str:
    """计算打包模块源码构建指纹（数字水印核心，可复算取证）。"""
    parts: list[str] = []
    for name in FINGERPRINT_MODULES:
        src = SRC_DIR / name
        if not src.is_file():
            raise RuntimeError(f"指纹计算缺少源文件：{src}")
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        parts.append(f"{name}:{digest}")
    merged = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return merged[:16].upper()


# ============================================================
# 冻结适配注入（只改写代码中的路径常量，不碰字符串字面量）
# ============================================================
def _string_spans(text: str) -> list[tuple[int, int]]:
    """返回全部字符串字面量在 text 中的绝对偏移区间（tokenize 识别）。"""
    line_starts = [0]
    for line in text.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    def offset(pos: tuple[int, int]) -> int:
        line, column = pos
        return line_starts[line - 1] + column

    spans: list[tuple[int, int]] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type == tokenize.STRING:
                spans.append((offset(token.start), offset(token.end)))
    except (tokenize.TokenError, IndentationError):
        pass        # 极端语法问题交由后续 compileall 暴露
    return spans


def inject_frozen_paths(text: str) -> tuple[str, int]:
    """把代码中的 Path(__file__).resolve().parent 改写为 _frozen_base(__file__)。

    只替换不在字符串字面量内的出现：dataset_exporter.py 的 train_example
    模板里也有同一字面量，那是「生成出去的示例代码」，不能被改写。
    """
    spans = _string_spans(text)
    pieces: list[str] = []
    last = 0
    replaced = 0
    for match in re.finditer(re.escape(FROZEN_PATH_LITERAL), text):
        start = match.start()
        if any(begin <= start < end for begin, end in spans):
            continue
        pieces.append(text[last:start])
        pieces.append(FROZEN_PATH_REPLACEMENT)
        last = match.end()
        replaced += 1
    if not replaced:
        return text, 0
    pieces.append(text[last:])
    return "".join(pieces), replaced


def prepare_frozen_sources(fingerprint: str, build_time: str) -> None:
    """步骤 1：生成 打包源码（复制 + 冻结适配 + 水印注入）。"""
    if PKG_DIR.exists():
        shutil.rmtree(PKG_DIR)
    PKG_DIR.mkdir(parents=True)
    ATTACH_DIR.mkdir(parents=True)

    for name in FROZEN_MODULES:
        src = SRC_DIR / name
        text = src.read_text(encoding="utf-8")
        if FUTURE_ANCHOR not in text:
            raise RuntimeError(f"{name} 缺少 from __future__ 导入锚点，无法注入冻结适配")
        if FROZEN_PATH_LITERAL not in text:
            raise RuntimeError(f"{name} 未使用异常路径常量，请检查冻结适配清单")
        text, replaced = inject_frozen_paths(text)
        text = text.replace(FUTURE_ANCHOR, FUTURE_ANCHOR + FROZEN_HELPER, 1)
        watermark_injected = "@@BUILD_FINGERPRINT@@" in text
        text = text.replace("@@BUILD_FINGERPRINT@@", fingerprint)
        text = text.replace("@@BUILD_TIME@@", build_time)
        (PKG_DIR / name).write_text(text, encoding="utf-8", newline="\n")
        extra = "，水印已注入" if watermark_injected else ""
        log(f"  [冻结] {name}：改写 {replaced} 处路径常量{extra}")

    for name in DATAFILE_MODULES:
        shutil.copy2(SRC_DIR / name, PKG_DIR / name)
        log(f"  [数据] {name}：原样复制（exe 旁源码形态，外部解释器运行）")

    # 内置视觉分析系统（11 个模块，零相互导入，按文件名加载）
    vision_out = PKG_DIR / "vision_system"
    vision_out.mkdir()
    missing = []
    for name in VISION_MODULES:
        src = SRC_DIR / "vision_system" / name
        if src.is_file():
            shutil.copy2(src, vision_out / name)
        else:
            missing.append(name)
    if missing:
        raise RuntimeError(f"视觉系统缺少模块：{', '.join(missing)}")
    log(f"  [视觉] vision_system：{len(VISION_MODULES)} 个模块")

    # 部位模型训练源码（由外部 torch 解释器以脚本方式运行）
    part_src = SRC_DIR / "part_model_training"
    part_out = PKG_DIR / "part_model_training"
    part_out.mkdir()
    part_files = sorted(p for p in part_src.glob("*.py") if p.is_file())
    if not part_files:
        raise RuntimeError("未找到 part_model_training 源码")
    for src in part_files:
        shutil.copy2(src, part_out / src.name)
    log(f"  [部位] part_model_training：{len(part_files)} 个模块")

    # 程序图标（exe / 快捷方式 / 安装程序共用）
    icon = SRC_DIR / "星云图标.ico"
    if not icon.is_file():
        raise RuntimeError(f"缺少图标文件：{icon}")
    shutil.copy2(icon, PKG_DIR / icon.name)


# ============================================================
# 发布附件（启动 bat / 使用说明）
# ============================================================
def write_bat(name: str, lines: list[str]) -> None:
    """生成 GBK + CRLF 编码的 bat（cmd 中文路径兼容要求）。"""
    content = "\r\n".join(lines) + "\r\n"
    (ATTACH_DIR / name).write_bytes(content.encode("gbk"))


def write_attachments(watermark_id: str) -> None:
    """步骤 2：生成启动脚本与使用说明。"""
    write_bat("启动爬虫界面.bat", [
        "@echo off",
        f"rem {APP_NAME} v{APP_VERSION} 桌面控制台启动脚本（双击即弹出操作界面）",
        f"rem 版权所有：2026 {APP_AUTHOR}（{APP_SIGNATURE}）",
        "rem 说明：本脚本与本程序输出均采用系统默认中文编码（GBK），请勿添加 chcp 切换，否则中文会乱码",
        'cd /d "%~dp0"',
        f'start "" "%~dp0{APP_EXE}"',
    ])
    write_bat("拖入文件自动爬取.bat", [
        "@echo off",
        "rem 拖入自动处理接口：将文件或文件夹拖到本脚本图标上即可",
        "rem 支持：txt/csv 清单（每行一个类群ID或物种名）→ 自动爬取",
        "rem       图片文件/文件夹（文件夹名=物种中文名/类群ID）→ 按种类指纹编码入库",
        "rem 可一次拖入多个项目；源文件保持原位，报告存于 data\\drop_inbox\\processed_已处理",
        f"rem 版权所有：2026 {APP_AUTHOR}（{APP_SIGNATURE}）",
        "rem 说明：本脚本与本程序输出均采用系统默认中文编码（GBK），请勿添加 chcp 切换，否则中文会乱码",
        'cd /d "%~dp0"',
        "",
        'if "%~1"=="" (',
        "    echo 未拖入文件：将以常驻监视模式运行，监视 data\\drop_inbox 目录。",
        "    echo 把 txt/csv 清单、图片或图片文件夹放入该目录即可自动处理，按 Ctrl+C 退出。",
        f'    "%~dp0{APP_EXE}" drop --watch',
        "    goto end",
        ")",
        "",
        "rem 收集全部拖入项（支持一次拖入多个文件/文件夹），合并为一次调用",
        'set "ARGS="',
        ":collect",
        'if "%~1"=="" goto run',
        'set "ARGS=%ARGS% "%~1""',
        "shift",
        "goto collect",
        "",
        ":run",
        f'"%~dp0{APP_EXE}" drop --file %ARGS%',
        "",
        ":end",
        "pause",
    ])

    readme = f"""{APP_NAME}（iNaturalist 鸟类数据采集、模型训练与视觉分析系统）v{APP_VERSION}
============================================================
作者签名：{APP_AUTHOR}（{APP_SIGNATURE}）
版权声明：{APP_COPYRIGHT}

【版权与数字水印】
  本程序及本安装包内嵌数字水印与源码构建指纹，用于标识著作权归属。
  查看方式：在程序所在文件夹地址栏输入 cmd 回车后执行
      {APP_EXE} copyright
  即可显示版权与数字水印信息（水印标识、构建指纹、构建时间）。
  数字水印标识：{watermark_id}
  请勿去除或篡改程序内嵌的数字水印与版权信息。

【环境要求】
  Windows 10/11（64 位），无需安装 Python 及任何第三方依赖
  （主程序已自带 Python 3.11 运行时、tkinter、requests、Pillow）。
  模型训练与视觉分析需要本机另有一个含 torch 的 Python 解释器，
  在「模型训练」页指定即可（不影响下载、导入、数据库等主功能）。

【目录说明】
  {APP_EXE}                主程序（双击直接打开桌面控制台）
  启动爬虫界面.bat           双击打开桌面控制台（与双击 exe 等价）
  拖入文件自动爬取.bat       拖入文件/文件夹自动处理接口
  model_trainer.py          模型训练脚本（由桌面控制台以外部 Python 调用）
  model_library.py          共享模型库模块（训练脚本与主程序共用）
  part_model_training\\       部位模型训练脚本（分割/检测，外部 Python 调用）
  vision_system\\            内置鸟类视觉分析系统（视觉分析页签引擎源码）
  data\\                     运行后自动生成的数据目录
                            （数据库、照片、投放收件箱、模型库、训练数据集）

【运行方式】
  1. 双击「鸟类」快捷方式、{APP_EXE} 或「启动爬虫界面.bat」，
     都会直接打开桌面控制台（推荐）。
     控制台包含：批量下载、拖入文件爬取、数据面板、训练数据集、
     模型训练、模型库、视觉分析、部位训练等页签。
  2. 命令行用法（在 exe 所在文件夹的地址栏输入 cmd 回车后执行）：
     {APP_EXE} --help           查看全部命令
     {APP_EXE} copyright        查看版权与数字水印信息
     {APP_EXE} gui              桌面控制台
     {APP_EXE} batch            批量下载 15 个鸟类科
     {APP_EXE} panel            浏览器数据面板
     {APP_EXE} stats            数据库统计
     {APP_EXE} init-db          初始化数据库
     {APP_EXE} health           数据库健康检查（--apply 执行安全修复）
     {APP_EXE} drop --watch     常驻监视投放目录
     {APP_EXE} dataset --out 导出目录   导出训练数据集

【拖入文件自动爬取】
  1. 文本清单：把含「类群ID 或 物种名」的 txt/csv 拖到
     「拖入文件自动爬取.bat」图标上，自动解析并爬取。
     清单示例（每行一项）：20967 / Upupa epops
  2. 本地图片入库：把图片文件夹（或若干图片文件）拖到同一 bat 上，
     按文件夹名/子文件夹名解析物种（如「戴胜」或「20967」），
     图片按内容 SHA-256 指纹去重入库，重复图片自动跳过，源文件保留原位。
  3. 层级文件夹（纲-目-科-种）整棵树拖入时自动下钻到含图片的最深层，
     并按文件夹名解析物种，层级路径一并记录。

【数据存储】
  照片：data/taxon_<ID>_<名称>/photos/
  数据库：data/db/（raw_data.db 原始留存 + bird_observations.db 规范主库，
          双层结构，SHA-256 自动去重，启用 WAL 与事务化写入）
  模型库：data/model_library/（训练完成模型自动注册）
  训练数据集：data/train_datasets/registry.json

【模型训练】（需要外部含 torch 的解释器）
  训练页签四种模式：基础训练 / 继续训练 / 外部导入训练 / 外部模型微调；
  训练完成自动进模型库；支持断点续训与按架构差异化数据增强。

【视觉分析】（需要外部含 torch 的解释器）
  视觉问答面板：物种识别与中文问答（如「这种鸟的翅膀是什么颜色？」），
  回答含标记图；问答引擎以子进程方式在训练解释器中运行。
  程序会自动探测本机常见位置中装有 torch 的解释器；确实没有时，
  视觉面板降级为页内中文指引，其余页签不受影响。

【卸载说明】
  卸载只移除程序文件，您的数据（照片、数据库、模型库）完整保留在
  安装目录的 data 文件夹中，可自行决定是否删除。

【版本历史】
  v1.7.1  发布包基于最新主线代码重建：界面 P0 布局缺陷与 DPI 适配修复、
          数据库事务化与 WAL、照片连接复用、图片索引移出主线程、
          分支变量引用与划分变量覆盖修复、DNS 副作用与静默降级留痕治理、
          本地导入检查点提交与完整性错误分流、新增 tools 回归套件。
  v1.7.0  新增部位模型训练（分割/检测）、模型加载优化与进度条。
  v1.5.1  修复缺少 torch 解释器时视觉分析页整体加载失败的问题。
  v1.5    新增训练数据集页签、数字水印与源码构建指纹。
  v1.4    修复打包体积回归（torch 依赖泄漏排除）。
  v1.3    新增模型训练、模型库管理、视觉分析页签，应用更名「鸟类」。
  v1.0    首个安装包版本：批量下载、拖入爬取、桌面控制台、数据面板、双层数据库。
"""
    (ATTACH_DIR / "使用说明.txt").write_text(readme, encoding="utf-8", newline="\n")
    log("  [附件] 启动爬虫界面.bat / 拖入文件自动爬取.bat / 使用说明.txt")


# ============================================================
# PE 版本资源 / PyInstaller spec / Inno Setup iss
# ============================================================
def write_version_info(watermark_id: str, fingerprint: str, build_time: str) -> Path:
    """步骤 3：生成 PyInstaller PE 版本资源（exe 属性页水印）。"""
    parts = APP_VERSION.split(".")
    filevers = ", ".join(parts + ["0"] * (4 - len(parts)))
    content = f"""# -*- coding: utf-8 -*-
# 鸟类 v{APP_VERSION} PE 版本资源（数字水印层：exe 文件属性详细信息页）
# 由 packaging/build_installer.py 生成，请勿手工编辑
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({filevers}),
    prodvers=({filevers}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', '{APP_AUTHOR}'),
        StringStruct('FileDescription', '鸟类——iNaturalist 鸟类数据采集、模型训练与视觉分析系统'),
        StringStruct('FileVersion', '{APP_VERSION}.0'),
        StringStruct('InternalName', '鸟类'),
        StringStruct('LegalCopyright', '{APP_COPYRIGHT}'),
        StringStruct('OriginalFilename', '{APP_EXE}'),
        StringStruct('ProductName', '鸟类'),
        StringStruct('ProductVersion', '{APP_VERSION}'),
        StringStruct('Comments', '数字水印：{watermark_id}；构建指纹：{fingerprint}；构建时间：{build_time}；作者：{APP_AUTHOR}（{APP_SIGNATURE}）')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    out = RELEASE_DIR / VERSION_INFO_NAME
    out.write_text(content, encoding="utf-8", newline="\n")
    log(f"  [版本] PE 版本资源：{out.name}")
    return out


def write_spec() -> Path:
    """步骤 4：生成 PyInstaller spec（onedir + console，torch 全链路排除）。"""
    content = f"""# -*- mode: python ; coding: utf-8 -*-
# 鸟类 v{APP_VERSION} PyInstaller 打包配置（由 packaging/build_installer.py 生成）
# 说明：onedir + console（中文日志与子命令交互依赖控制台）；
#       model_trainer.py / part_model_training / vision_system 不进 Analysis
#       （torch 不打入主程序），由构建流水线以源码形态部署到 exe 旁；
#       excludes 显式排除 torch 系依赖，避免 PyInstaller 跟踪函数级 try 保护
#       的导入把 2GB+ torch 全套 DLL 打进安装包（v1.4 体积回归修复项）。

import os

a = Analysis(
    [os.path.join(SPECPATH, '打包源码', 'main.py')],
    pathex=[os.path.join(SPECPATH, '打包源码')],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'torchaudio', 'transformers', 'timm',
              'accelerate', 'model_trainer',
              # hub_resolver.py 在函数内 import modelscope（模型底座下载）：
              # modelscope → tqdm → pandas → scipy → sphinx/matplotlib/PIL 等，
              # 依赖图会膨胀到数 GB 且构建耗时 15 分钟以上（2026-11-18 实测）。
              # 冻结主程序只用 hub_resolver 的 DEFAULT_MIRROR 常量，
              # modelscope 缺失时源码已有 import 失败回退（HF 镜像兜底）；
              # 真正需要 modelscope 的是 exe 旁由外部 torch 解释器运行的
              # part_model_training 脚本，不受本排除影响。
              'modelscope', 'numpy', 'pandas', 'scipy', 'matplotlib',
              'sphinx', 'docutils', 'pygments', 'IPython', 'tqdm',
              'psutil', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='{APP_NAME}',
    # PE 版本资源水印：文件属性详细信息页展示公司、版权与水印标识
    version=os.path.join(SPECPATH, '{VERSION_INFO_NAME}'),
    icon=os.path.join(SPECPATH, '打包源码', '星云图标.ico'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='{APP_NAME}',
)
"""
    out = RELEASE_DIR / SPEC_NAME
    out.write_text(content, encoding="utf-8", newline="\n")
    log(f"  [spec] {out.name}")
    return out


def write_inno_script(watermark_id: str, fingerprint: str) -> Path:
    """步骤 5：生成 Inno Setup 封装脚本（安装包三层水印）。"""
    parts = APP_VERSION.split(".")
    version_info = ".".join(parts + ["0"] * (4 - len(parts)))
    replacements = {
        "@@NAME@@": APP_NAME,
        "@@VERSION@@": APP_VERSION,
        "@@VERSIONINFO@@": version_info,
        "@@AUTHOR@@": APP_AUTHOR,
        "@@SIGNATURE@@": APP_SIGNATURE,
        "@@COPYRIGHT@@": APP_COPYRIGHT,
        "@@EXE@@": APP_EXE,
        "@@WATERMARK@@": watermark_id,
        "@@FINGERPRINT@@": fingerprint,
        "@@OUTDIR@@": str(OUT_DIR),
        "@@BASENAME@@": SETUP_BASENAME,
        "@@DIST@@": str(DIST_DIR),
        "@@ICON@@": str(PKG_DIR / "星云图标.ico"),
    }
    content = INNO_TEMPLATE
    for key, value in replacements.items():
        content = content.replace(key, value)
    out = RELEASE_DIR / ISS_NAME
    out.write_text(content, encoding="utf-8-sig", newline="\r\n")
    log(f"  [iss] {out.name}")
    return out


# ============================================================
# 构建与部署
# ============================================================
def run_pyinstaller(build_python: str, spec: Path) -> None:
    """步骤 6：PyInstaller 冻结构建（onedir）。"""
    run([build_python, "-m", "PyInstaller",
         "--noconfirm", "--clean",
         "--distpath", str(DIST_ROOT),
         "--workpath", str(RELEASE_DIR / "build_tmp"),
         str(spec)],
        cwd=RELEASE_DIR)


def deploy_artifacts() -> None:
    """步骤 7：把源码形态脚本与内置目录部署到 exe 旁。"""
    if not (DIST_DIR / APP_EXE).is_file():
        raise RuntimeError(f"PyInstaller 产物缺失：{DIST_DIR / APP_EXE}")

    shutil.copy2(PKG_DIR / "model_trainer.py", DIST_DIR)
    shutil.copy2(PKG_DIR / "model_library.py", DIST_DIR)
    for directory in ("vision_system", "part_model_training"):
        target = DIST_DIR / directory
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(PKG_DIR / directory, target)
    for item in ATTACH_DIR.iterdir():
        shutil.copy2(item, DIST_DIR / item.name)
    shutil.copy2(PKG_DIR / "星云图标.ico", DIST_DIR)
    # 运行缓存不属于发布内容
    for cache in DIST_DIR.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    log("  [部署] model_trainer.py / model_library.py / vision_system / "
        "part_model_training / bat / 使用说明 / 图标")


def seed_database() -> None:
    """步骤 8：生成预置空数据库（验证冻结形态下数据目录解析正确）。"""
    data_dir = DIST_DIR / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    run([str(DIST_DIR / APP_EXE), "init-db"], cwd=DIST_DIR, timeout=300)
    main_db = DIST_DIR / "data" / "db" / "bird_observations.db"
    raw_db = DIST_DIR / "data" / "db" / "raw_data.db"
    for db in (main_db, raw_db):
        if not db.is_file():
            raise RuntimeError(f"预置数据库未生成：{db}（冻结适配可能未生效）")
    log(f"  [预置] data/db 已生成（{main_db.stat().st_size} B 主库）")


def compile_installer() -> Path:
    """步骤 9：Inno Setup 编译安装包。"""
    iscc = find_iscc()
    if iscc is None:
        raise RuntimeError("未找到 Inno Setup 编译器 ISCC.exe（可用环境变量 ISCC 指定）")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run([str(iscc), f"/O{OUT_DIR}", str(RELEASE_DIR / ISS_NAME)], cwd=RELEASE_DIR)
    if not SETUP_EXE.is_file():
        raise RuntimeError(f"安装包未生成：{SETUP_EXE}")
    return SETUP_EXE


def sign_target(path: Path, thumbprint: str, pfx: str, password: str,
                timestamp: str) -> None:
    """调用 packaging/sign_release.py 的签名实现（signtool 封装）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sign_release import find_signtool, sign_file, verify_file

    signtool = find_signtool()
    if signtool is None:
        raise RuntimeError("未找到 signtool.exe：请安装 Windows SDK（含 Signing Tools）")
    sign_file(path, signtool, thumbprint or None, pfx or None,
              password or None, timestamp)
    verify_file(path, signtool)


def write_checksums(setup: Path) -> Path:
    """生成发布产物 SHA-256 清单（供下载页校验）。"""
    out = OUT_DIR / "SHA256SUMS.txt"
    lines = []
    for target in (DIST_DIR / APP_EXE, setup):
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        lines.append(f"{digest}  {target.name}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    log(f"  [校验] SHA-256 清单：{out}")
    return out


# ============================================================
# 触发外部验证脚本
# ============================================================
def run_verifier(mode: str, target: Path) -> None:
    verifier = REPO_ROOT / "packaging" / "verify_installer.py"
    if not verifier.is_file():
        log(f"  [跳过] 未找到验证脚本 {verifier.name}")
        return
    run([sys.executable, str(verifier), mode, str(target)], cwd=REPO_ROOT, timeout=1800)


# ============================================================
# 主流程
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION} 安装包构建流水线")
    parser.add_argument("--python", default=sys.executable, help="构建解释器（需装 PyInstaller）")
    parser.add_argument("--skip-build", action="store_true", help="跳过 PyInstaller，复用已有发布程序")
    parser.add_argument("--skip-iscc", action="store_true", help="只产出发布程序，不编译安装包")
    parser.add_argument("--skip-smoke", action="store_true", help="跳过安装前冒烟测试")
    parser.add_argument("--skip-verify", action="store_true", help="跳过安装包安装/卸载验证")
    parser.add_argument("--sign-thumbprint", default="",
                        help="代码签名证书指纹（提供后构建时自动签名主程序与安装包）")
    parser.add_argument("--sign-pfx", default="", help="代码签名 PFX 证书路径")
    parser.add_argument("--sign-password", default="", help="PFX 密码")
    parser.add_argument("--sign-timestamp", default="http://timestamp.digicert.com",
                        help="RFC3161 时间戳服务地址")
    args = parser.parse_args()

    started = time.time()
    log(f"== {APP_NAME} v{APP_VERSION} 安装包构建 ==")
    log(f"仓库根目录：{REPO_ROOT}")
    log(f"构建目录：{RELEASE_DIR}")

    check_environment(args.python)
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    fingerprint = compute_build_fingerprint()
    build_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    watermark_id = f"TYWM-BIRD-v{APP_VERSION}-{fingerprint}"
    log(f"  [水印] 构建指纹：{fingerprint}")
    log(f"  [水印] 水印标识：{watermark_id}")

    log("\n[1/6] 准备打包源码（冻结适配 + 数字水印）...")
    prepare_frozen_sources(fingerprint, build_time)
    write_attachments(watermark_id)
    write_version_info(watermark_id, fingerprint, build_time)
    spec = write_spec()
    write_inno_script(watermark_id, fingerprint)
    FINGERPRINT_TXT.write_text(
        f"watermark_id={watermark_id}\nfingerprint={fingerprint}\n"
        f"build_time={build_time}\nversion={APP_VERSION}\n",
        encoding="utf-8", newline="\n")
    log(f"  [存档] {FINGERPRINT_TXT.name}")

    if not args.skip_build:
        log("\n[2/6] PyInstaller 冻结构建（onedir，首次约 1~3 分钟）...")
        run_pyinstaller(args.python, spec)
    else:
        log("\n[2/6] 跳过 PyInstaller（--skip-build）")

    log("\n[3/6] 部署源码形态组件 ...")
    deploy_artifacts()

    sign_requested = bool(args.sign_thumbprint or args.sign_pfx)
    if sign_requested:
        log("\n[签名] 代码签名主程序（必须在编译安装包之前，签名才会随包分发）...")
        sign_target(DIST_DIR / APP_EXE, args.sign_thumbprint, args.sign_pfx,
                    args.sign_password, args.sign_timestamp)
    else:
        log("  [签名] 未配置代码签名证书（--sign-thumbprint / --sign-pfx），"
            "本次跳过（未签名安装包会触发 SmartScreen 提示）")

    log("\n[4/6] 生成预置数据库 ...")
    seed_database()

    if not args.skip_smoke:
        log("\n[5/6] 发布程序冒烟测试 ...")
        run_verifier("smoke", DIST_DIR)
    else:
        log("\n[5/6] 跳过冒烟测试（--skip-smoke）")

    if args.skip_iscc:
        log("\n[6/6] 跳过安装包编译（--skip-iscc）")
        log(f"\n== 发布程序就绪：{DIST_DIR}（耗时 {time.time() - started:.1f}s）==")
        return 0

    log("\n[6/6] Inno Setup 编译安装包 ...")
    setup = compile_installer()
    size_mb = setup.stat().st_size / 1024 / 1024
    log(f"  安装包：{setup}（{size_mb:.1f} MB）")

    if sign_requested:
        log("\n[签名] 代码签名安装包 ...")
        sign_target(setup, args.sign_thumbprint, args.sign_pfx,
                    args.sign_password, args.sign_timestamp)

    write_checksums(setup)

    if not args.skip_verify:
        log("\n[附加] 安装包静默安装 / 功能 / 卸载验证 ...")
        run_verifier("installer", setup)

    log(f"\n== 构建完成（水印 {watermark_id}，耗时 {time.time() - started:.1f}s）==")
    log(f"安装包：{setup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
