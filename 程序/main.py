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

from download_inaturalist import _v4ec0b3e2
from inaturalist_db_initializer import _ve49f7201
from inaturalist_db_manager import _vc1f32a11

# ============================================================
# 数字水印与版权信息（程序权属标识，受保护内容，请勿移除）
# ============================================================
_v3698f533 = "1.5.1"
_va54a0eaa = "天咏"
_vc78006c9 = "明月照今日，风摇过两川。"
# 控制台输出的版权行不含 © 字符（Windows 控制台默认 GBK 代码页无法
# 编码该字符）；PE 版本资源中的版权行由版本信息文件独立定义（含 ©）。
_vc08e4dd5 = "2026 天咏 版权所有（明月照今日，风摇过两川。）"
# 以下两项为打包占位符：打包准备脚本构建时自动替换为源码指纹与构建时间；
# 源码直接运行时保留占位说明文本，不影响任何功能。
_vfd80eb11 = "f370869eed682eac"
_v234b6456 = "2026-10-16 11:06:49"
# 数字水印标识：TYWM（天咏水印）+ 产品代号 + 版本 + 源码指纹
_vd0d9683b = f"TYWM-BIRD-v{_v3698f533}-{_vfd80eb11}"


def _v40fcadd3() -> str:
    lines = [
        "程序名称：鸟类（iNaturalist 鸟类数据采集与图像下载系统）",
        f"程序版本：v{_v3698f533}",
        f"作者署名：{_va54a0eaa}（{_vc78006c9}）",
        f"版权声明：{_vc08e4dd5}",
        f"数字水印：{_vd0d9683b}",
        f"构建指纹：{_vfd80eb11}",
        f"构建时间：{_v234b6456}",
        "权属说明：本程序及其安装包内嵌数字水印与源码构建指纹，",
        "        用以标识著作权归属；未经授权不得去除或篡改。",
    ]
    return "\n".join(lines)


def _vbbc13256(args: argparse.Namespace, out_dir: Path) -> tuple[Path, Path]:
    db_dir = args.db_dir or (out_dir / "db")
    raw_db = args.raw_db or (db_dir / "raw_data.db")
    main_db = args.main_db or (db_dir / "bird_observations.db")
    return Path(raw_db), Path(main_db)


def _vdee07610(parser: argparse.ArgumentParser) -> None:
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


def _vc7b4cd0f(args: argparse.Namespace) -> int:
    from batch_download_birds import _v6f5fe057 as batch_main

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


def _vf97c83f4(args: argparse.Namespace) -> int:
    summary = _v4ec0b3e2(
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
        raw_db_path=_vbbc13256(args, args.out_dir)[0],
        main_db_path=_vbbc13256(args, args.out_dir)[1],
    )
    return 0 if summary["photos_failed"] == 0 else 1


def _vcdd72447(args: argparse.Namespace) -> int:
    from inaturalist_db_migrator import _v6f5fe057 as migrator_main

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


def _v9150cd3a(args: argparse.Namespace) -> int:
    raw_db, main_db = _vbbc13256(args, args.out_dir)
    _ve49f7201(raw_db, main_db)
    print(f"数据库初始化完成：")
    print(f"  原始库: {raw_db}")
    print(f"  主库: {main_db}")
    return 0


def _v9099e1af(args: argparse.Namespace) -> int:
    raw_db, main_db = _vbbc13256(args, args.out_dir)
    if not main_db.exists():
        print(f"主数据库不存在: {main_db}")
        return 1
    manager = _vc1f32a11(raw_db, main_db)
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


def _v39d8b066(args: argparse.Namespace) -> int:
    # 面板仅依赖主数据库，路径延迟导入以加快其他子命令的启动速度
    from bird_db_panel import _v52e537e0

    _, main_db = _vbbc13256(args, args.out_dir)
    _v52e537e0(main_db=main_db, port=args.port, open_browser=not args.no_browser)
    return 0


def _v4269340b(args: argparse.Namespace) -> int:
    from bird_crawler_gui import run as gui_run

    return gui_run()


def _v73707c7f(args: argparse.Namespace) -> int:
    from drop_crawl_interface import _va0fedbb1

    # db-dir 未指定时按 out-dir/db 兜底解析，避免空值传给接口
    if getattr(args, "db_dir", None) is None:
        args.db_dir = Path(args.out_dir) / "db"
    return _va0fedbb1(args)


def _v108cad6d(args: argparse.Namespace) -> int:
    from dataset_exporter import run as dataset_run

    _, main_db = _vbbc13256(args, args.out_dir)
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


def _v59966678(args: argparse.Namespace) -> int:
    print(_v40fcadd3())
    return 0


def _veca4458f(args: argparse.Namespace) -> int:
    from db_health_check import _v6f5fe057 as health_main

    raw_db, main_db = _vbbc13256(args, args.out_dir)
    argv: list[str] = ["--data-dir", str(args.out_dir),
                       "--db", str(main_db),
                       "--raw-db", str(raw_db)]
    if args.apply:
        argv.append("--apply")
    return health_main(argv)


def _v536f243a() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="iNaturalist 鸟类下载项目统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # 顶层 --version：输出版本、版权与数字水印（GBK 安全文本，控制台直接输出）
    parser.add_argument(
        "--version",
        action="version",
        version=f"鸟类 v{_v3698f533}（2026 {_va54a0eaa} 版权所有） 数字水印 {_vd0d9683b}",
        help="输出版本、版权与数字水印信息",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="子命令")

    # batch 子命令
    batch_parser = subparsers.add_parser("batch", help="批量下载多个鸟类类群")
    _vdee07610(batch_parser)
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
    _vdee07610(download_parser)
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
    _vdee07610(migrate_parser)
    migrate_parser.add_argument(
        "--batch-uuid",
        type=str,
        default=None,
        help="迁移批次 UUID（默认自动生成）。",
    )

    # init-db 子命令
    init_parser = subparsers.add_parser("init-db", help="初始化数据库")
    _vdee07610(init_parser)

    # stats 子命令
    stats_parser = subparsers.add_parser("stats", help="查看数据库统计")
    _vdee07610(stats_parser)

    # panel 子命令
    panel_parser = subparsers.add_parser("panel", help="启动数据库交互面板（浏览器访问）")
    _vdee07610(panel_parser)
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

    # dataset 子命令（训练数据集导出：按观察划分、manifest 清单、硬链接零拷贝）
    dataset_parser = subparsers.add_parser(
        "dataset",
        help="导出标准训练数据集（按观察划分防泄漏、manifest 清单、硬链接零拷贝）")
    _vdee07610(dataset_parser)
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

    # health 子命令（数据库健康检查：残留/孤儿/空批次/重复/歧义/两库一致性）
    health_parser = subparsers.add_parser(
        "health", help="数据库健康检查（残留/孤儿记录/空批次/重复/同名歧义/两库一致性）")
    _vdee07610(health_parser)
    health_parser.add_argument(
        "--apply", action="store_true",
        help="执行安全修复（默认只检查；修复前自动整库备份）")

    return parser


def parse_args() -> argparse.Namespace:
    return _v536f243a().parse_args()


def _v6f5fe057() -> int:
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
            parser = _v536f243a()
            parser.print_help()
            return 0

    args = parse_args()
    command_map: dict[str, Any] = {
        "batch": _vc7b4cd0f,
        "download": _vf97c83f4,
        "migrate": _vcdd72447,
        "init-db": _v9150cd3a,
        "stats": _v9099e1af,
        "panel": _v39d8b066,
        "gui": _v4269340b,
        "drop": _v73707c7f,
        "dataset": _v108cad6d,
        "copyright": _v59966678,
        "health": _veca4458f,
    }
    handler = command_map[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(_v6f5fe057())
