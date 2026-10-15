#!/usr/bin/env python3
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-08
"""iNaturalist 鸟类下载项目统一入口。

提供子命令：
    batch      批量下载多个鸟类类群
    download   单个类群下载
    migrate    将已有 CSV/JSON 历史数据迁移到数据库
    init-db    初始化 SQLite 数据库
    stats      查看数据库统计

用法示例：
    python main.py batch --max-per-taxon 200
    python main.py download --taxon-id 20967 --max-observations 10
    python main.py migrate
    python main.py init-db
    python main.py stats
    python main.py panel
    python main.py gui
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


import argparse
import sys
from pathlib import Path
from typing import Any

from download_inaturalist import download_taxon_data
from inaturalist_db_initializer import init_all_databases
from inaturalist_db_manager import INaturalistDatabaseManager

# ============================================================
# 数字水印与版权信息（程序权属标识，受保护内容，请勿移除）
# ============================================================
APP_VERSION = "1.5.1"
APP_AUTHOR = "天咏"
APP_AUTHOR_SIGNATURE = "明月照今日，风摇过两川。"
# 控制台输出的版权行不含 © 字符（Windows 控制台默认 GBK 代码页无法
# 编码该字符）；PE 版本资源中的版权行由版本信息文件独立定义（含 ©）。
APP_COPYRIGHT = "2026 天咏 版权所有（明月照今日，风摇过两川。）"
# 以下两项为打包占位符：打包准备脚本构建时自动替换为源码指纹与构建时间；
# 源码直接运行时保留占位说明文本，不影响任何功能。
APP_BUILD_FINGERPRINT = "4F182B222DA55343"
APP_BUILD_TIME = "2026-10-15 22:10:00"
# 数字水印标识：TYWM（天咏水印）+ 产品代号 + 版本 + 源码指纹
APP_WATERMARK_ID = f"TYWM-BIRD-v{APP_VERSION}-{APP_BUILD_FINGERPRINT}"


def watermark_banner() -> str:
    """组装程序权属水印文本（copyright 子命令与版本输出共用）。"""
    lines = [
        "程序名称：鸟类（iNaturalist 鸟类数据采集与图像下载系统）",
        f"程序版本：v{APP_VERSION}",
        f"作者署名：{APP_AUTHOR}（{APP_AUTHOR_SIGNATURE}）",
        f"版权声明：{APP_COPYRIGHT}",
        f"数字水印：{APP_WATERMARK_ID}",
        f"构建指纹：{APP_BUILD_FINGERPRINT}",
        f"构建时间：{APP_BUILD_TIME}",
        "权属说明：本程序及其安装包内嵌数字水印与源码构建指纹，",
        "        用以标识著作权归属；未经授权不得去除或篡改。",
    ]
    return "\n".join(lines)


def _resolve_db_paths(args: argparse.Namespace, out_dir: Path) -> tuple[Path, Path]:
    """根据命令行参数解析数据库路径。"""
    db_dir = args.db_dir or (out_dir / "db")
    raw_db = args.raw_db or (db_dir / "raw_data.db")
    main_db = args.main_db or (db_dir / "bird_observations.db")
    return Path(raw_db), Path(main_db)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """添加通用参数：输出目录与数据库路径。"""
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_frozen_base(__file__) / "data",
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


def cmd_batch(args: argparse.Namespace) -> int:
    """批量下载子命令。"""
    from batch_download_birds import main as batch_main

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


def cmd_download(args: argparse.Namespace) -> int:
    """单类群下载子命令。"""
    summary = download_taxon_data(
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
        raw_db_path=_resolve_db_paths(args, args.out_dir)[0],
        main_db_path=_resolve_db_paths(args, args.out_dir)[1],
    )
    return 0 if summary["photos_failed"] == 0 else 1


def cmd_migrate(args: argparse.Namespace) -> int:
    """历史数据迁移子命令。"""
    from inaturalist_db_migrator import main as migrator_main

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


def cmd_init_db(args: argparse.Namespace) -> int:
    """初始化数据库子命令。"""
    raw_db, main_db = _resolve_db_paths(args, args.out_dir)
    init_all_databases(raw_db, main_db)
    print(f"数据库初始化完成：")
    print(f"  原始库: {raw_db}")
    print(f"  主库: {main_db}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """查看数据库统计子命令。"""
    raw_db, main_db = _resolve_db_paths(args, args.out_dir)
    if not main_db.exists():
        print(f"主数据库不存在: {main_db}")
        return 1
    manager = INaturalistDatabaseManager(raw_db, main_db)
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


def cmd_panel(args: argparse.Namespace) -> int:
    """启动数据库交互面板子命令（浏览器访问）。"""
    # 面板仅依赖主数据库，路径延迟导入以加快其他子命令的启动速度
    from bird_db_panel import serve

    _, main_db = _resolve_db_paths(args, args.out_dir)
    serve(main_db=main_db, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """启动 Windows 桌面控制台子命令（Tkinter 弹窗界面）。"""
    from bird_crawler_gui import run as gui_run

    return gui_run()


def cmd_drop(args: argparse.Namespace) -> int:
    """拖入自动处理子命令（txt/csv 清单爬取；图片/文件夹按种类指纹编码入库）。"""
    from drop_crawl_interface import run_cli

    # db-dir 未指定时按 out-dir/db 兜底解析，避免空值传给接口
    if getattr(args, "db_dir", None) is None:
        args.db_dir = Path(args.out_dir) / "db"
    return run_cli(args)


def cmd_dataset(args: argparse.Namespace) -> int:
    """训练数据集导出子命令（按观察划分防泄漏，硬链接零拷贝）。"""
    from dataset_exporter import run as dataset_run

    _, main_db = _resolve_db_paths(args, args.out_dir)
    # 映射为导出模块的参数命名空间
    export_args = argparse.Namespace(
        out=args.out,
        taxon_ids=getattr(args, "taxon_ids", ""),
        data_dir=args.out_dir,
        main_db=main_db,
        splits=args.splits,
        seed=args.seed,
        label_scheme=args.label_scheme,
        min_per_class=args.min_per_class,
        max_per_class=args.max_per_class,
        min_width=args.min_width,
        min_height=args.min_height,
        license=args.license,
        link=args.link,
        no_verify=args.no_verify,
    )
    return dataset_run(export_args)


def cmd_copyright(args: argparse.Namespace) -> int:
    """版权与数字水印查询子命令：输出程序权属标识信息。"""
    print(watermark_banner())
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器。"""
    parser = argparse.ArgumentParser(
        description="iNaturalist 鸟类下载项目统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # 顶层 --version：输出版本、版权与数字水印（GBK 安全文本，控制台直接输出）
    parser.add_argument(
        "--version",
        action="version",
        version=f"鸟类 v{APP_VERSION}（2026 {APP_AUTHOR} 版权所有） 数字水印 {APP_WATERMARK_ID}",
        help="输出版本、版权与数字水印信息",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="子命令")

    # batch 子命令
    batch_parser = subparsers.add_parser("batch", help="批量下载多个鸟类类群")
    _add_common_args(batch_parser)
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
    _add_common_args(download_parser)
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
    _add_common_args(migrate_parser)
    migrate_parser.add_argument(
        "--batch-uuid",
        type=str,
        default=None,
        help="迁移批次 UUID（默认自动生成）。",
    )

    # init-db 子命令
    init_parser = subparsers.add_parser("init-db", help="初始化数据库")
    _add_common_args(init_parser)

    # stats 子命令
    stats_parser = subparsers.add_parser("stats", help="查看数据库统计")
    _add_common_args(stats_parser)

    # panel 子命令
    panel_parser = subparsers.add_parser("panel", help="启动数据库交互面板（浏览器访问）")
    _add_common_args(panel_parser)
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
                             default=_frozen_base(__file__) / "data" / "drop_inbox",
                             help="投放目录（默认 data/drop_inbox）")
    drop_parser.add_argument("--out-dir", type=Path,
                             default=_frozen_base(__file__) / "data",
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

    # dataset 子命令（训练数据集导出：按观察划分、manifest 清单、硬链接零拷贝）
    dataset_parser = subparsers.add_parser(
        "dataset",
        help="导出标准训练数据集（按观察划分防泄漏、manifest 清单、硬链接零拷贝）")
    _add_common_args(dataset_parser)
    dataset_parser.add_argument(
        "--out", type=Path, required=True,
        help="数据集导出目标目录")
    dataset_parser.add_argument(
        "--taxon-ids", type=str, default="",
        help="仅导出指定类群（逗号分隔的类群 ID，空=全部；"
             "GUI「训练数据集」页签按勾选类群导出时使用）")
    dataset_parser.add_argument(
        "--splits", type=str, default="8:1:1",
        help="train:val:test 比例（默认 8:1:1，按观察划分）")
    dataset_parser.add_argument(
        "--seed", type=int, default=42,
        help="划分随机种子（保证可复现，默认 42）")
    dataset_parser.add_argument(
        "--label-scheme", choices=["id", "sci", "cn"], default="id",
        help="类别目录命名（id=类群ID / sci=学名 / cn=中文名，默认 id）")
    dataset_parser.add_argument(
        "--min-per-class", type=int, default=10,
        help="每类最少照片数，少于此数的类跳过（0=不过滤）")
    dataset_parser.add_argument(
        "--max-per-class", type=int, default=0,
        help="每类最多照片数，超出按观察随机舍弃（0=不限）")
    dataset_parser.add_argument(
        "--min-width", type=int, default=0,
        help="最小图片宽度像素（0=不过滤）")
    dataset_parser.add_argument(
        "--min-height", type=int, default=0,
        help="最小图片高度像素（0=不过滤）")
    dataset_parser.add_argument(
        "--license", type=str, default="",
        help="许可证白名单（逗号分隔，如 cc-by,cc-by-nc；空=不过滤）")
    dataset_parser.add_argument(
        "--link", choices=["hard", "copy"], default="hard",
        help="导出方式（hard=硬链接零拷贝，跨盘自动回退复制）")
    dataset_parser.add_argument(
        "--no-verify", action="store_true",
        help="跳过图片完整性校验（PIL 未安装时自动跳过）")

    # copyright 子命令（版权与数字水印查询：输出权属标识信息）
    subparsers.add_parser("copyright", help="查看版权与数字水印信息")

    return parser


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    return build_parser().parse_args()


def main() -> int:
    """项目统一入口主函数。"""
    try:
        # 行缓冲即时输出；errors=replace 保证极端代码页下不可编码字符不致崩溃
        sys.stdout.reconfigure(line_buffering=True, errors="replace")
    except Exception:
        pass

    # 不带任何参数时：源码运行显示帮助；打包为 exe 后直接启动桌面控制台
    if len(sys.argv) == 1:
        if getattr(sys, "frozen", False):
            # 冻结环境（双击 exe）：默认打开桌面控制台界面
            sys.argv.append("gui")
        else:
            parser = build_parser()
            parser.print_help()
            return 0

    args = parse_args()
    command_map: dict[str, Any] = {
        "batch": cmd_batch,
        "download": cmd_download,
        "migrate": cmd_migrate,
        "init-db": cmd_init_db,
        "stats": cmd_stats,
        "panel": cmd_panel,
        "gui": cmd_gui,
        "drop": cmd_drop,
        "dataset": cmd_dataset,
        "copyright": cmd_copyright,
    }
    handler = command_map[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
