#!/usr/bin/env python3
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-05
"""批量下载多个鸟类类群的 iNaturalist 观察数据与图片。

基于官方 API（https://api.inaturalist.org/v1），合规采集。
每个类群（科）的图片自动存放于独立文件夹：
    data/taxon_{id}_{学名}_{俗名}/photos/
并生成各自的 observations.csv、photos.csv、summary.json 元数据。

默认内置 15 个常见鸟类科；默认仅下载研究级（research grade）观察，
并限制每个类群的最大观察数，防止大类群（如鸭科 486 万条）拖垮内存。

用法示例：
    python batch_download_birds.py
    python batch_download_birds.py --max-per-taxon 100
    python batch_download_birds.py --taxon-ids 574 19728
    python batch_download_birds.py --taxon-ids 574 19728 --max-per-taxon 50 --workers 4
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from download_inaturalist import download_taxon_data
from inaturalist_db_manager import INaturalistDatabaseManager

# 默认鸟类类群列表（科级别，附中文名便于识别）
# 数据量为 2026-09 查询值，仅供参考
DEFAULT_TAXA: list[dict[str, Any]] = [
    {"taxon_id": 574, "label_cn": "雉科"},      # Phasianidae（鸡形目，含竹鸡、鹧鸪等）
    {"taxon_id": 6912, "label_cn": "鸭科"},     # Anatidae（雁形目，雁、鸭、天鹅）
    {"taxon_id": 5067, "label_cn": "鹰科"},     # Accipitridae（鹰形目，鹰、雕、鸢）
    {"taxon_id": 19728, "label_cn": "鸱鸮科"},  # Strigidae（鸮形目，猫头鹰）
    {"taxon_id": 17599, "label_cn": "啄木鸟科"},  # Picidae（鴷形目）
    {"taxon_id": 2715, "label_cn": "鸠鸽科"},   # Columbidae（鸽形目）
    {"taxon_id": 1627, "label_cn": "杜鹃科"},   # Cuculidae（鹃形目）
    {"taxon_id": 13685, "label_cn": "雀科"},    # Passeridae（雀形目，麻雀等）
    {"taxon_id": 12704, "label_cn": "鹟科"},    # Muscicapidae（雀形目，鹟、鸲等）
    {"taxon_id": 15977, "label_cn": "鸫科"},    # Turdidae（雀形目，鸫等）
    {"taxon_id": 4929, "label_cn": "鹭科"},     # Ardeidae（鹈形目，鹭类）
    {"taxon_id": 4342, "label_cn": "鸥科"},     # Laridae（鸻形目，鸥类）
    {"taxon_id": 3835, "label_cn": "鹬科"},     # Scolopacidae（鸻形目，鹬类）
    {"taxon_id": 154, "label_cn": "秧鸡科"},    # Rallidae（鹤形目）
    {"taxon_id": 20967, "label_cn": "戴胜科"},  # Upupidae（犀鸟目，戴胜）
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch download iNaturalist bird photos by family (one folder per taxon)."
    )
    parser.add_argument(
        "--taxon-ids",
        type=int,
        nargs="*",
        default=None,
        help="自定义类群 taxon_id 列表；不指定则使用内置 15 个鸟类科。",
    )
    parser.add_argument(
        "--max-per-taxon",
        type=int,
        default=200,
        help="每个类群最多获取的观察记录数（默认 200；0 表示不限制）。",
    )
    parser.add_argument(
        "--photo-size",
        choices=["original", "large", "medium"],
        default="large",
        help="图片尺寸（默认 large）。",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="并行图片下载线程数（默认 6）。",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data",
        help="输出根目录（默认脚本所在目录下的 data）。",
    )
    parser.add_argument(
        "--quality-grade",
        choices=["research", "needs_id", "casual", "any"],
        default="research",
        help="质量等级过滤（默认 research：社区确认物种，数据最可靠）。",
    )
    parser.add_argument(
        "--save-raw-json",
        action="store_true",
        default=False,
        help="保存原始 API 数据为 JSONL（默认关闭，节省磁盘）。",
    )
    parser.add_argument(
        "--no-skip-places",
        action="store_true",
        default=False,
        help="启用地名查询（默认关闭）。",
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
        "--no-db",
        action="store_true",
        default=False,
        help="禁用数据库存储（默认启用）。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # 组装类群列表：命令行指定优先，否则用内置列表
    if args.taxon_ids:
        taxa = [{"taxon_id": tid, "label_cn": ""} for tid in args.taxon_ids]
    else:
        taxa = DEFAULT_TAXA

    # 质量等级 "any" 表示不过滤
    quality = None if args.quality_grade == "any" else args.quality_grade

    max_label = "不限制" if args.max_per_taxon == 0 else str(args.max_per_taxon)
    print(f"批量下载 {len(taxa)} 个鸟类类群，每个类群最多 {max_label} 条观察记录")
    print(f"输出目录: {args.out_dir}")
    print(f"质量等级: {args.quality_grade}")
    print(f"保存原始数据: {args.save_raw_json}")
    print(f"地名查询: {'开启' if args.no_skip_places else '关闭'}")
    print(f"数据库存储: {'禁用' if args.no_db else '启用'}")
    print("=" * 70)

    # 初始化数据库管理器并创建批次记录
    db_manager: INaturalistDatabaseManager | None = None
    batch_id: int | None = None
    db_dir = args.db_dir or (args.out_dir / "db")
    raw_db = args.raw_db or (db_dir / "raw_data.db")
    main_db = args.main_db or (db_dir / "bird_observations.db")

    if not args.no_db:
        try:
            db_manager = INaturalistDatabaseManager(raw_db, main_db)
            batch_id = db_manager.create_batch_run(
                batch_time=datetime.now().isoformat(timespec="seconds"),
                taxa_requested=len(taxa),
                quality_grade=args.quality_grade,
                photo_size=args.photo_size,
                max_per_taxon=args.max_per_taxon,
                output_dir=str(args.out_dir),
            )
            print(f"已创建批次记录，batch_id={batch_id}")
        except Exception as exc:
            print(f"数据库初始化失败，将跳过数据库存储: {exc}")
            db_manager = None

    results: list[dict[str, Any]] = []
    failed_taxa: list[int] = []

    for index, entry in enumerate(taxa, start=1):
        taxon_id = entry["taxon_id"]
        label = entry.get("label_cn") or ""
        print(
            f"\n[{index}/{len(taxa)}] "
            f"taxon_id={taxon_id} {('[' + label + ']') if label else ''} 开始下载..."
        )
        try:
            # 单类群下载失败不影响其他类群（失败隔离）
            # 0 在命令行中表示不限制，转换为 None
            max_obs = None if args.max_per_taxon == 0 else args.max_per_taxon
            summary = download_taxon_data(
                taxon_id=taxon_id,
                out_dir=args.out_dir,
                photo_size=args.photo_size,
                workers=args.workers,
                per_page=200,
                max_observations=max_obs,
                quality_grade=quality,
                save_raw_json=args.save_raw_json,
                skip_places=not args.no_skip_places,
                db_enabled=not args.no_db,
                raw_db_path=raw_db,
                main_db_path=main_db,
                batch_id=batch_id,
            )
            summary["label_cn"] = label
            results.append(summary)
        except Exception as exc:
            # 记录失败原因并继续下一个类群
            print(f"  类群 taxon_id={taxon_id} 下载失败: {exc}")
            failed_taxa.append(taxon_id)
            results.append(
                {
                    "taxon_id": taxon_id,
                    "label_cn": label,
                    "error": str(exc),
                }
            )

    # 汇总统计
    total_obs = sum(r.get("observation_count", 0) for r in results)
    total_photos = sum(r.get("photo_count", 0) for r in results)
    total_downloaded = sum(r.get("photos_downloaded", 0) for r in results)
    total_failed = sum(r.get("photos_failed", 0) for r in results)

    # 更新数据库中的批次汇总统计
    if db_manager is not None and batch_id is not None:
        try:
            db_manager.update_batch_run(
                batch_id=batch_id,
                taxa_succeeded=len(taxa) - len(failed_taxa),
                total_observations=total_obs,
                total_photos=total_photos,
                total_downloaded=total_downloaded,
                total_failed=total_failed,
            )
        except Exception as exc:
            print(f"更新数据库批次汇总失败: {exc}")

    batch_summary = {
        "batch_time": datetime.now().isoformat(timespec="seconds"),
        "taxa_requested": len(taxa),
        "taxa_succeeded": len(taxa) - len(failed_taxa),
        "taxa_failed": failed_taxa,
        "max_per_taxon": args.max_per_taxon,
        "quality_grade": args.quality_grade,
        "photo_size": args.photo_size,
        "total_observations": total_obs,
        "total_photos": total_photos,
        "total_photos_downloaded": total_downloaded,
        "total_photos_failed": total_failed,
        "output_dir": str(args.out_dir),
        "taxa_results": results,
    }
    summary_path = args.out_dir / "batch_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(batch_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 70)
    print("批量下载完成，汇总：")
    print(f"  类群成功/失败: {len(taxa) - len(failed_taxa)}/{len(failed_taxa)}")
    print(f"  观察记录总数: {total_obs}")
    print(f"  图片总数: {total_photos}（下载 {total_downloaded}，失败 {total_failed}）")
    print(f"  汇总文件: {summary_path}")

    if db_manager is not None:
        db_manager.close()

    return 0 if not failed_taxa else 1


if __name__ == "__main__":
    sys.exit(main())
