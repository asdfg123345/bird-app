#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：鸟类 v1.5.1 打包准备脚本（含数字水印注入）
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

"""v1.5.1 打包准备脚本：生成「打包源码」目录与全部构建配套文件。

功能（可重复执行，每次全量重建）：
    1. 计算打包模块的源码构建指纹（13 个模块逐个 SHA-256 后合并再
       哈希，取前 16 位十六进制），作为数字水印核心；
    2. 复制 12 个运行模块到 打包源码/，注入「冻结环境适配函数」
       _frozen_base（数据目录固定到 exe 所在目录），并将 main.py 中
       的水印占位符替换为真实构建指纹与构建时间；
    3. 复制 model_trainer.py 作为数据文件源（exe 旁源码形态运行）；
    4. 复制视觉分析系统 11 个最小运行模块到 打包源码/vision_system/；
    5. 生成发布附件：两个 bat（GBK+CRLF）、使用说明.txt（UTF-8 LF，
       含版权与数字水印节）；
    6. 生成 版本信息_v1.5.1.txt（PyInstaller PE 版本资源：exe 文件
       属性中的公司、版权、水印标识）；
    7. 生成 封装脚本_v1.5.1.iss（Inno Setup 安装包脚本：向导版权行、
       安装包 PE 版本资源、备注水印）。

数字水印说明：
    水印标识格式：TYWM-BIRD-v1.5.1-<16位源码指纹>
    同一指纹同时嵌入：exe 内代码常量、exe PE 版本资源、
    安装包 PE 版本资源、控制面板卸载备注、使用说明、构建日志。

用法：python 准备打包源码_v1.5.1.py
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 本脚本所在目录（即 v1.5.1 发布目录）
RELEASE_DIR = Path(__file__).resolve().parent
# 项目源码目录（相对发布目录推导，不硬编码绝对路径）
SRC_DIR = RELEASE_DIR.parent / "iNaturalist"
# 打包源码输出目录
PKG_DIR = RELEASE_DIR / "打包源码"
ATTACH_DIR = PKG_DIR / "发布附件"

# 需要冻结适配并打进 EXE 的模块（main.py 的依赖闭包；
# v1.5 新增 dataset_registry：训练数据集页签的注册表模块）
FROZEN_MODULES = [
    "main.py",
    "batch_download_birds.py",
    "bird_crawler_gui.py",
    "bird_db_panel.py",
    "dataset_exporter.py",
    "dataset_registry.py",
    "download_inaturalist.py",
    "drop_crawl_interface.py",
    "inaturalist_db_initializer.py",
    "inaturalist_db_manager.py",
    "inaturalist_db_migrator.py",
    "model_library.py",
]

# 作为数据文件放在 exe 旁边的脚本（GUI 训练子进程以外部解释器运行）
DATAFILE_MODULES = ["model_trainer.py"]

# 参与构建指纹计算的模块全集（12 个冻结模块 + model_trainer.py）
FINGERPRINT_MODULES = FROZEN_MODULES + DATAFILE_MODULES

# 鸟类视觉分析系统源码目录（相对发布目录推导：上两级即项目根 d:\鸟类）
VISION_SRC = RELEASE_DIR.parent.parent / "鸟类视觉分析系统_20261007_001"
# 视觉系统需要随安装包内置的模块（问答面板与子进程服务的最小运行集）
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
# 冻结环境适配：打包为 exe 后，数据目录固定到 exe 所在目录
# ============================================================
def _frozen_base(src_file):
    """打包后返回 exe 所在目录；源码运行返回脚本所在目录。"""
    import sys as _sys
    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(src_file).resolve().parent

'''

# 应用名与版本（与 iss / 版本信息文件保持一致）
APP_NAME = "鸟类"
APP_VERSION = "1.5.1"
APP_EXE = f"{APP_NAME}.exe"
APP_AUTHOR = "天咏"
APP_SIGNATURE = "明月照今日，风摇过两川。"
APP_COPYRIGHT = f"© 2026 {APP_AUTHOR} 版权所有（{APP_SIGNATURE}）"


def compute_build_fingerprint() -> str:
    """计算打包模块源码构建指纹（数字水印核心）。

    算法：对 13 个模块源文件逐个计算 SHA-256，把 13 个十六进制
    摘要拼接后再做一次 SHA-256，取结果前 16 位（十六进制大写）。
    同一源码状态必然得到同一指纹，可复算比对，用于权属追踪。
    """
    parts: list[str] = []
    for name in FINGERPRINT_MODULES:
        src = SRC_DIR / name
        if not src.is_file():
            raise RuntimeError(f"指纹计算缺少源文件：{src}")
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        parts.append(f"{name}:{digest}")
    merged = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return merged[:16].upper()


def prepare_frozen_sources(fingerprint: str, build_time: str) -> None:
    """步骤 1：复制模块、注入冻结适配、替换水印占位符。"""
    if PKG_DIR.exists():
        shutil.rmtree(PKG_DIR)
    PKG_DIR.mkdir(parents=True)
    ATTACH_DIR.mkdir(parents=True)

    anchor = "from __future__ import annotations\n"
    for name in FROZEN_MODULES:
        src = SRC_DIR / name
        text = src.read_text(encoding="utf-8")
        # 路径常量替换：__file__ 所在目录 → 冻结感知函数
        count = text.count("Path(__file__).resolve().parent")
        text = text.replace("Path(__file__).resolve().parent", "_frozen_base(__file__)")
        # 注入适配函数（紧跟 __future__ 导入之后）
        if anchor in text:
            text = text.replace(anchor, anchor + FROZEN_HELPER, 1)
        else:
            raise RuntimeError(f"{name} 缺少 from __future__ 导入锚点，无法注入适配函数")
        # 数字水印占位符替换（仅 main.py 含占位符，其余模块原样）
        if "@@BUILD_FINGERPRINT@@" in text:
            text = text.replace("@@BUILD_FINGERPRINT@@", fingerprint)
            text = text.replace("@@BUILD_TIME@@", build_time)
            print(f"  [水印] {name}：构建指纹与时间已注入")
        (PKG_DIR / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"  [适配] {name}：替换 {count} 处路径常量")

    # 数据文件模块：原样复制（源码形态运行，__file__ 解析天然正确）
    for name in DATAFILE_MODULES:
        shutil.copy2(SRC_DIR / name, PKG_DIR / name)
        print(f"  [数据] {name}：原样复制（exe 旁数据文件）")


def prepare_vision_system() -> None:
    """步骤 2：复制视觉分析系统源码到打包源码的 vision_system 子目录。"""
    if not VISION_SRC.is_dir():
        print(f"  [警告] 未找到视觉系统目录 {VISION_SRC}，跳过内置视觉系统")
        return
    out_dir = PKG_DIR / "vision_system"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    missing = []
    for name in VISION_MODULES:
        src = VISION_SRC / name
        if src.is_file():
            shutil.copy2(src, out_dir / name)
        else:
            missing.append(name)
    if missing:
        print(f"  [警告] 视觉系统缺少文件：{', '.join(missing)}")
    print(f"  [视觉] 内置视觉系统已复制 {len(VISION_MODULES) - len(missing)} 个模块")


def write_bat(name: str, lines: list[str]) -> None:
    """生成 GBK + CRLF 编码的 bat 脚本（cmd 对中文路径的兼容要求）。"""
    content = "\r\n".join(lines) + "\r\n"
    (ATTACH_DIR / name).write_bytes(content.encode("gbk"))


def prepare_attachments(watermark_id: str) -> None:
    """步骤 3：生成三个发布附件（bat 含版权头，说明含水印节）。"""
    # 附件 1：桌面控制台启动脚本
    write_bat("启动爬虫界面.bat", [
        "@echo off",
        f"rem {APP_NAME} v{APP_VERSION} 桌面控制台启动脚本（双击即弹出操作界面）",
        f"rem 版权所有：2026 {APP_AUTHOR}（{APP_SIGNATURE}）",
        "rem 说明：本脚本与本程序输出均采用系统默认中文编码（GBK），请勿添加 chcp 切换，否则中文会乱码",
        'cd /d "%~dp0"',
        f'start "" "%~dp0{APP_EXE}"',
    ])

    # 附件 2：拖入自动处理脚本
    write_bat("拖入文件自动爬取.bat", [
        "@echo off",
        "rem 拖入自动处理接口：将文件或文件夹摆放到本脚本图标上即可",
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

    # 附件 3：使用说明（UTF-8 无 BOM + LF 换行；含版权与数字水印节）
    readme = f"""{APP_NAME}（iNaturalist 鸟类数据采集与图像下载系统）v{APP_VERSION}
============================================
作者签名：{APP_AUTHOR}（{APP_SIGNATURE}）
版权声明：{APP_COPYRIGHT}

【版权与数字水印】
  本程序及本安装包内嵌数字水印与源码构建指纹，用于标识著作权
  归属。查看方式：在程序所在文件夹地址栏输入 cmd 回车后执行
      {APP_EXE} copyright
  即可显示版权与数字水印信息（水印标识、构建指纹、构建时间）。
  数字水印标识：{watermark_id}
  请勿去除或篡改程序内嵌的数字水印与版权信息。

【环境要求】
  Windows 10/11（64 位），无需安装 Python 及任何依赖（已全部打包进本程序）。

【目录说明】
  {APP_EXE}                主程序（双击直接打开桌面控制台）
  启动爬虫界面.bat           双击打开桌面控制台（与双击 exe 等价）
  拖入文件自动爬取.bat       拖入文件/文件夹自动处理接口
  model_trainer.py          模型训练脚本（由桌面控制台以外部 Python 调用）
  model_library.py          共享模型库模块（训练脚本与主程序共用）
  vision_system\\            内置鸟类视觉分析系统（视觉分析页签的问答引擎源码）
  data\\                     运行后自动生成的数据目录
                            （数据库、照片、投放收件箱、模型库、训练数据集）

【运行方式】
  1. 双击「启动爬虫界面.bat」或直接双击 {APP_EXE}，
     均直接打开桌面控制台（推荐）。
     控制台内含「拖入文件爬取」标签页：可直接选择 txt/csv 处理，
     或启动常驻监视 data/drop_inbox，放入文件即自动爬取。
  2. 命令行用法（在 exe 所在文件夹的地址栏输入 cmd 回车后执行）：
     {APP_EXE} --help          （查看全部命令）
     {APP_EXE} copyright       （查看版权与数字水印信息）
     {APP_EXE} batch           （批量下载 15 个鸟类科）
     {APP_EXE} gui             （桌面控制台）
     {APP_EXE} panel           （浏览器数据面板）
     {APP_EXE} stats           （数据库统计）
     {APP_EXE} init-db         （初始化数据库）
     {APP_EXE} drop --watch    （常驻监视投放目录）
     {APP_EXE} dataset --out 导出目录 （导出训练数据集）

【拖入文件自动爬取】
  1. 文本清单：将包含「类群ID 或 物种名」的 txt/csv 文件拖到
     「拖入文件自动爬取.bat」图标上，即自动解析并爬取。
     文件内容示例：
         20967
         Upupa epops
  2. 本地图片入库：将图片文件夹（或若干图片文件）拖到同一
     bat 图标上，按文件夹名/子文件夹名解析物种（如文件夹名
     为「戴胜」或「20967」），图片按种类依次指纹编码入库：
     内容 SHA-256 去重，重复图片自动跳过，源文件保留原位。
  3. 桌面控制台「拖入文件爬取」标签页同样支持以上两种方式。

【层级文件夹导入】（v1.1 新增）
  将「纲-目-科-种」整棵层级文件夹树拖到 bat 图标上，自动下钻
  到直接含图片的最深层文件夹并按文件夹名解析物种；种文件夹内
  的说明文本（.txt）自动并入该种观察备注，层级路径一并记录。

【数据存储】
  照片按种类存于 data/taxon_<ID>_<名称>/photos/
  数据库存于 data/db/（raw_data.db 原始留存 +
  bird_observations.db 规范主库，SHA-256 指纹自动去重）

【训练数据集】（v1.5 新增页签）
  桌面控制台「训练数据集」标签页：勾选库内类群（中文名/学名/
  英文名三列展示，支持搜索），一键生成已按 8:1:1 划分（train/
  val/test，按观察划分防泄漏）的标准训练数据集；
  「已生成数据集」列表双击即联动到模型训练页使用；
  生成的数据集登记于 data/train_datasets/registry.json。
  命令行等价命令：{APP_EXE} dataset --out 导出目录

【模型训练】（v1.3 新增）
  桌面控制台「模型训练」标签页：选择训练模式与模型基名即可启动
  训练，实时查看终端日志。四种训练模式：
    1. 适应模式（ad）：读取本机 GPU 显存/内存/磁盘配置，在保证
       系统安全运行的前提下自动配置训练参数（批大小经显存实测
       探测确定，安全优先、效果优先，允许以时间换效果）；
    2. 低速模式：低资源占用，训练慢但稳定；
    3. 高速模式：大批量+混合精度，快速出结果；
    4. 自定义模式：自行设置轮数、优化器、耐心值、批大小、学习率。
  训练脚本内置按网络架构差异化的数据增强、学习率预热与余弦退火
  策略，并支持断点续训（训练中断后可从最近快照恢复继续训练）。
  训练前提：本机需另装有 Python 训练环境（含 torch/torchvision/
  Pillow），在训练页「解释器」处选择其 python.exe 路径。
  训练完成的模型自动注册进模型库，命名规则：基名_月日_版本号
  （如 resnet50_10月11日_001），同日多次训练版本号自增。

【模型库】（v1.3 新增）
  桌面控制台「模型库」标签页：浏览已注册模型（含训练样例缩略图
  拼图预览）、设为激活模型、删除、导入外部模型。
  模型库位置：data/model_library/（registry.json 记录元数据）。
  激活模型被「视觉分析」与外部视觉系统共用。

【视觉分析】（v1.3 新增）
  桌面控制台「视觉分析」页签：视觉问答面板，支持物种识别与中文问答
  （如「这种鸟的翅膀是什么颜色？」），回答含红框/圆圈标记图。
  视觉系统代码已内置安装包（vision_system\\ 目录），开箱零配置；
  问答引擎在本机训练环境的 Python 解释器中以子进程运行（首次提问
  自动启动，退出控制台自动关闭），解释器与「模型训练」页共用同一份
  配置；程序会自动探测本机常见位置中装有 torch 的解释器（v1.5.1
  新增），也可在训练页手工指定。视觉模型（ViT 物种识别、OWL-ViT
  部位检测）不随安装包分发，由训练环境按其自带配置加载本地权重文件。

【卸载说明】
  卸载只移除程序文件，您的数据（照片、数据库、模型库）将完整
  保留在安装目录的 data 文件夹中，可自行决定是否删除。

【版本历史】
  v1.5.1 修复：其他设备安装后「视觉分析」页签因缺少含 torch 的训练
        解释器而整体加载失败的问题。现在程序会自动探测本机常见位置
        （conda 环境 / 系统 Python）中装有 torch 的解释器并回填到
        「模型训练」页配置；确实没有时视觉面板优雅降级为页内中文
        指引，不再报「No module named 'torch'」，其余页签不受影响。
  v1.5  新增：训练数据集页签（类群勾选生成已划分数据集）、
        训练脚本批大小实测探测与断点续训快照完善、按架构差异化
        数据增强/学习率预热/余弦退火；程序与安装包全面嵌入
        数字水印与源码构建指纹（copyright 命令可查）。
  v1.4  修复：打包体积回归（torch 依赖泄漏排除）。
  v1.3  新增：模型训练（四模式）、模型库管理、视觉分析页签，
        应用更名为「鸟类」，与鸟类视觉分析系统共用模型库。
  v1.2  新增：训练数据集导出（dataset 命令）、分类树同步。
  v1.1  新增：层级文件夹自适应导入、中文分类名合并。
  v1.0  首个安装包版本：批量下载、拖入爬取、桌面控制台、
        数据面板、双层数据库。
"""
    # UTF-8 无 BOM + LF（与历史版本编码约定一致，勿用 GBK 往返读写）
    (ATTACH_DIR / "使用说明.txt").write_text(readme, encoding="utf-8", newline="\n")
    print("  [附件] 启动爬虫界面.bat / 拖入文件自动爬取.bat / 使用说明.txt 已生成")


def write_version_info(watermark_id: str, fingerprint: str, build_time: str) -> None:
    """步骤 4：生成 PyInstaller PE 版本资源文件（exe 属性页水印）。"""
    content = f"""# -*- coding: utf-8 -*-
# ============================================================
# 鸟类 v1.5.1 PE 版本资源（数字水印层：exe 文件属性详细信息页）
# 由 准备打包源码_v1.5.1.py 生成，请勿手工编辑
# ============================================================
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 5, 1, 0),
    prodvers=(1, 5, 1, 0),
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
        StringStruct('FileVersion', '1.5.1.0'),
        StringStruct('InternalName', '鸟类'),
        StringStruct('LegalCopyright', '{APP_COPYRIGHT}'),
        StringStruct('OriginalFilename', '{APP_EXE}'),
        StringStruct('ProductName', '鸟类'),
        StringStruct('ProductVersion', '1.5.1'),
        StringStruct('Comments', '数字水印：{watermark_id}；构建指纹：{fingerprint}；构建时间：{build_time}；作者：{APP_AUTHOR}（{APP_SIGNATURE}）')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    out = RELEASE_DIR / "版本信息_v1.5.1.txt"
    out.write_text(content, encoding="utf-8", newline="\n")
    print(f"  [版本] PE 版本资源已生成：{out.name}（水印标识嵌入 Comments）")


def write_inno_script(watermark_id: str, fingerprint: str) -> None:
    """步骤 5：生成 Inno Setup 封装脚本（安装包三层水印）。

    模板使用 @@占位符@@ 显式替换（不用 f-string）：iss 含大量 Inno
    花括号常量（{{app}}、{#MyAppName}} 等），f-string 转义极易出错；
    模板必须用 raw 字符串（r'''）：iss 内的 Windows 路径含 \r \b 等
    序列，普通字符串会把它们解释成控制符破坏文件内容。
    """
    content = r"""; ============================================================
; 鸟类 v1.5.1 标准安装包封装脚本（含数字水印）
; 工具：Inno Setup 7.1
; 作者签名：@@AUTHOR@@（@@SIGNATURE@@）
; 说明：AppId 与历史版本保持一致，v1.0~v1.5 可直接原地升级
;       水印位置：向导版权行 / 安装包 PE 版本资源 / 卸载备注
; 本文件由 准备打包源码_v1.5.1.py 生成，请勿手工编辑
; ============================================================

#define MyAppName "鸟类"
#define MyAppVersion "1.5.1"
#define MyAppPublisher "@@AUTHOR@@"
#define MyAppExeName "鸟类.exe"

[Setup]
; 固定应用标识：同一软件的所有版本必须保持不变（升级兼容）
AppId={{F48372BD-EC83-4434-B628-5B713BD36932}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppComments=版权所有 © 2026 @@AUTHOR@@；数字水印：@@WATERMARK@@；构建指纹：@@FINGERPRINT@@
AppCopyright=@@COPYRIGHT@@
; 安装包 PE 版本资源（安装包 exe 文件属性详细信息页水印）
VersionInfoVersion=1.5.1.0
VersionInfoCompany=@@AUTHOR@@
VersionInfoCopyright=@@COPYRIGHT@@
VersionInfoDescription=鸟类 v1.5.1 安装程序（数字水印：@@WATERMARK@@）
VersionInfoProductName=鸟类

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
OutputDir=@@RELEASE_DIR@@
OutputBaseFilename=鸟类_安装包_v1.5.1
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} v{#MyAppVersion}

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
; 桌面快捷方式默认勾选
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: checkedonce

[Files]
; 整个发布单元原样装入（exe、运行库、训练脚本、模型库模块、两个 bat、使用说明）
; 预置数据库单独装入且标记卸载不删除：用户运行期间持续写入数据，卸载必须保留；
; onlyifdoesntexist：升级安装时用户已有数据库原样保留，仅全新安装写入预置空库
; （缺少该标志时空预置库会在升级时覆盖用户数据库，2026-10-14 升级冒烟前修复）
Source: "@@RELEASE_DIR@@\发布程序\鸟类\*"; DestDir: "{app}"; Excludes: "data\db\raw_data.db,data\db\bird_observations.db"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "@@RELEASE_DIR@@\发布程序\鸟类\data\db\raw_data.db"; DestDir: "{app}\data\db"; Flags: ignoreversion onlyifdoesntexist uninsneveruninstall
Source: "@@RELEASE_DIR@@\发布程序\鸟类\data\db\bird_observations.db"; DestDir: "{app}\data\db"; Flags: ignoreversion onlyifdoesntexist uninsneveruninstall

[UninstallDelete]
; 卸载时清理运行库目录壳（Inno 只删有记录的文件，纯空目录会残留；以下均为纯程序文件，删除不影响用户数据）
; _internal：PyInstaller 运行库；vision_system：内置视觉系统源码（其 __pycache__ 运行缓存无安装记录，
; 2026-10-14 真实卸载实测残留，故整目录纳入卸载清理）
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\vision_system"

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
"""
    # 占位符显式替换（Inno 花括号常量原样保留，避免 f-string 转义）
    replacements = {
        "@@AUTHOR@@": APP_AUTHOR,
        "@@SIGNATURE@@": APP_SIGNATURE,
        "@@COPYRIGHT@@": APP_COPYRIGHT,
        "@@WATERMARK@@": watermark_id,
        "@@FINGERPRINT@@": fingerprint,
        "@@RELEASE_DIR@@": str(RELEASE_DIR),
    }
    for key, value in replacements.items():
        content = content.replace(key, value)
    out = RELEASE_DIR / "封装脚本_v1.5.1.iss"
    out.write_text(content, encoding="utf-8-sig", newline="\r\n")
    print(f"  [封装] Inno Setup 脚本已生成：{out.name}（版权行 + PE 资源 + 备注水印）")


def main() -> int:
    print(f"== {APP_NAME} v{APP_VERSION} 打包源码准备 ==")
    print(f"源码目录: {SRC_DIR}")
    print(f"输出目录: {PKG_DIR}")
    if not (SRC_DIR / "main.py").is_file():
        print("[错误] 未找到源码目录，请确认发布目录相对位置")
        return 1

    # 数字水印核心：构建指纹与水印标识（全流程唯一来源）
    fingerprint = compute_build_fingerprint()
    build_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    watermark_id = f"TYWM-BIRD-v{APP_VERSION}-{fingerprint}"
    print(f"  [水印] 构建指纹：{fingerprint}")
    print(f"  [水印] 水印标识：{watermark_id}")

    prepare_frozen_sources(fingerprint, build_time)
    prepare_vision_system()
    prepare_attachments(watermark_id)
    write_version_info(watermark_id, fingerprint, build_time)
    write_inno_script(watermark_id, fingerprint)

    # 指纹存档：供构建脚本与系统日志引用比对
    (RELEASE_DIR / "构建指纹_v1.5.1.txt").write_text(
        f"watermark_id={watermark_id}\nfingerprint={fingerprint}\nbuild_time={build_time}\n",
        encoding="utf-8", newline="\n")
    print(f"== 打包源码准备完成（水印标识 {watermark_id}）==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
