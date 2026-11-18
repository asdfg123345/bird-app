#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================
"""准备打包源码：把 src 整棵目录复制为发布暂存目录（打包源码/）。

为什么需要本脚本（补齐 README 中缺失的环节）：
    安装包脚本 [Files] 的 Source 是「打包源码」目录下的全部内容，也就是说
    安装包内容的唯一来源是这个目录。此前该目录靠人工整理，新增子系统
    （如 part_model_training、vision_system）容易被漏掉，直到用户装完才
    发现功能点不开。本脚本用「整树复制 + 关键文件断言」消除这类漏包。

它做四件事：
    1. 整树复制 src → 输出目录（排除缓存、运行时产物、开发机本地配置）；
    2. 用全部 .py 计算构建指纹（基于注入前的源码内容），替换 main.py 中的
       @@BUILD_FINGERPRINT@@ / @@BUILD_TIME@@ 占位符；
    3. 断言关键文件齐全（部位训练、视觉系统、LICENSE、启动脚本），缺一即失败；
    4. 写出 构建信息.json，并在指定 --iss 时校验其 AppVersion 与 version.py 一致。

用法：
    python packaging/准备打包源码.py
    python packaging/准备打包源码.py --out D:/发布/打包源码 --force
    python packaging/准备打包源码.py --with-data --data-dir D:/鸟类/data
    python packaging/准备打包源码.py --iss packaging/鸟类_安装包_20261015_002.iss

边界：
    本脚本只准备「源码形态」的暂存目录，不负责 PyInstaller 打包；
    若发布形态为 exe，需在此基础上再执行 PyInstaller。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent      # packaging/
PROJECT_ROOT = BASE_DIR.parent                  # bird-app/
DEFAULT_SRC = PROJECT_ROOT / "src"

# 不进入发布包的目录名（任意层级匹配）
EXCLUDE_DIR_NAMES = {
    "__pycache__", ".git", ".idea", ".vscode", "tests",
    "output", "training_output", "gallery_index_cache",
    "models_cache", "drop_inbox", "发布附件",
}
# 不进入发布包的文件名
EXCLUDE_FILE_NAMES = {"本地环境路径.local.json", ".DS_Store", "Thumbs.db"}
# 不进入发布包的后缀（运行时产物；.db 不在列表内，含数据版需要）
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".part", ".lock", ".log"}

# 复制后必须存在的关键文件（缺一即失败）
REQUIRED_FILES = (
    "main.py",
    "version.py",
    "LICENSE",
    "使用说明.txt",
    "启动爬虫界面.bat",
    "拖入文件自动爬取.bat",
    "part_model_training/train_segpart.py",
    "part_model_training/train_detector_entry.py",
    "part_model_training/train_generic_seg.py",
    "part_model_training/train_generic_det.py",
    "vision_system/gui_app.py",
    "vision_system/qa_server.py",
)


def _make_ignore(with_data: bool):
    """构造 copytree 的 ignore 回调：按目录名 / 文件名 / 后缀排除。"""
    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            path = Path(directory) / name
            if name in EXCLUDE_FILE_NAMES:
                ignored.add(name)
                continue
            if path.is_file() and path.suffix.lower() in EXCLUDE_SUFFIXES:
                ignored.add(name)
                continue
            if path.is_dir():
                if name == "data":
                    if not with_data:
                        ignored.add(name)
                    continue
                if name in EXCLUDE_DIR_NAMES:
                    ignored.add(name)
        return ignored
    return _ignore


def compute_fingerprint(root: Path) -> str:
    """按「相对路径 + 内容」计算全部 .py 的 SHA-256 指纹（16 位大写）。

    注意：必须在替换 main.py 占位符之前计算，否则指纹会包含自身，
    成为循环依赖。
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16].upper()


def stamp_build_info(main_py: Path, fingerprint: str, build_time: str) -> None:
    """把构建指纹与时间写入 main.py 的占位符，并确认已无残留。"""
    text = main_py.read_text(encoding="utf-8")
    text = text.replace("@@BUILD_FINGERPRINT@@", fingerprint)
    text = text.replace("@@BUILD_TIME@@", build_time)
    main_py.write_text(text, encoding="utf-8")
    leftover = [token for token in ("@@BUILD_FINGERPRINT@@", "@@BUILD_TIME@@")
                if token in text]
    if leftover:
        raise RuntimeError(f"main.py 占位符替换不完整：{leftover}")


def read_version(version_py: Path) -> str:
    """从 version.py 读取 APP_VERSION（与两个构建脚本同一正则口径）。"""
    if not version_py.is_file():
        raise RuntimeError(f"缺少 {version_py}")
    match = re.search(r'^APP_VERSION = "([^"]+)"',
                      version_py.read_text(encoding="utf-8"),
                      re.MULTILINE)
    if not match:
        raise RuntimeError("version.py 中未找到 APP_VERSION 定义")
    return match.group(1)


def check_iss_version(iss: Path, version: str) -> list[str]:
    """校验 .iss 的 AppVersion 与 version.py 一致，返回告警列表。"""
    if not iss.is_file():
        return [f"未找到安装包脚本：{iss}"]
    text = iss.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^AppVersion=(.+)$", text, re.MULTILINE)
    if not match:
        return [f"{iss.name} 中未找到 AppVersion"]
    iss_version = match.group(1).strip()
    if iss_version != version:
        return [f"AppVersion 不一致：.iss = {iss_version}，"
                f"version.py = {version}（发版前须同步）"]
    return []


def main(argv: list[str] | None = None) -> int:
    """命令行入口：准备发布暂存目录并输出校验结果。"""
    parser = argparse.ArgumentParser(
        description="准备打包源码（整树复制 + 指纹注入 + 一致性校验）")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC,
                        help="源码目录（默认 ../src）")
    parser.add_argument("--out", type=Path, default=None,
                        help="输出目录（默认 ../发布_鸟类应用_<日期>/打包源码）")
    parser.add_argument("--with-data", action="store_true",
                        help="连同 data/ 一起打包（含数据版安装包）")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="替换 src/data 的数据目录（配合 --with-data）")
    parser.add_argument("--iss", type=Path, default=None,
                        help="安装包脚本：校验 AppVersion 并复制到输出上级目录")
    parser.add_argument("--force", action="store_true",
                        help="输出目录已存在时先删除")
    args = parser.parse_args(argv)

    src = args.src.resolve()
    if not (src / "main.py").is_file():
        print(f"[错误] 源目录缺少 main.py：{src}")
        return 2

    default_out = PROJECT_ROOT / f"发布_鸟类应用_{datetime.now():%Y%m%d}" / "打包源码"
    out = (args.out or default_out).resolve()
    if out == src or src in out.parents:
        print(f"[错误] 输出目录不能位于源码目录内：{out}")
        return 2
    if out.exists():
        if not args.force:
            print(f"[错误] 输出目录已存在：{out}\n       如需覆盖请加 --force")
            return 2
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 1) 整树复制
    shutil.copytree(src, out, ignore=_make_ignore(args.with_data))

    # 2) 含数据版：补充/替换 data 目录
    data_note = "不含数据"
    if args.with_data:
        if args.data_dir:
            data_src = args.data_dir.resolve()
            if not data_src.is_dir():
                print(f"[错误] --data-dir 不存在：{data_src}")
                return 2
            shutil.copytree(data_src, out / "data", dirs_exist_ok=True)
            data_note = f"数据来自 {data_src}"
        else:
            data_note = "数据来自 src/data"

    # 3) 指纹 + 构建时间注入
    try:
        version = read_version(src / "version.py")
        build_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fingerprint = compute_fingerprint(out)
        stamp_build_info(out / "main.py", fingerprint, build_time)
    except (OSError, RuntimeError) as exc:
        print(f"[失败] 构建信息注入出错：{exc}")
        return 1

    # 4) 关键文件断言
    missing = [name for name in REQUIRED_FILES if not (out / name).is_file()]
    if missing:
        print("[失败] 发布目录缺少关键文件：")
        for name in missing:
            print(f"       - {name}")
        return 1

    # 5) 安装包脚本校验与复制
    warnings: list[str] = []
    if args.iss:
        iss = args.iss.resolve()
        warnings.extend(check_iss_version(iss, version))
        if iss.is_file():
            shutil.copy2(iss, out.parent / iss.name)

    # 6) 构建信息清单
    info = {
        "version": version,
        "build_time": build_time,
        "fingerprint": fingerprint,
        "watermark": f"TYWM-BIRD-v{version}-{fingerprint}",
        "data": data_note,
        "files": sum(1 for p in out.rglob("*") if p.is_file()),
    }
    (out.parent / "构建信息.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[完成] 版本 {version}  指纹 {fingerprint}")
    print(f"       输出 {out}")
    print(f"       文件 {info['files']} 个（{data_note}）")
    for line in warnings:
        print(f"[告警] {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
