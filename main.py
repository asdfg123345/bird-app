#!/usr/bin/env python3
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-08

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from download_inaturalist import _v7ea57622
from inaturalist_db_initializer import _v31e07de3
from inaturalist_db_manager import _v8b9d65af


def _v76f390a5(args: argparse.Namespace, out_dir: Path) -> tuple[Path, Path]:
    db_dir = args.db_dir or (out_dir / "db")
    raw_db = args.raw_db or (db_dir / "raw_data.db")
    main_db = args.main_db or (db_dir / "bird_observations.db")
    return Path(raw_db), Path(main_db)


def _vdf7e8271(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data",
        help="输出根目录（默认脚本所在目录下的 data）。",
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


def _v9be8290d(args: argparse.Namespace) -> int:
    from batch_download_birds import _v9a12dab4 as batch_main

    # 构造 sys.argv 以复用 batch_download_birds 的 argparse
    sys.argv = ["batch_download_birds.py"]
    if args.taxon_ids:
        sys.argv.extend(["--taxon-ids"] + [str(t) for t in args.taxon_ids])
    sys.argv.extend([
        "--out-dir", str(args.out_dir),
        "--max-per-taxon", str(args.max_per_taxon),
        "--workers", str(args.workers),
        "--per-page", str(args.per_page),
        "--photo-size", args.photo_size,
        "--quality-grade", args.quality_grade,
    ])
    if args.save_raw_json:
        sys.argv.append("--save-raw-json")
    if args.no_skip_places:
        sys.argv.append("--no-skip-places")
    if args.db_dir:
        sys.argv.extend(["--db-dir", str(args.db_dir)])
    if args.raw_db:
        sys.argv.extend(["--raw-db", str(args.raw_db)])
    if args.main_db:
        sys.argv.extend(["--main-db", str(args.main_db)])
    if args.no_db:
        sys.argv.append("--no-db")
    return batch_main()


def _v091e902e(args: argparse.Namespace) -> int:
    summary = _v7ea57622(
        taxon_id=args.taxon_id,
        out_dir=args.out_dir,
        photo_size=args.photo_size,
        workers=args.workers,
        per_page=args.per_page,
        max_observations=args.max_observations,
        quality_grade=None if args.quality_grade == "any" else args.quality_grade,
        save_raw_json=args.save_raw_json,
        skip_places=not args.no_skip_places,
        db_enabled=not args.no_db,
        raw_db_path=_v76f390a5(args, args.out_dir)[0],
        main_db_path=_v76f390a5(args, args.out_dir)[1],
    )
    return 0 if summary["photos_failed"] == 0 else 1


def _v2cb152ff(args: argparse.Namespace) -> int:
    from inaturalist_db_migrator import _v9a12dab4 as migrator_main

    sys.argv = ["inaturalist_db_migrator.py"]
    sys.argv.extend(["--out-dir", str(args.out_dir)])
    if args.db_dir:
        sys.argv.extend(["--db-dir", str(args.db_dir)])
    if args.raw_db:
        sys.argv.extend(["--raw-db", str(args.raw_db)])
    if args.main_db:
        sys.argv.extend(["--main-db", str(args.main_db)])
    if args.batch_uuid:
        sys.argv.extend(["--batch-uuid", args.batch_uuid])
    return migrator_main()


def _v8dabf474(args: argparse.Namespace) -> int:
    raw_db, main_db = _v76f390a5(args, args.out_dir)
    _v31e07de3(raw_db, main_db)
    print(f"数据库初始化完成：")
    print(f"  原始库: {raw_db}")
    print(f"  主库: {main_db}")
    return 0


def _vdb0a9d2b(args: argparse.Namespace) -> int:
    raw_db, main_db = _v76f390a5(args, args.out_dir)
    if not main_db.exists():
        print(f"主数据库不存在: {main_db}")
        return 1
    manager = _v8b9d65af(raw_db, main_db)
    print("=== 数据库统计 ===")
    print(f"观察记录总数: {manager.get_observation_count()}")
    print(f"照片总数: {manager.get_photo_count()}")
    print("\n按类群统计:")
    for row in manager.get_taxon_stats():
        print(
            f"  taxon_id={row['taxon_id']} "
            f"{row['scientific_name']} ({row['common_name']}): "
            f"观察 {row['observation_count']} 条，照片 {row['photo_count']} 张"
        )
    print("\n批次记录:")
    for row in manager.get_batch_summary():
        print(
            f"  batch_id={row['id']} time={row['batch_time']} "
            f"taxa={row['taxa_succeeded']}/{row['taxa_requested']} "
            f"obs={row['total_observations']} photos={row['total_photos']}"
        )
    manager.close()
    return 0


def _v65395e70(args: argparse.Namespace) -> int:
    # 面板仅依赖主数据库，路径延迟导入以加快其他子命令的启动速度
    from bird_db_panel import _v6308aa23

    _, main_db = _v76f390a5(args, args.out_dir)
    _v6308aa23(main_db=main_db, port=args.port, open_browser=not args.no_browser)
    return 0


def _v347f414b(args: argparse.Namespace) -> int:
    from bird_crawler_gui import _v7dab2a83 as gui_run

    return gui_run()


def _v7f9ebc7a(args: argparse.Namespace) -> int:
    from drop_crawl_interface import _v16f83c08

    # db-dir 未指定时按 out-dir/db 兜底解析，避免空值传给接口
    if getattr(args, "db_dir", None) is None:
        args.db_dir = Path(args.out_dir) / "db"
    return _v16f83c08(args)


def _v4e1ce92b() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="iNaturalist 鸟类下载项目统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="子命令")

    # batch 子命令
    batch_parser = subparsers.add_parser("batch", help="批量下载多个鸟类类群")
    _vdf7e8271(batch_parser)
    batch_parser.add_argument(
        "--taxon-ids",
        type=int,
        nargs="+",
        default=None,
        help="指定类群 ID 列表（默认使用内置 15 个鸟类科）。",
    )
    batch_parser.add_argument(
        "--max-per-taxon",
        type=int,
        default=200,
        help="每个类群最多下载多少条观察记录（0 表示不限制）。",
    )
    batch_parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="照片下载并发线程数。",
    )
    batch_parser.add_argument(
        "--per-page",
        type=int,
        default=200,
        help="每页请求观察记录数。",
    )
    batch_parser.add_argument(
        "--photo-size",
        type=str,
        default="large",
        help="下载图片尺寸（small/medium/large/original）。",
    )
    batch_parser.add_argument(
        "--quality-grade",
        type=str,
        default="research",
        help="观察记录质量等级（research/needs_id/casual/any）。",
    )
    batch_parser.add_argument(
        "--save-raw-json",
        action="store_true",
        default=False,
        help="保存每页原始 API 数据为 JSONL。",
    )
    batch_parser.add_argument(
        "--no-skip-places",
        action="store_true",
        default=False,
        help="启用地名查询（默认关闭）。",
    )

    # download 子命令
    download_parser = subparsers.add_parser("download", help="单个类群下载")
    _vdf7e8271(download_parser)
    download_parser.add_argument(
        "--taxon-id",
        type=int,
        required=True,
        help="iNaturalist 类群 ID。",
    )
    download_parser.add_argument(
        "--max-observations",
        type=int,
        default=None,
        help="最多下载多少条观察记录。",
    )
    download_parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="照片下载并发线程数。",
    )
    download_parser.add_argument(
        "--per-page",
        type=int,
        default=200,
        help="每页请求观察记录数。",
    )
    download_parser.add_argument(
        "--photo-size",
        type=str,
        default="large",
        help="下载图片尺寸。",
    )
    download_parser.add_argument(
        "--quality-grade",
        type=str,
        default="research",
        help="观察记录质量等级。",
    )
    download_parser.add_argument(
        "--save-raw-json",
        action="store_true",
        default=False,
        help="保存每页原始 API 数据为 JSONL。",
    )
    download_parser.add_argument(
        "--no-skip-places",
        action="store_true",
        default=False,
        help="启用地名查询（默认关闭）。",
    )

    # migrate 子命令
    migrate_parser = subparsers.add_parser("migrate", help="迁移已有 CSV/JSON 到数据库")
    _vdf7e8271(migrate_parser)
    migrate_parser.add_argument(
        "--batch-uuid",
        type=str,
        default=None,
        help="迁移批次 UUID（默认自动生成）。",
    )

    # init-db 子命令
    init_parser = subparsers.add_parser("init-db", help="初始化数据库")
    _vdf7e8271(init_parser)

    # stats 子命令
    stats_parser = subparsers.add_parser("stats", help="查看数据库统计")
    _vdf7e8271(stats_parser)

    # panel 子命令
    panel_parser = subparsers.add_parser("panel", help="启动数据库交互面板（浏览器访问）")
    _vdf7e8271(panel_parser)
    panel_parser.add_argument("--port", type=int, default=8765, help="面板服务端口（默认 8765）")
    panel_parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")

    # gui 子命令
    subparsers.add_parser("gui", help="启动 Windows 桌面控制台（弹窗界面）")

    # drop 子命令（拖入文件自动爬取接口：文本清单爬取 + 图片按种类指纹编码入库）
    drop_parser = subparsers.add_parser(
        "drop", help="拖入自动处理：txt/csv 清单爬取；图片/文件夹按种类指纹编码入库")
    drop_parser.add_argument("--file", nargs="*", default=[],
                             help="要处理的文件/文件夹路径，可一次传多个")
    drop_parser.add_argument("--watch", action="store_true", help="常驻监视投放目录")
    drop_parser.add_argument("--inbox", type=Path,
                             default=Path(__file__).resolve().parent / "data" / "drop_inbox",
                             help="投放目录（默认 data/drop_inbox）")
    drop_parser.add_argument("--out-dir", type=Path,
                             default=Path(__file__).resolve().parent / "data",
                             help="输出根目录（默认脚本所在目录下的 data）")
    drop_parser.add_argument("--db-dir", type=Path, default=None,
                             help="数据库目录（默认 out-dir/db）")
    drop_parser.add_argument("--taxon", default="",
                             help="统一归属类群（物种名/中文名/ID），覆盖文件夹名判定")
    drop_parser.add_argument("--max-per-taxon", type=int, default=50, help="每个类群爬取上限")
    drop_parser.add_argument("--workers", type=int, default=6, help="图片下载线程数")
    drop_parser.add_argument("--photo-size", choices=["original", "large", "medium"],
                             default="large")
    drop_parser.add_argument("--quality-grade",
                             choices=["research", "needs_id", "casual", "any"],
                             default="research")

    return parser


def _v772a86c3() -> argparse.Namespace:
    return _v4e1ce92b().parse_args()


def _v9a12dab4() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # 不带任何参数时直接显示帮助，避免报错退出
    if len(sys.argv) == 1:
        parser = _v4e1ce92b()
        parser.print_help()
        return 0

    args = _v772a86c3()
    command_map: dict[str, Any] = {
        "batch": _v9be8290d,
        "download": _v091e902e,
        "migrate": _v2cb152ff,
        "init-db": _v8dabf474,
        "stats": _vdb0a9d2b,
        "panel": _v65395e70,
        "gui": _v347f414b,
        "drop": _v7f9ebc7a,
    }
    handler = command_map[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(_v9a12dab4())
