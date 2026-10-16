#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

# 路径相对脚本目录解析，避免硬编码
_vfca465fc = Path(__file__).resolve().parent
DATASETS_DIR = _vfca465fc / "data" / "train_datasets"
_vdd14865b = DATASETS_DIR / "registry.json"

# 数据集统计时认定的图片后缀（与拖入导入接口保持一致）
_vaa115322 = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# 数据集名称中不允许出现的字符（Windows 文件系统非法字符 + 控制符）
_vba6e9821 = '\\/:*?"<>|'


def clean_name(name: str) -> str:
    cleaned = "".join(ch for ch in (name or "").strip() if ch not in _vba6e9821)
    return cleaned.strip()


def _vd96965d3() -> Path:
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    return DATASETS_DIR


def _vf298d7c9() -> list[dict]:
    try:
        text = _vdd14865b.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return []
    items = data.get("datasets") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    # 逐条校验：必须有名称与路径，坏条目直接跳过
    return [d for d in items
            if isinstance(d, dict) and d.get("name") and d.get("path")]


def _v59322f41(datasets: list[dict]) -> None:
    _vd96965d3()
    tmp = _vdd14865b.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"version": 1, "datasets": datasets},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_vdd14865b)


def dataset_exists(name: str) -> bool:
    return any(d["name"] == name for d in _vf298d7c9())


def add_dataset(name: str, path: Path, config: dict,
                stats: dict | None = None) -> dict:
    datasets = _vf298d7c9()
    entry = {
        "name": name,
        "path": str(path),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": config,
        "stats": stats or {},
    }
    datasets.append(entry)
    _v59322f41(datasets)
    return entry


def remove_dataset(name: str, delete_files: bool = True) -> bool:
    datasets = _vf298d7c9()
    remain = [d for d in datasets if d["name"] != name]
    if len(remain) == len(datasets):
        return False
    target = next(d for d in datasets if d["name"] == name)
    _v59322f41(remain)
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
                                if f.is_file() and f.suffix.lower() in _vaa115322)
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
                               if f.is_file() and f.suffix.lower() in _vaa115322)
        if flat_classes:
            stats["split_detail"]["train"] = {"classes": flat_classes,
                                              "images": flat_images}
            stats["images"] = flat_images
    stats["classes"] = len(classes)
    return stats


def list_available() -> list[dict]:
    result = []
    for d in _vf298d7c9():
        if Path(d["path"]).is_dir():
            result.append(d)
    return result


if __name__ == "__main__":
    # 独立运行时打印注册表概览，便于命令行快速核查
    print(f"数据集根目录: {DATASETS_DIR}")
    for item in _vf298d7c9():
        st = item.get("stats") or {}
        print(f"  {item['name']} | 类别 {st.get('classes', '?')} | "
              f"图片 {st.get('images', '?')} | {item['path']}")
