#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================
"""混合加密发布版构建脚本。

在既有「标识符混淆」的基础上叠加双层加密，构建混合加密发布版：
    第一层（混淆）：16 个业务模块标识符随机重命名（跨模块一致），
        移除文档字符串与注释，复用 build_scrambled_release.py 的
        成熟机制（含属性访问名排除修复）；
    第二层（对称加密）：混淆后的源码以 SHA-256 计数器模式流加密
        （每个模块独立的 32 字节随机内容密钥）；
    第三层（密钥封装）：模块内容密钥经「构建主密钥 + 模块盐」派生
        的掩码异或封装；构建主密钥分片变换后嵌入模块引导文件。

产出：与原文件同名的自解密引导 .py 文件——运行/导入时自动解密
装载原模块代码，所有调用路径（python main.py、import、GUI 子进程
「解释器 + 脚本路径」）保持完全不变，不影响程序运行。

vision_system 子目录（视觉分析系统）与顶层模块无相互导入，
采用「仅加密、不改名」策略，保证主程序按名引用其类的接口稳定。

用法：
    python build_hybrid_encrypted_release.py [--out 输出目录] [--seed 种子]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import random
import re
from datetime import datetime
from pathlib import Path

# 复用既有混淆构建脚本的成熟能力（同目录导入）
from build_scrambled_release import (
    SOURCE_FILES,
    build_rename_map,
    compute_source_fingerprint,
    scramble_source,
    stamp_build_placeholders,
)

BASE_DIR = Path(__file__).resolve().parent

# 视觉分析系统模块：与顶层模块零相互导入，仅加密不改名
VISION_DIR = BASE_DIR / "vision_system"

# 引导文件（stub）模板：运行时重组密钥 → 解密载荷 → 编译执行
# 说明：模板中全部占位符在构建时填充；辅助名使用 _hybrid 前缀，
# 与业务标识符（_v* 随机名/原业务名）无冲突空间。
STUB_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================
# 本文件为混合加密发布模块（受保护内容）：程序逻辑已加密封装，
# 运行时自动解密装载，请勿修改或篡改本文件。
import base64 as _hybrid_b64
import hashlib as _hybrid_sha
import sys as _hybrid_sys

_HYB_K1 = "{k1}"
_HYB_K2 = "{k2}"
_HYB_SALT = "{salt}"
_HYB_KEY = "{key}"
_HYB_DATA = "{data}"


def _hybrid_xor(a: bytes, b: bytes) -> bytes:
    return bytes(p ^ q for p, q in zip(a, b))


def _hybrid_load() -> None:
    # 重组构建主密钥（两片反转的 base64 片段）
    master = (_hybrid_b64.b64decode(_HYB_K1[::-1])
              + _hybrid_b64.b64decode(_HYB_K2[::-1]))
    salt = _hybrid_b64.b64decode(_HYB_SALT)
    # 密钥封装层：主密钥 + 盐 派生掩码，还原模块内容密钥
    mask = _hybrid_sha.sha256(master + salt).digest()
    content_key = _hybrid_xor(_hybrid_b64.b64decode(_HYB_KEY), mask)
    # 对称层：SHA-256 计数器模式流解密载荷
    raw = _hybrid_b64.b64decode(_HYB_DATA)
    blocks = (len(raw) + 31) // 32
    stream = b"".join(
        _hybrid_sha.sha256(content_key + i.to_bytes(8, "little")).digest()
        for i in range(blocks))
    source = _hybrid_xor(raw, stream[: len(raw)]).decode("utf-8")
    # 清理引导辅助名后，将原模块代码装载进本模块命名空间
    for _n in ("_HYB_K1", "_HYB_K2", "_HYB_SALT", "_HYB_KEY",
               "_HYB_DATA", "_hybrid_xor", "_hybrid_load"):
        globals().pop(_n, None)
    exec(compile(source, __file__, "exec"), globals())


_hybrid_load()
'''


def derive_keystream(key: bytes, length: int) -> bytes:
    """按 SHA-256 计数器模式生成解密用密钥流（构建端与引导端同构）。"""
    blocks = (length + 31) // 32
    return b"".join(
        hashlib.sha256(key + i.to_bytes(8, "little")).digest()
        for i in range(blocks))[:length]


def encrypt_module(payload: str, master_key: bytes, rng: random.Random) -> str:
    """对模块源码执行混合加密，返回自解密引导文件内容。

    对称层：随机内容密钥流加密（SHA-256 CTR）；
    封装层：内容密钥经（主密钥 + 盐）派生掩码异或封装。
    """
    data = payload.encode("utf-8")
    salt = bytes(rng.randrange(256) for _ in range(16))
    content_key = bytes(rng.randrange(256) for _ in range(32))
    masked_key = bytes(a ^ b for a, b in zip(
        content_key, hashlib.sha256(master_key + salt).digest()))
    cipher = bytes(a ^ b for a, b in zip(data, derive_keystream(content_key, len(data))))
    # 主密钥分片：两段 16 字节，各自 base64 后字符串反转嵌入
    half1 = base64.b64encode(master_key[:16]).decode("ascii")[::-1]
    half2 = base64.b64encode(master_key[16:]).decode("ascii")[::-1]
    return STUB_TEMPLATE.format(
        k1=half1,
        k2=half2,
        salt=base64.b64encode(salt).decode("ascii"),
        key=base64.b64encode(masked_key).decode("ascii"),
        data=base64.b64encode(cipher).decode("ascii"),
    )


def write_bat_files(out_dir: Path) -> None:
    """生成源码形态启动脚本（GBK 编码 + CRLF，符合 cmd 解析规范）。"""
    gui_bat = "\r\n".join([
        "@echo off",
        "rem 鸟类爬虫桌面控制台启动脚本（双击即可弹出操作界面）",
        "rem 作者签名：天咏（明月照今日，风摇过两川。）",
        'cd /d "%~dp0"',
        "rem 优先使用 pythonw（无黑色控制台窗口），不存在则回退 python",
        "where pythonw >nul 2>nul",
        "if %errorlevel%==0 (",
        '    start "" pythonw main.py gui',
        ") else (",
        '    start "" python main.py gui',
        ")",
        "",
    ])
    drop_bat = "\r\n".join([
        "@echo off",
        "rem 拖入自动处理接口：将文件或文件夹摆放到本脚本图标上即可",
        "rem 支持：txt/csv 清单（每行一个类群ID或物种名）→ 自动爬取",
        "rem       图片文件/文件夹（文件夹名=物种中文名/类群ID）→ 按种类指纹编码入库",
        "rem 可一次拖入多个项目；源文件保持原位，报告存于 data\\drop_inbox\\processed_已处理",
        "rem 作者签名：天咏（明月照今日，风摇过两川。）",
        'cd /d "%~dp0"',
        "",
        'if "%~1"=="" (',
        "    echo 未拖入文件：将以常驻监视模式运行，监视 data\\drop_inbox 目录。",
        "    echo 把 txt/csv 清单、图片或图片文件夹放入该目录即可自动处理，按 Ctrl+C 退出。",
        "    python drop_crawl_interface.py --watch",
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
        "python drop_crawl_interface.py --file %ARGS%",
        "",
        ":end",
        "pause",
        "",
    ])
    (out_dir / "启动爬虫界面.bat").write_bytes(gui_bat.encode("gbk"))
    (out_dir / "拖入文件自动爬取.bat").write_bytes(drop_bat.encode("gbk"))


def write_usage(out_dir: Path, fingerprint: str, build_time: str,
                app_version: str) -> None:
    """生成使用说明（含数字水印与混合加密说明）。"""
    usage = "\n".join([
        "iNaturalist 鸟类数据采集与图像下载系统（混合加密发布版）",
        "=" * 50,
        "作者签名：天咏（明月照今日，风摇过两川。）",
        "版权声明：2026 天咏 版权所有（明月照今日，风摇过两川。）",
        f"数字水印：TYWM-BIRD-v{app_version}-{fingerprint}",
        f"构建指纹：{fingerprint}",
        f"构建时间：{build_time}",
        "",
        "【版权与加密说明】",
        "  本发布版采用混合加密（标识符混淆 + 对称流加密 + 密钥封装），",
        "  全部模块以自解密引导文件分发，运行时自动解密装载。",
        "  请勿修改、反编译或去除程序内嵌的数字水印与版权信息。",
        "",
        "【环境要求】",
        "  Windows 10/11 + Python 3.10 及以上（无需安装任何第三方库）",
        "",
        "【运行方式】",
        "  1. 双击「启动爬虫界面.bat」打开桌面控制台（推荐）",
        "     控制台内含「拖入文件爬取」标签页：可直接选择 txt/csv 处理，",
        "     或启动常驻监视 data/drop_inbox，放入文件即自动爬取。",
        "  2. 或命令行运行：python main.py            （查看全部命令）",
        "     python main.py gui                      （桌面控制台）",
        "     python main.py batch                    （批量下载 15 个鸟类科）",
        "     python main.py panel                    （浏览器数据面板）",
        "     python main.py stats                    （数据库统计）",
        "     python main.py health                   （数据库健康检查）",
        "     python main.py copyright                （版权与水印信息）",
        "",
        "【拖入文件自动爬取】",
        "  1. 文本清单：将包含「类群ID 或 物种名」的 txt/csv 文件拖到",
        "     「拖入文件自动爬取.bat」图标上，即自动解析并爬取。",
        "  2. 本地图片入库：将图片文件夹（或若干图片文件）拖到同一",
        "     bat 图标上，按文件夹名/子文件夹名解析物种，图片按种类",
        "     依次指纹编码入库：内容 SHA-256 去重，重复图片自动跳过，",
        "     源文件保留原位。",
        "",
        "【数据存储】",
        "  照片按种类存于 data/taxon_<ID>_<名称>/photos/",
        "  数据库存于 data/db/（raw_data.db 原始留存 +",
        "  bird_observations.db 规范主库，SHA-256 指纹自动去重）",
        "  模型库存于 data/model_library/（训练完成的模型自动入库）",
        "",
    ])
    (out_dir / "使用说明.txt").write_text(usage, encoding="utf-8")


def build_release(out_dir: Path, seed: int | None = None) -> dict[str, str]:
    """构建混合加密发布版，返回构建信息（水印/指纹/统计）。"""
    rng = random.Random(seed)
    master_key = os.urandom(32)  # 构建主密钥（每次构建随机）

    # ---------- 第一层：读取并混淆 16 个业务模块 ----------
    import ast

    trees = []
    sources: dict[str, str] = {}
    for name in SOURCE_FILES:
        text = (BASE_DIR / name).read_text(encoding="utf-8")
        sources[name] = text
        trees.append(ast.parse(text))

    fingerprint = compute_source_fingerprint(sources)
    build_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    version_match = re.search(r'APP_VERSION = "([^"]+)"', sources["main.py"])
    app_version = version_match.group(1) if version_match else "未知"
    rename_map = build_rename_map(trees, rng)

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in sources.items():
        scrambled = scramble_source(text, rename_map)
        if name == "main.py":
            scrambled = stamp_build_placeholders(scrambled, fingerprint, build_time)
        stub = encrypt_module(scrambled, master_key, rng)
        ast.parse(stub)  # 引导文件语法自检
        (out_dir / name).write_text(stub, encoding="utf-8")
        print(f"已混淆+加密: {name}")

    # ---------- vision_system：仅加密不改名 ----------
    vision_out = out_dir / "vision_system"
    vision_out.mkdir(exist_ok=True)
    for py in sorted(VISION_DIR.glob("*.py")):
        payload = py.read_text(encoding="utf-8")
        stub = encrypt_module(payload, master_key, rng)
        ast.parse(stub)
        (vision_out / py.name).write_text(stub, encoding="utf-8")
        print(f"已加密: vision_system/{py.name}")

    # ---------- 启动脚本 / 图标 / 使用说明 ----------
    write_bat_files(out_dir)
    # 图标资源：星云封面（ico 供快捷方式，png 供 GUI 窗口图标）
    import shutil
    for res in ("星云图标.ico", "星云图标.png"):
        if (BASE_DIR / res).exists():
            shutil.copy2(BASE_DIR / res, out_dir / res)
            print(f"已复制: {res}")
    write_usage(out_dir, fingerprint, build_time, app_version)
    print("已生成: 使用说明.txt / 启动爬虫界面.bat / 拖入文件自动爬取.bat")

    info = {
        "fingerprint": fingerprint,
        "build_time": build_time,
        "watermark": f"TYWM-BIRD-v{app_version}-{fingerprint}",
        "renamed": str(len(rename_map)),
    }
    print(f"\n混合加密发布版构建完成: {out_dir}")
    print(f"源码指纹: {fingerprint}  构建时间: {build_time}")
    print(f"数字水印: {info['watermark']}")
    print(f"标识符重命名条目数: {len(rename_map)}")
    return info


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="混合加密发布版构建脚本")
    parser.add_argument("--out", type=Path, default=None,
                        help="输出目录（默认 同级目录/程序_混合加密）")
    parser.add_argument("--seed", type=int, default=None,
                        help="随机种子（可复现构建；主密钥仍每次随机）")
    args = parser.parse_args()
    out_dir = args.out or BASE_DIR.parent / "程序_混合加密"
    build_release(out_dir, seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
