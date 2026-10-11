#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：鸟类（原 iNaturalist 鸟类爬虫）v1.3 打包准备脚本
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

"""v1.3 打包准备脚本：生成「打包源码」目录。

功能（可重复执行，每次全量重建）：
    1. 复制当前源码的 11 个运行模块到 打包源码/ 目录；
    2. 为每个模块注入「冻结环境适配函数」：把
       Path(__file__).resolve().parent 替换为 _frozen_base(__file__)，
       打包为 exe 后数据目录自动固定到 exe 所在目录（绝不使用 _MEIPASS）；
    3. 复制 model_trainer.py / model_library.py 作为数据文件源
       （不注入适配：它们以源码形态放在 exe 旁边，__file__ 天然正确）；
    4. 生成发布附件：启动爬虫界面.bat、拖入文件自动爬取.bat（GBK+CRLF）、
       使用说明.txt（UTF-8 无 BOM + LF，含 v1.3 新功能说明）。

用法：python 准备打包源码_v1.3.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# 本脚本所在目录（即 v1.3 发布目录）
RELEASE_DIR = Path(__file__).resolve().parent
# 项目源码目录（相对发布目录推导，不硬编码绝对路径）
SRC_DIR = RELEASE_DIR.parent / "iNaturalist"
# 打包源码输出目录
PKG_DIR = RELEASE_DIR / "打包源码"
ATTACH_DIR = PKG_DIR / "发布附件"

# 需要冻结适配并打进 EXE 的模块（main.py 的依赖闭包）
FROZEN_MODULES = [
    "main.py",
    "batch_download_birds.py",
    "bird_crawler_gui.py",
    "bird_db_panel.py",
    "dataset_exporter.py",
    "download_inaturalist.py",
    "drop_crawl_interface.py",
    "inaturalist_db_initializer.py",
    "inaturalist_db_manager.py",
    "inaturalist_db_migrator.py",
    "model_library.py",
]

# 作为数据文件放在 exe 旁边的脚本（GUI 训练子进程以外部解释器运行）。
# model_library.py 不在此列：它进 EXE 的适配版（PYZ 内）与数据文件版（exe 旁）
# 共用打包源码根目录的同一份适配版文件——源码运行时 _frozen_base 回退
# __file__ 目录，放 exe 旁解析结果一致，无需两份。
DATAFILE_MODULES = ["model_trainer.py"]

# 鸟类视觉分析系统源码目录（相对发布目录推导：上两级即项目根 d:\鸟类，
# 视觉系统目录位于项目根下；注意不是 RELEASE_DIR.parent——那是 iNaturalist）
VISION_SRC = RELEASE_DIR.parent.parent / "鸟类视觉分析系统_20261007_001"
# 视觉系统需要随安装包内置的模块（问答面板与子进程服务的最小运行集；
# 训练管线/测试不随安装包分发，保持发布单元精简）
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

# 应用名与版本（与 iss 保持一致）
APP_NAME = "鸟类"
APP_EXE = f"{APP_NAME}.exe"


def prepare_frozen_sources() -> None:
    """步骤 1+2：复制模块并注入冻结适配函数。"""
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
            # 无 __future__ 行时插在文件头注释块后首个 import 之前（本组模块均有该行，兜底）
            raise RuntimeError(f"{name} 缺少 from __future__ 导入锚点，无法注入适配函数")
        (PKG_DIR / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"  [适配] {name}：替换 {count} 处路径常量")

    # 数据文件模块：原样复制（源码形态运行，__file__ 解析天然正确）
    for name in DATAFILE_MODULES:
        shutil.copy2(SRC_DIR / name, PKG_DIR / name)
        print(f"  [数据] {name}：原样复制（exe 旁数据文件）")


def prepare_vision_system() -> None:
    """步骤 3：复制视觉分析系统源码到打包源码的 vision_system 子目录。

    该目录随安装包整体释放到 exe 旁（内置视觉系统），控制台「视觉分析」
    页签的目录探测第 2 步直接命中，开箱零配置；问答引擎由训练环境
    Python 以子进程方式运行其中的 qa_server.py。
    """
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
    print(f"  [视觉] 内置视觉系统已复制 {len(VISION_MODULES) - len(missing)} 个模块"
          f"（{out_dir.name}\\ 子目录，随安装包释放）")


def write_bat(name: str, lines: list[str]) -> None:
    """生成 GBK + CRLF 编码的 bat 脚本（cmd 对中文路径的兼容要求）。"""
    content = "\r\n".join(lines) + "\r\n"
    (ATTACH_DIR / name).write_bytes(content.encode("gbk"))


def prepare_attachments() -> None:
    """步骤 4：生成三个发布附件。"""
    # 附件 1：桌面控制台启动脚本
    write_bat("启动爬虫界面.bat", [
        "@echo off",
        f"rem {APP_NAME}桌面控制台启动脚本（双击即弹出操作界面）",
        "rem 作者签名：天咏（明月照今日，风摇过两川。）",
        "rem 说明：本脚本与本程序输出均采用系统默认中文编码（GBK），请勿添加 chcp 切换，否则中文会乱码",
        'cd /d "%~dp0"',
        f'start "" "%~dp0{APP_EXE}"',
    ])

    # 附件 2：拖入自动处理脚本（无参数常驻监视 / 有参数收集全部拖入项）
    write_bat("拖入文件自动爬取.bat", [
        "@echo off",
        "rem 拖入自动处理接口：将文件或文件夹摆放到本脚本图标上即可",
        "rem 支持：txt/csv 清单（每行一个类群ID或物种名）→ 自动爬取",
        "rem       图片文件/文件夹（文件夹名=物种中文名/类群ID）→ 按种类指纹编码入库",
        "rem 可一次拖入多个项目；源文件保持原位，报告存于 data\\drop_inbox\\processed_已处理",
        "rem 作者签名：天咏（明月照今日，风摇过两川。）",
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

    # 附件 3：使用说明（UTF-8 无 BOM + LF 换行）
    readme = f"""{APP_NAME}（iNaturalist 鸟类数据采集与图像下载系统）v1.3
============================================
作者签名：天咏（明月照今日，风摇过两川。）

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
                            （数据库、照片、投放收件箱、模型库）

【运行方式】
  1. 双击「启动爬虫界面.bat」或直接双击 {APP_EXE}，
     均直接打开桌面控制台（推荐）。
     控制台内含「拖入文件爬取」标签页：可直接选择 txt/csv 处理，
     或启动常驻监视 data/drop_inbox，放入文件即自动爬取。
  2. 命令行用法（在 exe 所在文件夹的地址栏输入 cmd 回车后执行）：
     {APP_EXE} --help          （查看全部命令）
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

【训练数据集导出】（v1.2 新增）
  命令：{APP_EXE} dataset --out 导出目录
  功能：把数据库中已入库的照片一键导出为 PyTorch 可直接加载的
  标准数据集：
    1. train/val/test 三个目录（默认 8:1:1，按「观察」为单位
       划分，同一观察的照片不会跨集合，防止数据泄漏）；
    2. manifest.csv 与 manifest.jsonl 清单（含纲-目-科层级、
       许可证、署名、图片尺寸、SHA-256 指纹等字段）；
    3. 默认硬链接导出（同盘零拷贝，不产生磁盘占用翻倍）。

【模型训练】（v1.3 新增）
  桌面控制台「模型训练」标签页：选择训练模式与模型基名即可启动
  训练，实时查看终端日志。四种训练模式：
    1. 适应模式（ad）：读取本机 GPU 显存/内存/磁盘配置，在保证
       系统安全运行的前提下自动配置训练参数（安全优先、效果优先，
       允许以时间换效果）；
    2. 低速模式：低资源占用，训练慢但稳定；
    3. 高速模式：大批量+混合精度，快速出结果；
    4. 自定义模式：自行设置轮数、优化器、耐心值、批大小、学习率。
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
  配置，无需另行设置。视觉模型（ViT 物种识别、OWL-ViT 部位检测）
  不随安装包分发，由训练环境按其自带配置加载本地权重文件。

【卸载说明】
  卸载只移除程序文件，您的数据（照片、数据库、模型库）将完整
  保留在安装目录的 data 文件夹中，可自行决定是否删除。

【版本历史】
  v1.3  新增：模型训练（四模式）、模型库管理、视觉分析页签，
        应用更名为「鸟类」，与鸟类视觉分析系统共用模型库。
  v1.2  新增：训练数据集导出（dataset 命令）、分类树同步。
  v1.1  新增：层级文件夹自适应导入、中文分类名合并。
  v1.0  首个安装包版本：批量下载、拖入爬取、桌面控制台、
        数据面板、双层数据库。
"""
    # UTF-8 无 BOM + LF（与历史版本编码约定一致，勿用 GBK 往返读写）
    (ATTACH_DIR / "使用说明.txt").write_text(readme, encoding="utf-8", newline="\n")
    print(f"  [附件] 启动爬虫界面.bat / 拖入文件自动爬取.bat / 使用说明.txt 已生成")


def main() -> int:
    print(f"== {APP_NAME} v1.3 打包源码准备 ==")
    print(f"源码目录: {SRC_DIR}")
    print(f"输出目录: {PKG_DIR}")
    if not (SRC_DIR / "main.py").is_file():
        print("[错误] 未找到源码目录，请确认发布目录相对位置")
        return 1
    prepare_frozen_sources()
    prepare_vision_system()
    prepare_attachments()
    print("== 打包源码准备完成 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
