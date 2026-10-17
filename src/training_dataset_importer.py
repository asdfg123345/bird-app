#!/usr/bin/env python3
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 模块：训练数据集文件夹批量导入工具（train/val/test 划分目录）
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-27
"""训练数据集导入工具：把「类别文件夹 + 划分目录」结构的图片数据集
按种类指纹编码导入鸟类观察数据库。

目录结构约定（常见训练数据集布局）：
    数据集根目录/
        train/<类别名>/*.jpg     训练集（类别名即物种名或标注名）
        val/<类别名>/*.jpg       验证集
        test/<类别名>/*.jpg      测试集
        （其他目录与散文件一律忽略，例如部位检测数据集、labels.txt）

导入规则：
    1. 类别名形如「XX同属（英文名：Bolle's Pigeon）」时优先用括号内的
       英文名查询 iNaturalist 官方接口；否则直接用类别名全称查询。
    2. 同一类别在 train/val/test 中的图片合并为一条本地观察记录
       （与拖入接口一致：同一「类群 + 来源名」只建一条观察），
       观察备注中记录各划分的张数，划分信息不丢失。
    3. 照片以内容 SHA-256 为指纹去重，复制到
       data/taxon_<ID>_<名称>/photos/ 并登记负数本地照片 ID，
       与主爬虫、拖入接口完全同一套存储规范。
    4. 全程复用 inaturalist_db_manager 既有入库方法，不改动原库逻辑。
    5. 支持中断续导：重复运行时已入库照片按指纹跳过，观察记录自动复用。

用法示例：
    python training_dataset_importer.py --root "D:/鸟类/鸟类训练数据集_压缩版"
    python training_dataset_importer.py --root <数据集目录> --dry-run
    python training_dataset_importer.py --root <数据集目录> --workers 12
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

# 复用拖入接口的名称解析、层级登记、指纹与尺寸工具，保证行为一致
from drop_crawl_interface import (
    IMG_SUFFIXES,
    _image_size,
    _sha256_of_file,
    fetch_taxon_tree_nodes,
    resolve_taxon_info,
)
from download_inaturalist import slugify
from inaturalist_db_manager import INaturalistDatabaseManager

# 匹配类别名中「（英文名：XXX）」括号内的英文名（全角冒号兼容半角）
_EN_NAME_RE = re.compile(r"[（(]\s*英文名\s*[:：]\s*(.+?)\s*[）)]$")

# 默认识别的划分目录名（小写比较），未匹配到的子目录一律忽略
DEFAULT_SPLIT_NAMES = {"train", "val", "test", "valid", "validation"}


def _base_dir() -> Path:
    """返回脚本所在目录（导入输出根目录的定位基准，避免硬编码盘符）。"""
    return Path(__file__).resolve().parent


def clean_class_base(class_name: str) -> str:
    """清洗类别名得到基础中文名：去掉「（英文名：X）」括号与尾部「同属」。"""
    base = _EN_NAME_RE.sub("", class_name.strip()).strip()
    if base.endswith("同属"):
        base = base[:-2].strip()
    return base


def load_manifest(manifest_path: Path) -> dict[str, str]:
    """读取物种清单文件，返回 {中文名: 拉丁名} 映射。

    支持 xlsx（标准库解析，兼容内联字符串与共享字符串两种存储）与
    csv（首列表头「中文名」、次列「拉丁名」）。中文名作为类别基名的
    权威对照，拉丁名作为官方接口查询词，避免中文名多义导致误匹配。
    """
    suffix = manifest_path.suffix.lower()
    result: dict[str, str] = {}
    if suffix == ".csv":
        import csv
        with manifest_path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) >= 2 and row[0] and row[0] != "中文名":
                    result[row[0].strip()] = row[1].strip()
        return result
    # xlsx：用 zipfile + ElementTree 解析第一个工作表
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(manifest_path) as zf:
        # 共享字符串表可能不存在（内联字符串存储）
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in sroot.findall(f"{NS}si"):
                shared.append("".join(
                    t.text or "" for t in si.iter(f"{NS}t")))
        sheet_name = next(
            name for name in zf.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
        root = ET.fromstring(zf.read(sheet_name))
        for row in root.iter(f"{NS}row"):
            cells: list[str] = []
            for cell in row.iter(f"{NS}c"):
                if cell.get("t") == "s":  # 共享字符串引用
                    idx = int(next(cell.iter(f"{NS}v")).text or "0")
                    cells.append(shared[idx] if idx < len(shared) else "")
                else:  # 内联字符串或数值
                    inline = "".join(
                        t.text or "" for t in cell.iter(f"{NS}t"))
                    value = next(cell.iter(f"{NS}v"), None)
                    cells.append(inline or (value.text if value is not None and value.text else ""))
            if len(cells) >= 2 and cells[0] and cells[0] != "中文名":
                result[cells[0].strip()] = cells[1].strip()
    return result


def detect_manifest(root: Path) -> Path | None:
    """在数据集根目录自动探测物种清单文件（唯一 xlsx/csv 时返回）。"""
    candidates = [p for p in sorted(root.iterdir())
                  if p.is_file() and p.suffix.lower() in {".xlsx", ".csv"}]
    return candidates[0] if len(candidates) == 1 else None


def _resolve_effective_root(root: Path, split_names: set[str]) -> Path:
    """自动下钻定位真正的数据集根目录。

    压缩包解压后常多包一层同名外壳文件夹；当当前层不存在划分目录、
    且只有一个子目录时，自动进入该子目录继续查找（与拖入接口的
    自动下钻理念一致），直到找到划分目录或无法继续下钻。
    """
    current = root
    while True:
        has_split = any(
            child.is_dir() and child.name.lower() in split_names
            for child in current.iterdir())
        if has_split:
            return current
        children = [child for child in current.iterdir() if child.is_dir()]
        if len(children) != 1:
            return current  # 找不到划分目录且无法唯一下钻，原样返回
        current = children[0]


def collect_dataset(root: Path, split_names: set[str]) -> dict[str, dict[str, list[Path]]]:
    """扫描数据集根目录，按类别聚合各划分的图片。

    返回 {类别名: {划分名: [图片路径列表]}}；只识别 root 下名称匹配
    划分名的子目录，类别文件夹内递归收集所有图片（兼容多层嵌套布局）。
    """
    grouped: dict[str, dict[str, list[Path]]] = {}
    for split_dir in sorted(root.iterdir()):
        if not split_dir.is_dir() or split_dir.name.lower() not in split_names:
            continue  # 非划分目录（如部位检测数据集、散文件）直接忽略
        for class_dir in sorted(split_dir.iterdir()):
            if not class_dir.is_dir():
                continue
            images = [
                p for p in class_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in IMG_SUFFIXES
            ]
            if images:
                grouped.setdefault(class_dir.name, {})\
                       .setdefault(split_dir.name, []).extend(images)
    return grouped


def build_query(class_name: str, manifest: dict[str, str] | None) -> str:
    """确定类别的官方接口查询词（三级优先）。

    1. 类别名含「（英文名：X）」→ 用英文名 X（同属展开类的唯一标识）；
    2. 清单中能按基名匹配到拉丁名 → 用拉丁名（权威，避免中文名多义、
       修复「台湾噪鹛」等中文名在 iNat 与中文文献间指代不一致的问题）；
    3. 兜底用类别名全称做中文名检索。
    """
    match = _EN_NAME_RE.search(class_name.strip())
    if match:
        return match.group(1).strip()
    base = clean_class_base(class_name)
    if manifest and base in manifest and manifest[base].strip():
        return manifest[base].strip()
    return class_name.strip()


def _hash_images(images: list[Path], workers: int) -> list[tuple[Path, str]]:
    """多线程并行计算图片内容 SHA-256 指纹，保持与输入同序。"""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(lambda p: (p, _sha256_of_file(p)), images))


def import_one_class(class_name: str, split_map: dict[str, list[Path]],
                     manifest: dict[str, str],
                     manager: INaturalistDatabaseManager, batch_id: int,
                     out_dir: Path, seen_in_session: set[str],
                     report_lines: list[str],
                     workers: int) -> tuple[int, int, int]:
    """导入一个类别在所有划分下的全部图片，汇入同一条本地观察。

    返回 (新入库数, 重复跳过数, 失败数)；类别解析失败时跳过并记录。
    """
    total = sum(len(v) for v in split_map.values())
    query = build_query(class_name, manifest)
    taxon_id, sci, common, note = resolve_taxon_info(query)
    report_lines.append(f"{class_name} → {note}")
    if taxon_id is None:
        report_lines.append(f"    共 {total} 张全部跳过（类别解析失败）")
        return 0, 0, total

    # 与主爬虫、拖入接口一致的类群目录命名，保证多来源数据合并存储
    folder_name = f"taxon_{taxon_id}_{slugify(sci)}_{slugify(common)}"
    photo_dir = out_dir / folder_name / "photos"
    photo_dir.mkdir(parents=True, exist_ok=True)

    # 登记 iNaturalist 官方纲-目-科-种层级链（失败不中断导入）
    try:
        tree_nodes = fetch_taxon_tree_nodes(taxon_id)
        if tree_nodes:
            manager.upsert_taxon_tree_bulk(tree_nodes)
    except Exception:
        pass

    # 观察备注记录各划分张数，划分信息不丢失（可据此重建训练划分）
    stat_text = "、".join(
        f"{split}={len(files)}张" for split, files in sorted(split_map.items()))
    note_parts = [f"训练数据集导入来源: {class_name}", f"划分统计: {stat_text}"]
    obs_id = manager.upsert_local_observation(
        taxon_id=taxon_id, scientific_name=sci, common_name=common,
        source_name=class_name, note="\n".join(note_parts))

    # 预取本类照片 ID 起点并按序递减分配，减少逐张查询开销
    base_photo_id = manager.next_local_photo_id()
    new = dup = fail = 0
    position = 0
    for split in sorted(split_map):
        for img, sha in _hash_images(split_map[split], workers):
            position += 1
            seen_in_session.add(sha)
            if manager.has_photo_fingerprint(sha):
                dup += 1
                continue  # 跨批次内容重复，跳过
            base_photo_id -= 1
            filename = f"obs_{obs_id}_photo_{base_photo_id}{img.suffix.lower()}"
            dest = photo_dir / filename
            try:
                # 先复制为临时名再原子改名，避免半截文件被当作有效照片
                tmp = dest.with_name(dest.name + ".importing")
                shutil.copy2(img, tmp)
                tmp.replace(dest)
            except OSError as exc:
                fail += 1
                report_lines.append(f"    复制失败 {img.name}: {exc}")
                continue
            width, height = _image_size(dest)
            status = manager.upsert_local_photo(
                photo_id=base_photo_id, observation_id=obs_id, content_sha=sha,
                original_name=img.name,
                dest_relative=str(Path("photos") / filename),
                file_size=dest.stat().st_size,
                width=width, height=height, position=position)
            if status == "inserted":
                new += 1
            else:  # 指纹唯一约束兜底：极小概率重复
                dup += 1
                dest.unlink(missing_ok=True)
        report_lines.append(
            f"    [{split}] 共 {len(split_map[split])} 张处理完毕")

    manager.refresh_local_observation_stats(obs_id)
    manager.record_batch_taxon(
        batch_id=batch_id, taxon_id=taxon_id,
        observation_count=1, photo_count=total,
        photos_downloaded=new, photos_failed=fail)
    manager.link_batch_observation(batch_id=batch_id, observation_id=obs_id)
    report_lines.append(
        f"    {folder_name}: 合计 {total} 张，新入库 {new}，重复 {dup}，"
        f"失败 {fail}，本地观察ID {obs_id}")
    return new, dup, fail


def run_import(args: argparse.Namespace) -> int:
    """导入主流程：扫描 → 按类别聚合 → 指纹编码入库 → 批次汇总。"""
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"数据集根目录不存在: {root}")
        return 1
    db_dir = Path(args.db_dir).resolve() if args.db_dir \
        else _base_dir() / "data" / "db"
    out_dir = Path(args.out_dir).resolve() if args.out_dir \
        else _base_dir() / "data"

    # 扫描并聚合数据集结构（压缩包外壳目录自动下钻）
    split_names = (set(DEFAULT_SPLIT_NAMES) if args.splits == "auto"
                   else {s.strip() for s in args.splits.split(",") if s.strip()})
    root = _resolve_effective_root(root, split_names)
    grouped = collect_dataset(root, split_names)
    if not grouped:
        print("未在根目录下发现 train/val/test 划分目录或任何图片，"
              "可用 --splits 指定划分目录名。")
        return 1
    photo_total = sum(len(files)
                      for splits in grouped.values()
                      for files in splits.values())
    print(f"数据集扫描完成：{len(grouped)} 个类别，共 {photo_total} 张图片")

    if args.dry_run:
        # 试运行：只做名称解析，不复制文件、不写数据库
        manifest = load_manifest(Path(args.manifest).resolve()) \
            if args.manifest else {}
        detected = detect_manifest(root)
        if not manifest and detected:
            manifest = load_manifest(detected)
            print(f"已自动加载物种清单: {detected}（{len(manifest)} 条）")
        print("=== 试运行模式：仅解析类别名，不写入任何数据 ===")
        cache: dict[str, str] = {}
        for index, class_name in enumerate(sorted(grouped), 1):
            query = build_query(class_name, manifest)
            if query in cache:
                note = cache[query]
            else:
                _, _, _, note = resolve_taxon_info(query)
                cache[query] = note
                time.sleep(0.2)  # 试运行限速，避免请求过快
            count = sum(len(files) for files in grouped[class_name].values())
            print(f"[{index}/{len(grouped)}] {class_name} → {note} ({count} 张)")
        return 0

    # 正式导入：创建批次，逐类入库，最后汇总批次统计
    manifest = load_manifest(Path(args.manifest).resolve()) \
        if args.manifest else {}
    detected = detect_manifest(root)
    if not manifest and detected:
        manifest = load_manifest(detected)
        print(f"已自动加载物种清单: {detected}（{len(manifest)} 条）")
    manager = INaturalistDatabaseManager(db_dir / "raw_data.db",
                                         db_dir / "bird_observations.db")
    batch_id = manager.create_batch_run(
        batch_time=datetime.now().isoformat(timespec="seconds"),
        taxa_requested=len(grouped), quality_grade="local",
        photo_size="dataset", max_per_taxon=0,
        output_dir=str(root))
    seen_in_session: set[str] = set()
    report_lines: list[str] = [
        f"# 训练数据集导入报告 {datetime.now().isoformat(timespec='seconds')}",
        f"- 数据集根目录: {root}",
        f"- 数据库目录: {db_dir}",
        f"- 批次ID: {batch_id}",
        "",
    ]
    total_new = total_dup = total_fail = 0
    started = time.time()
    try:
        for index, class_name in enumerate(sorted(grouped), 1):
            count = sum(len(files) for files in grouped[class_name].values())
            print(f"[{index}/{len(grouped)}] {class_name} ({count} 张)")
            new, dup, fail = import_one_class(
                class_name, grouped[class_name], manifest, manager,
                batch_id, out_dir, seen_in_session, report_lines, args.workers)
            total_new += new
            total_dup += dup
            total_fail += fail
    except KeyboardInterrupt:
        print("用户中断：已完成部分保持入库，可重复运行本工具续导（指纹去重）")
        raise
    finally:
        elapsed = time.time() - started
        manager.update_batch_run(
            batch_id, total_observations=len(grouped),
            total_photos=photo_total,
            total_downloaded=total_new, total_failed=total_fail)
        manager.close()
        report_lines += [
            "",
            "## 汇总",
            f"- 类别数: {len(grouped)}",
            f"- 新入库照片: {total_new}",
            f"- 重复跳过: {total_dup}",
            f"- 失败: {total_fail}",
            f"- 耗时: {elapsed:.1f} 秒",
        ]
        # 导入报告写入 data 目录，文件名带时间戳便于区分多次导入
        report_path = out_dir / (
            f"导入报告_训练数据集_{datetime.now():%Y%m%d_%H%M%S}.md")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(report_lines), encoding="utf-8")
        print(f"导入报告已保存: {report_path}")

    print(f"=== 导入完成：新入库 {total_new}，重复 {total_dup}，"
          f"失败 {total_fail}，耗时 {time.time() - started:.1f} 秒 ===")
    return 0 if total_fail == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="训练数据集导入工具：train/val/test 划分目录按种类指纹编码入库")
    parser.add_argument("--root", required=True, help="数据集根目录路径")
    parser.add_argument("--splits", default="auto",
                        help="划分目录名（逗号分隔；auto=自动识别 train/val/test）")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="照片输出根目录（默认脚本目录下的 data）")
    parser.add_argument("--db-dir", type=Path, default=None,
                        help="数据库目录（默认脚本目录下 data/db）")
    parser.add_argument("--workers", type=int, default=8,
                        help="指纹计算并发线程数（默认 8）")
    parser.add_argument("--manifest", default=None,
                        help="物种清单文件路径（xlsx/csv，中文名+拉丁名；"
                             "默认自动探测数据集根目录下唯一清单文件）")
    parser.add_argument("--dry-run", action="store_true",
                        help="试运行：仅解析类别名并统计，不写库不复制")
    return parser


def main() -> int:
    """命令行入口。"""
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    return run_import(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
