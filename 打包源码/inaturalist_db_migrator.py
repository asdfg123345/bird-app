#!/usr/bin/env python3
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-07
"""iNaturalist 鸟类下载项目历史数据迁移脚本。

将已有的 taxon_* 目录下的 observations.csv、photos.csv、summary.json
以及根目录的 batch_summary.json 导入到 SQLite 两层数据库中。

用法示例：
    python inaturalist_db_migrator.py
    python inaturalist_db_migrator.py --out-dir d:/鸟类/iNaturalist/iNaturalist/data
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from inaturalist_db_manager import INaturalistDatabaseManager


# 英文 CSV 字段到中文字段的映射（用于原始库）
OBS_FIELD_MAP: dict[str, str] = {
    "observation_id": "观察记录ID",
    "uuid": "UUID",
    "uri": "链接",
    "quality_grade": "质量等级",
    "scientific_name": "学名",
    "common_name": "俗名",
    "taxon_id": "类群ID",
    "taxon_rank": "类群等级",
    "species_guess": "猜测种名",
    "observed_on": "观察日期",
    "time_observed_at": "观察时间",
    "time_zone": "时区",
    "latitude": "纬度",
    "longitude": "经度",
    "place_guess": "地点猜测",
    "place_names": "地点名称",
    "positional_accuracy_m": "位置精度",
    "public_positional_accuracy_m": "公开位置精度",
    "geoprivacy": "地理隐私",
    "taxon_geoprivacy": "类群地理隐私",
    "obscured": "是否模糊",
    "mappable": "是否可映射",
    "captive": "是否圈养",
    "description": "描述",
    "observation_license": "观察许可证",
    "observer_id": "观察者ID",
    "observer_login": "观察者登录名",
    "observer_name": "观察者姓名",
    "identifications_count": "鉴定数",
    "photo_count": "照片数",
    "sound_count": "声音数",
    "local_photo_files": "本地照片文件",
}

PHOTO_FIELD_MAP: dict[str, str] = {
    "observation_id": "观察记录ID",
    "photo_id": "照片ID",
    "position": "位置",
    "license_code": "许可协议",
    "attribution": "署名",
    "width": "宽度",
    "height": "高度",
    "requested_size": "请求尺寸",
    "downloaded_size": "实际下载尺寸",
    "original_url": "原始URL",
    "local_path": "本地路径",
    "download_ok": "下载成功",
}


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="将已有 CSV/JSON 数据迁移到 SQLite 数据库。"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data",
        help="包含 taxon_* 目录的输出根目录（默认脚本所在目录下的 data）。",
    )
    parser.add_argument(
        "--db-dir",
        type=Path,
        default=None,
        help="数据库目录（默认 out-dir/db）。",
    )
    parser.add_argument(
        "--raw-db",
        type=Path,
        default=None,
        help="原始数据库路径（默认 db-dir/raw_data.db）。",
    )
    parser.add_argument(
        "--main-db",
        type=Path,
        default=None,
        help="主数据库路径（默认 db-dir/bird_observations.db）。",
    )
    parser.add_argument(
        "--batch-uuid",
        type=str,
        default=None,
        help="迁移批次 UUID（默认自动生成）。",
    )
    return parser.parse_args()


def _resolve_db_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """根据参数解析出两个数据库的绝对路径。"""
    db_dir = args.db_dir or (args.out_dir / "db")
    raw_db = args.raw_db or (db_dir / "raw_data.db")
    main_db = args.main_db or (db_dir / "bird_observations.db")
    return Path(raw_db), Path(main_db)


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    """读取 CSV 文件为字典列表。"""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [row for row in reader]


def _read_json(path: Path) -> dict[str, Any] | None:
    """读取 JSON 文件。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _to_chinese_obs(obs: dict[str, Any]) -> dict[str, Any]:
    """将英文观察记录字段映射为中文字段，用于原始库写入。"""
    return {cn: obs.get(en) for en, cn in OBS_FIELD_MAP.items()}


def _to_chinese_photo(photo: dict[str, Any]) -> dict[str, Any]:
    """将英文照片字段映射为中文字段，用于原始库写入。"""
    return {cn: photo.get(en) for en, cn in PHOTO_FIELD_MAP.items()}


def migrate_taxon_directory(
    manager: INaturalistDatabaseManager,
    taxon_dir: Path,
    batch_id: int | None,
) -> dict[str, Any]:
    """迁移单个类群目录的数据到数据库。"""
    summary = _read_json(taxon_dir / "summary.json") or {}
    taxon_id = summary.get("taxon_id")
    scientific_name = summary.get("scientific_name", "")
    common_name = summary.get("common_name", "")
    quality_grade = summary.get("quality_grade", "")

    if taxon_id is None:
        # 尝试从目录名解析 taxon_id
        match = Path(taxon_dir.name).name.split("_")
        if len(match) >= 2 and match[1].isdigit():
            taxon_id = int(match[1])

    if taxon_id is None:
        return {"taxon_dir": str(taxon_dir), "error": "无法识别 taxon_id"}

    # 登记类群
    manager.ensure_taxon(
        taxon_id=taxon_id,
        scientific_name=scientific_name,
        common_name=common_name,
        rank="family",
        label_cn="",
    )

    observations = _read_csv_rows(taxon_dir / "observations.csv")
    photos = _read_csv_rows(taxon_dir / "photos.csv")

    crawl_time = summary.get("batch_time") or datetime.now().isoformat(timespec="seconds")

    # 规范化照片行：CSV 中的 download_ok 可能是字符串 "True"/"False"
    for photo in photos:
        photo["download_ok"] = str(photo.get("download_ok", "")).lower() == "true"

    # 写入两层数据库
    for obs in observations:
        manager.upsert_observation(obs)
        manager.insert_raw_observation(obs, {}, crawl_time)
        if batch_id is not None:
            oid = obs.get("observation_id")
            if oid is not None:
                manager.link_batch_observation(batch_id, int(oid))

    for photo in photos:
        manager.upsert_photo(photo)
        manager.insert_raw_photo(photo, {}, crawl_time)

    obs_count = len(observations)
    photo_count = len(photos)
    downloaded = sum(1 for p in photos if p.get("download_ok"))
    failed = photo_count - downloaded

    if batch_id is not None:
        manager.record_batch_taxon(
            batch_id=batch_id,
            taxon_id=taxon_id,
            observation_count=obs_count,
            photo_count=photo_count,
            photos_downloaded=downloaded,
            photos_failed=failed,
        )

    return {
        "taxon_id": taxon_id,
        "scientific_name": scientific_name,
        "common_name": common_name,
        "observation_count": obs_count,
        "photo_count": photo_count,
        "photos_downloaded": downloaded,
        "photos_failed": failed,
    }


def migrate_batch_summary(
    manager: INaturalistDatabaseManager,
    batch_summary_path: Path,
) -> int:
    """根据 batch_summary.json 创建一条合成批次记录，返回 batch_id。"""
    data = _read_json(batch_summary_path)
    if data is None:
        return manager.create_batch_run(
            batch_time=datetime.now().isoformat(timespec="seconds")
        )

    return manager.create_batch_run(
        batch_time=data.get("batch_time"),
        taxa_requested=data.get("taxa_requested", 0),
        quality_grade=data.get("quality_grade", ""),
        photo_size=data.get("photo_size", ""),
        max_per_taxon=data.get("max_per_taxon", 0),
        output_dir=data.get("output_dir", ""),
        total_observations=data.get("total_observations", 0),
        total_photos=data.get("total_photos", 0),
        total_downloaded=data.get("total_photos_downloaded", 0),
        total_failed=data.get("total_photos_failed", 0),
    )


def main() -> int:
    """迁移入口。"""
    args = parse_args()
    raw_db, main_db = _resolve_db_paths(args)

    print(f"输出目录: {args.out_dir}")
    print(f"原始数据库: {raw_db}")
    print(f"主数据库: {main_db}")

    if not args.out_dir.exists():
        print(f"错误：输出目录不存在 {args.out_dir}")
        return 1

    manager = INaturalistDatabaseManager(raw_db, main_db, batch_uuid=args.batch_uuid)

    # 查找所有 taxon_* 目录
    taxon_dirs = sorted([d for d in args.out_dir.iterdir() if d.is_dir() and d.name.startswith("taxon_")])
    print(f"发现 {len(taxon_dirs)} 个类群目录")

    # 如果存在 batch_summary.json，先创建合成批次
    batch_summary_path = args.out_dir / "batch_summary.json"
    batch_id: int | None = None
    if batch_summary_path.exists():
        print("检测到 batch_summary.json，创建合成批次记录...")
        batch_id = migrate_batch_summary(manager, batch_summary_path)
        print(f"合成批次 ID: {batch_id}")
    else:
        print("未检测到 batch_summary.json，跳过批次关联")

    results: list[dict[str, Any]] = []
    errors: list[str] = []

    for index, taxon_dir in enumerate(taxon_dirs, start=1):
        print(f"[{index}/{len(taxon_dirs)}] 迁移 {taxon_dir.name} ...")
        try:
            result = migrate_taxon_directory(manager, taxon_dir, batch_id)
            results.append(result)
            if "error" in result:
                errors.append(f"{taxon_dir.name}: {result['error']}")
            else:
                print(
                    f"  观察记录 {result['observation_count']} 条，"
                    f"照片 {result['photo_count']} 张"
                )
        except Exception as exc:
            errors.append(f"{taxon_dir.name}: {exc}")
            print(f"  迁移失败: {exc}")

    if batch_id is not None:
        # 更新批次成功数
        succeeded = sum(1 for r in results if "error" not in r)
        manager.update_batch_run(batch_id, taxa_succeeded=succeeded)

    manager.close()

    print("\n迁移完成：")
    print(f"  成功类群: {len(results) - len(errors)}/{len(taxon_dirs)}")
    print(f"  观察记录总数: {manager.get_observation_count()}")
    print(f"  照片总数: {manager.get_photo_count()}")
    if errors:
        print("  失败项:")
        for err in errors:
            print(f"    - {err}")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
