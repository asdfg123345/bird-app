#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================
"""训练数据集注册表（data/train_datasets/registry.json 的读写与统计）。

用途：
    「训练数据集」页签生成的每个数据集都需要登记到注册表，
    供数据集列表展示与训练页「已生成数据集」下拉联动选用。
    本模块只做纯文件读写与统计，不依赖 GUI，可独立测试。

注册表结构（data/train_datasets/registry.json）：
    {
      "version": 1,
      "datasets": [
        {
          "name": "数据集名称",
          "path": "数据集目录绝对路径",
          "created_at": "2026-10-13 21:30:00",
          "config": {"taxon_ids": [827400, ...], "splits": "8:1:1",
                     "min_per_class": 0, "max_per_class": 0,
                     "label_scheme": "cn", "seed": 42},
          "stats": {"classes": 3, "images": 500,
                    "split_detail": {"train": {"classes": 3, "images": 400}, ...}}
        }
      ]
    }

容错约定：
    注册表文件不存在或 JSON 损坏时按空表处理；单个条目缺字段时跳过，
    绝不因注册表问题阻塞界面（数据集目录本身仍在磁盘上）。
"""

from __future__ import annotations


# ============================================================
# 冻结环境适配：打包为 exe 后，数据目录固定到 exe 所在目录
# ============================================================
def _frozen_base(src_file):
    """打包后返回 exe 所在目录；源码运行返回脚本所在目录。"""
    import sys as _sys
    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(src_file).resolve().parent


import json
import shutil
from datetime import datetime
from pathlib import Path

# 路径相对脚本目录解析，避免硬编码
BASE_DIR = _frozen_base(__file__)
DATASETS_DIR = BASE_DIR / "data" / "train_datasets"
REGISTRY_PATH = DATASETS_DIR / "registry.json"

# 数据集统计时认定的图片后缀（与拖入导入接口保持一致）
IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# 数据集名称中不允许出现的字符（Windows 文件系统非法字符 + 控制符）
_ILLEGAL_CHARS = '\\/:*?"<>|'


def clean_name(name: str) -> str:
    """清洗数据集名称：剔除文件系统非法字符与首尾空白。

    清洗后为空则返回空串，由调用方提示用户重新输入。
    """
    cleaned = "".join(ch for ch in (name or "").strip() if ch not in _ILLEGAL_CHARS)
    return cleaned.strip()


def ensure_dirs() -> Path:
    """确保数据集根目录存在，返回该目录。"""
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    return DATASETS_DIR


def load_registry() -> list[dict]:
    """读取注册表，返回数据集条目列表（容错：损坏/缺失返回空表）。"""
    try:
        text = REGISTRY_PATH.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return []
    items = data.get("datasets") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    # 逐条校验：必须有名称与路径，坏条目直接跳过
    return [d for d in items
            if isinstance(d, dict) and d.get("name") and d.get("path")]


def save_registry(datasets: list[dict]) -> None:
    """把数据集条目列表写回注册表（原子覆盖写，先写临时文件再替换）。"""
    ensure_dirs()
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"version": 1, "datasets": datasets},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)


def dataset_exists(name: str) -> bool:
    """判断同名数据集是否已登记（名称唯一性校验）。"""
    return any(d["name"] == name for d in load_registry())


def add_dataset(name: str, path: Path, config: dict,
                stats: dict | None = None) -> dict:
    """登记一个新数据集并保存注册表，返回登记条目。"""
    datasets = load_registry()
    entry = {
        "name": name,
        "path": str(path),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": config,
        "stats": stats or {},
    }
    datasets.append(entry)
    save_registry(datasets)
    return entry


def remove_dataset(name: str, delete_files: bool = True) -> bool:
    """按名称删除数据集：先移除注册表条目，再按需删除磁盘目录。

    目录删除仅针对登记路径本身（硬链接删除不影响库内原片）；
    返回是否成功删除条目（未找到同名条目时返回 False）。
    """
    datasets = load_registry()
    remain = [d for d in datasets if d["name"] != name]
    if len(remain) == len(datasets):
        return False
    target = next(d for d in datasets if d["name"] == name)
    save_registry(remain)
    if delete_files:
        target_dir = Path(target["path"])
        # 安全约束：仅删除位于数据集根目录内的目录，避免误删任意路径
        try:
            if (target_dir.is_dir()
                    and target_dir.resolve().parent == DATASETS_DIR.resolve()):
                shutil.rmtree(target_dir, ignore_errors=True)
        except OSError:
            pass  # 目录删除失败不影响注册表已删除的状态
    return True


def scan_dataset_stats(path: Path) -> dict:
    """扫描数据集目录，统计各划分的类别数与图片数。

    预期结构：path/{train|val|test}/{类别名}/*.jpg；
    兼容自动划分前的平铺结构（path/{类别名}/*.jpg 计入 train）。
    """
    stats: dict = {"classes": 0, "images": 0, "split_detail": {}}
    if not path.is_dir():
        return stats
    classes: set[str] = set()
    for split in ("train", "val", "test"):
        split_dir = path / split
        if not split_dir.is_dir():
            continue
        split_classes = 0
        split_images = 0
        for class_dir in split_dir.iterdir():
            if not class_dir.is_dir():
                continue
            split_classes += 1
            classes.add(class_dir.name)
            split_images += sum(1 for f in class_dir.iterdir()
                                if f.is_file() and f.suffix.lower() in IMG_SUFFIXES)
        stats["split_detail"][split] = {"classes": split_classes,
                                        "images": split_images}
        stats["images"] += split_images
    # 平铺结构兜底（无 train/val/test 子目录时按 train 统计）
    if not stats["split_detail"]:
        flat_classes = 0
        flat_images = 0
        for class_dir in path.iterdir():
            if not class_dir.is_dir():
                continue
            flat_classes += 1
            classes.add(class_dir.name)
            flat_images += sum(1 for f in class_dir.iterdir()
                               if f.is_file() and f.suffix.lower() in IMG_SUFFIXES)
        if flat_classes:
            stats["split_detail"]["train"] = {"classes": flat_classes,
                                              "images": flat_images}
            stats["images"] = flat_images
    stats["classes"] = len(classes)
    return stats


def list_available() -> list[dict]:
    """列出注册表中有磁盘目录的数据集（目录缺失的条目跳过）。"""
    result = []
    for d in load_registry():
        if Path(d["path"]).is_dir():
            result.append(d)
    return result


if __name__ == "__main__":
    # 独立运行时打印注册表概览，便于命令行快速核查
    print(f"数据集根目录: {DATASETS_DIR}")
    for item in load_registry():
        st = item.get("stats") or {}
        print(f"  {item['name']} | 类别 {st.get('classes', '?')} | "
              f"图片 {st.get('images', '?')} | {item['path']}")
