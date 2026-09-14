#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# 路径相对脚本目录解析，避免硬编码
_vd4ca91d7 = Path(__file__).resolve().parent
_va1aa2a9b = _vd4ca91d7 / "data"
_va6de3477 = _va1aa2a9b / "db"
_v0fbbe809 = _va1aa2a9b / "drop_inbox"
_v428ddb6f = "processed_已处理"

# 支持的拖入文本文件类型
_v5e94797e = {".txt", ".csv", ".list"}
# 支持的拖入图片类型
_v170a9bb4 = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# 常驻监视模式中文件夹处理完成后的标记文件名
_v52c8c893 = "_已导入_指纹编码完成.txt"


def _v726a4423(token: str, timeout: int = 15) -> tuple[int | None, str]:
    token = token.strip()
    if not token:
        return None, "空行"
    if token.isdigit():
        return int(token), "直接使用 ID"
    # 名称解析：调用官方 taxa/autocomplete 接口，取第一个结果
    api = ("https://api.inaturalist.org/v1/taxa/autocomplete?q="
           + urllib.parse.quote(token))
    try:
        with urllib.request.urlopen(api, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results") or []
        if not results:
            return None, f"未找到与「{token}」匹配的类群"
        first = results[0]
        return int(first["id"]), f"「{token}」→ {first.get('name')} (rank={first.get('rank')})"
    except Exception as exc:  # 网络异常时不中断整体流程
        return None, f"解析「{token}」失败: {exc}"


def _v5d3d437b(token: str, timeout: int = 15) -> tuple[int | None, str, str, str]:
    token = token.strip()
    if not token:
        return None, "", "", "空行"
    if token.isdigit():
        api = f"https://api.inaturalist.org/v1/taxa/{token}"
    else:
        api = ("https://api.inaturalist.org/v1/taxa/autocomplete?q="
               + urllib.parse.quote(token))
    try:
        with urllib.request.urlopen(api, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results") or []
        if not results:
            return None, "", "", f"未找到与「{token}」匹配的类群"
        first = results[0]
        taxon_id = int(first["id"])
        sci = first.get("name") or ""
        common = first.get("preferred_common_name") or ""
        rank = first.get("rank") or ""
        note = f"「{token}」→ {sci} / {common} (id={taxon_id}, rank={rank})"
        return taxon_id, sci, common, note
    except Exception as exc:  # 网络异常时不中断整体流程
        return None, "", "", f"解析「{token}」失败: {exc}"


def _v6f4e2283(path: Path) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


# ================= 图片本地入库：依次指纹编码 =================

def _v8fc0948f(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _va0464332(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as im:
            return im.width, im.height
    except Exception:
        return None, None


def _v64cf522e(kind_name: str, images: list[Path],
                            manager, batch_id: int, out_dir: Path,
                            seen_in_session: set[str],
                            report_lines: list[str]) -> tuple[int, int, int]:
    taxon_id, sci, common, note = _v5d3d437b(kind_name)
    report_lines.append(f"  {note}")
    if taxon_id is None:
        return 0, 0, 0

    # 目录命名与主爬虫完全一致，保证与爬取数据合并存储
    from download_inaturalist import _v58595b53
    folder_name = f"taxon_{taxon_id}_{_v58595b53(sci)}_{_v58595b53(common)}"
    photo_dir = out_dir / folder_name / "photos"
    photo_dir.mkdir(parents=True, exist_ok=True)

    obs_id = manager.upsert_local_observation(
        taxon_id=taxon_id, scientific_name=sci, common_name=common,
        source_name=kind_name, note=f"本地导入来源: {kind_name}")

    new = dup = fail = 0
    for position, img in enumerate(images, 1):
        sha = _v8fc0948f(img)
        seen_in_session.add(sha)
        if manager.has_photo_fingerprint(sha):
            dup += 1
            report_lines.append(
                f"    [{position}] {img.name} → 指纹 {sha[:16]}… 内容重复，跳过")
            continue
        photo_id = manager.next_local_photo_id()
        filename = f"obs_{obs_id}_photo_{photo_id}{img.suffix.lower()}"
        dest = photo_dir / filename
        try:
            shutil.copy2(img, dest)
        except OSError as exc:
            fail += 1
            report_lines.append(f"    [{position}] {img.name} → 复制失败: {exc}")
            continue
        width, height = _va0464332(dest)
        status = manager.upsert_local_photo(
            photo_id=photo_id, observation_id=obs_id, content_sha=sha,
            original_name=img.name,
            dest_relative=str(Path("photos") / filename),
            file_size=dest.stat().st_size,
            width=width, height=height, position=position)
        if status == "inserted":
            new += 1
            report_lines.append(
                f"    [{position}] {img.name} → {filename} "
                f"指纹 {sha[:16]}… 已编码入库")
        else:
            dup += 1
            dest.unlink(missing_ok=True)
            report_lines.append(
                f"    [{position}] {img.name} → 指纹重复，跳过")

    manager.refresh_local_observation_stats(obs_id)
    manager.record_batch_taxon(
        batch_id=batch_id, taxon_id=taxon_id,
        observation_count=1, photo_count=len(images),
        photos_downloaded=new, photos_failed=fail)
    manager.link_batch_observation(batch_id=batch_id, observation_id=obs_id)
    report_lines.append(
        f"  小计 {folder_name}: 新入库 {new}, 重复 {dup}, 失败 {fail}, "
        f"本地观察ID {obs_id}")
    return new, dup, fail


def _v70fe9a45(path: Path, out_dir: Path, db_dir: Path,
                        taxon_token: str = "") -> str:
    from inaturalist_db_manager import _v8b9d65af

    report_lines = [
        "拖入文件夹导入报告（按种类指纹编码）",
        f"源文件夹: {path}",
        f"处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "-" * 56,
    ]

    # 收集图片：子文件夹各成一类；根层图片以根文件夹名为一类
    kinds: list[tuple[str, list[Path]]] = []
    if taxon_token:
        all_imgs = sorted(p for p in path.rglob("*")
                          if p.is_file() and p.suffix.lower() in _v170a9bb4)
        if all_imgs:
            kinds.append((taxon_token, all_imgs))
    else:
        for sub in sorted(d for d in path.iterdir() if d.is_dir()):
            imgs = sorted(p for p in sub.rglob("*")
                          if p.is_file() and p.suffix.lower() in _v170a9bb4)
            if imgs:
                kinds.append((sub.name, imgs))
        root_imgs = sorted(p for p in path.iterdir()
                           if p.is_file() and p.suffix.lower() in _v170a9bb4)
        if root_imgs:
            kinds.append((path.name, root_imgs))

    if not kinds:
        report_lines.append("未发现图片文件（支持 jpg/jpeg/png/webp/gif）。")
        report_lines.append("用法：文件夹名或其子文件夹名应为物种名/中文名/类群ID，")
        report_lines.append("      或在调用时用 --taxon 统一指定类群。")
        return "\n".join(report_lines)

    manager = _v8b9d65af(db_dir / "raw_data.db",
                                         db_dir / "bird_observations.db")
    batch_id = manager.create_batch_run(
        quality_grade="local_import", photo_size="original",
        output_dir=str(out_dir))
    try:
        seen: set[str] = set()
        total_new = total_dup = total_fail = 0
        for k, (kind_name, images) in enumerate(kinds, 1):
            report_lines.append(f"[{k}/{len(kinds)}] 种类「{kind_name}」共 {len(images)} 张")
            new, dup, fail = _v64cf522e(
                kind_name, images, manager, batch_id, out_dir, seen, report_lines)
            total_new += new
            total_dup += dup
            total_fail += fail
        manager.update_batch_run(
            batch_id, taxa_succeeded=len(kinds),
            total_photos=total_new + total_dup,
            total_downloaded=total_new, total_failed=total_fail)
    finally:
        manager.close()

    report_lines.append("-" * 56)
    report_lines.append(
        f"导入完成: 新入库 {total_new} 张, 重复跳过 {total_dup} 张, 失败 {total_fail} 张。")
    report_lines.append("源文件夹保持原位未动；照片已按种类存入对应 taxon_ 文件夹。")
    return "\n".join(report_lines)


def _v3c6be5a8(paths: list[Path], out_dir: Path, db_dir: Path,
                        taxon_token: str = "") -> str:
    from inaturalist_db_manager import _v8b9d65af

    report_lines = [
        "拖入图片导入报告（按种类指纹编码）",
        f"图片数量: {len(paths)}",
        f"处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "-" * 56,
    ]
    if taxon_token:
        kinds: list[tuple[str, list[Path]]] = [(taxon_token, sorted(paths))]
    else:
        groups: dict[str, list[Path]] = {}
        for p in paths:
            groups.setdefault(p.parent.name, []).append(p)
        kinds = [(name, sorted(imgs)) for name, imgs in groups.items()]
        if len(kinds) > 1:
            report_lines.append(f"检测到 {len(kinds)} 个来源文件夹，将按文件夹名分别解析类群。")

    manager = _v8b9d65af(db_dir / "raw_data.db",
                                         db_dir / "bird_observations.db")
    batch_id = manager.create_batch_run(
        quality_grade="local_import", photo_size="original",
        output_dir=str(out_dir))
    try:
        seen: set[str] = set()
        total_new = total_dup = total_fail = 0
        for k, (kind_name, images) in enumerate(kinds, 1):
            report_lines.append(f"[{k}/{len(kinds)}] 种类「{kind_name}」共 {len(images)} 张")
            new, dup, fail = _v64cf522e(
                kind_name, images, manager, batch_id, out_dir, seen, report_lines)
            total_new += new
            total_dup += dup
            total_fail += fail
        manager.update_batch_run(
            batch_id, taxa_succeeded=len(kinds),
            total_photos=total_new + total_dup,
            total_downloaded=total_new, total_failed=total_fail)
    finally:
        manager.close()

    report_lines.append("-" * 56)
    report_lines.append(
        f"导入完成: 新入库 {total_new} 张, 重复跳过 {total_dup} 张, 失败 {total_fail} 张。")
    report_lines.append("源图片保持原位未动。")
    return "\n".join(report_lines)


# ================= 文本清单爬取（原有流程） =================

def _v04ba6693(path: Path, max_per_taxon: int, workers: int,
                      photo_size: str, quality_grade: str) -> str:
    # 延迟导入，保持模块加载轻量
    from download_inaturalist import _v7ea57622

    report_lines = [
        f"拖入文件爬取报告",
        f"源文件: {path.name}",
        f"处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"参数: 每类群上限={max_per_taxon}, 尺寸={photo_size}, "
        f"质量={quality_grade}, 线程={workers}",
        "-" * 56,
    ]

    rows = _v6f4e2283(path)
    if not rows:
        report_lines.append("文件中没有有效内容（应为每行一个类群 ID 或物种名）。")
        return "\n".join(report_lines)

    for i, row in enumerate(rows, 1):
        taxon_id, note = _v726a4423(row)
        report_lines.append(f"[{i}/{len(rows)}] {note}")
        if taxon_id is None:
            continue
        try:
            summary = _v7ea57622(
                taxon_id=taxon_id,
                out_dir=_va1aa2a9b,
                photo_size=photo_size,
                workers=workers,
                max_observations=max_per_taxon,
                quality_grade=None if quality_grade == "any" else quality_grade,
            )
            report_lines.append(
                f"     完成: 观察 {summary['observation_count']} 条, "
                f"照片 {summary['photo_count']} 张 "
                f"(下载 {summary['photos_downloaded']}, "
                f"已存在 {summary['photos_skipped_existing']}, "
                f"失败 {summary['photos_failed']})")
            report_lines.append(f"     输出: {summary['output_dir']}")
        except Exception as exc:
            report_lines.append(f"     爬取失败: {exc}")

    report_lines.append("-" * 56)
    report_lines.append("全部条目处理完毕。")
    return "\n".join(report_lines)


# ================= 入口调度 =================

def _vcabbc455(path: Path, args: argparse.Namespace) -> None:
    print(f"\n>>> 检测到拖入文件: {path}")
    processed_dir = Path(args.inbox) / _v428ddb6f
    processed_dir.mkdir(parents=True, exist_ok=True)

    try:
        report = _v04ba6693(
            path, max_per_taxon=args.max_per_taxon, workers=args.workers,
            photo_size=args.photo_size, quality_grade=args.quality_grade)
        print(report)
        # 保存爬取报告
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = processed_dir / f"{path.stem}_爬取报告_{stamp}.txt"
        report_path.write_text(report, encoding="utf-8")
        print(f"报告已保存: {report_path.name}")
        # 移动源文件到已处理目录（避免重复处理）
        shutil.move(str(path), str(processed_dir / f"{stamp}_{path.name}"))
    except Exception as exc:
        print(f"处理失败: {exc}")
    print("<<< 处理结束，等待下一个文件...\n")


def _v0d7b53cd(path: Path, args: argparse.Namespace,
                  write_marker: bool = False,
                  move_to_processed: bool = False) -> None:
    print(f"\n>>> 检测到拖入文件夹: {path}")
    processed_dir = Path(args.inbox) / _v428ddb6f
    processed_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = _v70fe9a45(
            path, out_dir=Path(args.out_dir), db_dir=Path(args.db_dir),
            taxon_token=(args.taxon or "").strip())
        print(report)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = processed_dir / f"{path.name}_导入报告_{stamp}.txt"
        report_path.write_text(report, encoding="utf-8")
        print(f"报告已保存: {report_path.name}")
        if write_marker:
            try:
                (path / _v52c8c893).write_text(report, encoding="utf-8")
            except OSError:
                pass
        if move_to_processed:
            shutil.move(str(path), str(processed_dir / f"{stamp}_{path.name}"))
    except Exception as exc:
        print(f"处理失败: {exc}")
    print("<<< 处理结束，等待下一个文件...\n")


def _vcb32e035(paths: list[Path], args: argparse.Namespace,
                  move_to_processed: bool = False) -> None:
    print(f"\n>>> 检测到拖入图片 {len(paths)} 张")
    processed_dir = Path(args.inbox) / _v428ddb6f
    processed_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = _v3c6be5a8(
            paths, out_dir=Path(args.out_dir), db_dir=Path(args.db_dir),
            taxon_token=(args.taxon or "").strip())
        print(report)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = processed_dir / f"图片导入报告_{stamp}.txt"
        report_path.write_text(report, encoding="utf-8")
        print(f"报告已保存: {report_path.name}")
        if move_to_processed:
            # 仅常驻监视模式：导入成功后把投放目录里的图片移入已处理
            for p in paths:
                try:
                    shutil.move(str(p), str(processed_dir / f"{stamp}_{p.name}"))
                except OSError:
                    pass
    except Exception as exc:
        print(f"处理失败: {exc}")
    print("<<< 处理结束，等待下一个文件...\n")


def _v204b7c76(args: argparse.Namespace) -> None:
    inbox = Path(args.inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    print(f"拖入监视已启动: {inbox}")
    print("将 txt/csv 清单、图片或图片文件夹放入该目录即可自动处理。按 Ctrl+C 退出。\n")
    while True:
        for item in sorted(inbox.iterdir()):
            if item.is_dir():
                # 文件夹：跳过已写入完成标记的
                if not (item / _v52c8c893).exists():
                    _v0d7b53cd(item, args, write_marker=True,
                                  move_to_processed=True)
            elif item.is_file():
                if item.suffix.lower() in _v5e94797e:
                    _vcabbc455(item, args)
                elif item.suffix.lower() in _v170a9bb4:
                    _vcb32e035([item], args, move_to_processed=True)
        time.sleep(2)


def _v16f83c08(args: argparse.Namespace) -> int:
    files = [Path(p) for p in (args.file or [])]
    if not files:
        if args.watch:
            try:
                _v204b7c76(args)
            except KeyboardInterrupt:
                print("\n监视已停止。")
            return 0
        print("请指定 --file 文件/文件夹路径（可多个）或 --watch 常驻监视。")
        return 1

    image_group: list[Path] = []
    for path in files:
        if path.is_dir():
            _v0d7b53cd(path, args)
        elif path.is_file():
            if path.suffix.lower() in _v170a9bb4:
                image_group.append(path)
            elif path.suffix.lower() in _v5e94797e:
                _vcabbc455(path, args)
            else:
                print(f"不支持的文件类型（仅支持 txt/csv/list 与图片）: {path}")
        else:
            print(f"路径不存在: {path}")
    if image_group:
        _vcb32e035(image_group, args)
    return 0


def _v9a12dab4() -> int:
    parser = argparse.ArgumentParser(description="拖入文件自动爬取接口")
    parser.add_argument("--file", nargs="*", default=[],
                        help="要处理的文件/文件夹路径，可一次传多个（拖到 bat 上时自动传入）")
    parser.add_argument("--watch", action="store_true", help="常驻监视 drop_inbox 目录")
    parser.add_argument("--inbox", type=Path, default=_v0fbbe809, help="投放目录")
    parser.add_argument("--out-dir", type=Path, default=_va1aa2a9b, help="输出根目录")
    parser.add_argument("--db-dir", type=Path, default=_va6de3477, help="数据库目录")
    parser.add_argument("--taxon", default="",
                        help="统一归属类群（物种名/中文名/ID），覆盖文件夹名判定")
    parser.add_argument("--max-per-taxon", type=int, default=50, help="每个类群爬取上限（默认 50）")
    parser.add_argument("--workers", type=int, default=6, help="图片下载线程数")
    parser.add_argument("--photo-size", choices=["original", "large", "medium"], default="large")
    parser.add_argument("--quality-grade", choices=["research", "needs_id", "casual", "any"],
                        default="research")
    return _v16f83c08(parser.parse_args())


if __name__ == "__main__":
    sys.exit(_v9a12dab4())
