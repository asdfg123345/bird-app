#!/usr/bin/env python3
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-15
"""训练数据集导出模块。

把数据库中已下载的照片一键导出为 PyTorch 可直接加载的标准数据集：
- 按"观察"为单位划分 train/val/test（防止同一观察的照片跨集合造成数据泄漏）
- 生成 manifest.csv / manifest.jsonl 清单（含纲-目-科层级与许可证信息）
- 默认硬链接导出（同盘零拷贝，100GB 级数据不产生磁盘翻倍）
- 支持最小尺寸、许可证白名单、每类样本上下限过滤
- 输出数据质量报告与 PyTorch 加载示例代码

纯标准库实现；PIL（Pillow）仅在存在时用于图片完整性校验，缺失则自动跳过。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

# 路径相对脚本目录解析，避免硬编码
BASE_DIR = Path(__file__).resolve().parent

# 三个标准数据划分集合
SPLIT_NAMES = ("train", "val", "test")

# manifest.jsonl 的固定字段顺序
MANIFEST_FIELDS = [
    "file", "split", "taxon_id", "label", "scientific_name", "common_name",
    "class", "order", "family", "observation_id", "photo_id",
    "license_code", "attribution", "width", "height", "sha256_16", "mode",
]


def build_parser() -> argparse.ArgumentParser:
    """构建 dataset 子命令的参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="dataset",
        description="把数据库照片导出为标准训练数据集（按观察划分、防泄漏、硬链接零拷贝）",
    )
    parser.add_argument("--out", type=Path, required=True,
                        help="导出目标目录（数据集根目录）")
    parser.add_argument("--taxon-ids", type=str, default="",
                        help="仅导出指定类群（逗号分隔的类群 ID，空=全部；"
                             "GUI「训练数据集」页签按勾选类群导出时使用）")
    parser.add_argument("--data-dir", type=Path, default=BASE_DIR / "data",
                        help="照片数据根目录（含 taxon_* 目录，默认脚本目录下的 data）")
    parser.add_argument("--main-db", type=Path, default=BASE_DIR / "data" / "db" / "bird_observations.db",
                        help="主数据库路径（默认 data/db/bird_observations.db）")
    parser.add_argument("--splits", type=str, default="8:1:1",
                        help="train:val:test 比例（默认 8:1:1）")
    parser.add_argument("--seed", type=int, default=42,
                        help="划分随机种子（保证可复现，默认 42）")
    parser.add_argument("--label-scheme", choices=["id", "sci", "cn"], default="id",
                        help="类别目录命名（id=类群ID / sci=学名 / cn=中文名，默认 id）")
    parser.add_argument("--min-per-class", type=int, default=10,
                        help="每类最少照片数，少于此数的类跳过（0=不过滤）")
    parser.add_argument("--max-per-class", type=int, default=0,
                        help="每类最多照片数，超出部分按观察随机舍弃（0=不限）")
    parser.add_argument("--min-width", type=int, default=0,
                        help="最小图片宽度像素（0=不过滤）")
    parser.add_argument("--min-height", type=int, default=0,
                        help="最小图片高度像素（0=不过滤）")
    parser.add_argument("--license", type=str, default="",
                        help="许可证白名单（逗号分隔，如 cc-by,cc-by-nc；空=不过滤）")
    parser.add_argument("--link", choices=["hard", "copy"], default="hard",
                        help="导出方式（hard=硬链接零拷贝，跨盘自动回退复制；copy=直接复制）")
    parser.add_argument("--no-verify", action="store_true",
                        help="跳过图片完整性校验（默认用 PIL 校验，PIL 未安装时自动跳过）")
    return parser


def _clean_label(text: str, fallback: str) -> str:
    """把类名清洗为文件系统安全的目录名，空结果回退到 fallback。"""
    cleaned = re.sub(r"[^\w\s()-]", "", text or "", flags=re.UNICODE).strip()
    cleaned = re.sub(r"[-\s]+", "_", cleaned)
    return cleaned or fallback


def _load_taxa_meta(manager) -> dict[int, dict[str, str]]:
    """读取 taxa 表的类群基础信息作为 taxon_tree 缺失时的兜底。"""
    conn = manager._get_main_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT taxon_id, scientific_name, common_name, rank FROM taxa"
    )
    meta: dict[int, dict[str, str]] = {}
    for taxon_id, sci, cn, rank in cursor.fetchall():
        meta[int(taxon_id)] = {
            "name_sci": sci or "",
            "name_cn": cn or "",
            "rank": rank or "",
        }
    return meta


def _ancestor_ranks(tree: dict[int, dict[str, Any]],
                    taxa_meta: dict[int, dict[str, str]],
                    taxon_id: int) -> dict[str, str]:
    """沿层级链向上取纲/目/科名称（tree 优先，taxa 表兜底）。

    返回 {"class": 纲名, "order": 目名, "family": 科名}，取不到的层级
    用 taxon_{ID} 占位，保证 manifest 字段始终可用。
    """
    want = {"class", "order", "family"}
    result = {"class": "", "order": "", "family": ""}
    seen: set[int] = set()
    current: int | None = taxon_id
    while current and current not in seen:
        seen.add(current)
        node = tree.get(current)
        if node is None:
            break
        rank = node["rank"]
        if rank in want and not result[rank]:
            result[rank] = node["name_cn"] or node["name_sci"] or f"taxon_{current}"
        current = node["parent_id"]
    # taxa 表兜底补全仍缺失的层级
    for tid in seen:
        if all(result[r] for r in want):
            break
        meta = taxa_meta.get(tid)
        if not meta:
            continue
        rank = meta["rank"]
        if rank in want and not result[rank]:
            result[rank] = (meta["name_cn"] or meta["name_sci"]
                            or f"taxon_{tid}")
    return {r: result[r] or f"taxon_{taxon_id}" for r in want}


def _resolve_taxon_dir(data_dir: Path, taxon_id: int,
                       sci: str, cn: str) -> Path | None:
    """定位照片所在的 taxon_* 目录：先按命名规则精确匹配，再按前缀兜底。"""
    from download_inaturalist import slugify

    exact = data_dir / f"taxon_{taxon_id}_{slugify(sci)}_{slugify(cn)}"
    if exact.is_dir():
        return exact
    for cand in sorted(data_dir.glob(f"taxon_{taxon_id}_*")):
        if cand.is_dir():
            return cand
    return None


def _fetch_candidates(manager) -> list[dict[str, Any]]:
    """从主库拉取全部有本地文件的照片记录（JOIN 观察表取类群信息）。"""
    conn = manager._get_main_conn()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT p.photo_id, p.observation_id, p.local_path, p.license_code,
               p.attribution, p.width, p.height, p.fingerprint,
               o.taxon_id, o.scientific_name, o.common_name
        FROM photos p
        JOIN observations o ON p.observation_id = o.observation_id
        WHERE p.local_path != ''
        """
    )
    rows: list[dict[str, Any]] = []
    for (photo_id, obs_id, local_path, license_code, attribution,
         width, height, fingerprint, taxon_id, sci, cn) in cursor.fetchall():
        rows.append({
            "photo_id": photo_id,
            "observation_id": obs_id,
            "local_path": local_path,
            "license_code": license_code or "",
            "attribution": attribution or "",
            "width": width,
            "height": height,
            "fingerprint": fingerprint or "",
            "taxon_id": taxon_id,
            "scientific_name": sci or "",
            "common_name": cn or "",
        })
    return rows


def _verify_image(path: Path) -> tuple[bool, int, int, str]:
    """用 PIL 校验图片完整性，返回 (是否有效, 宽, 高, 色彩模式)。

    PIL 未安装时视为有效但信息未知，调用方据此前进。
    """
    try:
        from PIL import Image
    except ImportError:
        return True, 0, 0, ""
    try:
        with Image.open(path) as img:
            img.load()
            return True, img.width, img.height, img.mode
    except Exception:
        return False, 0, 0, ""


def _link_or_copy(src: Path, dst: Path, mode: str) -> str:
    """按指定方式落盘：hard=硬链接（跨盘/不支持时回退复制），copy=复制。

    返回实际使用的方式（"hard" 或 "copy"），供报告统计。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if mode == "hard":
        try:
            os.link(src, dst)
            return "hard"
        except OSError:
            pass  # 跨盘或文件系统不支持，回退复制
    shutil.copy2(src, dst)
    return "copy"


def export_dataset(args: argparse.Namespace) -> int:
    """导出主流程：查询 → 过滤 → 观察级划分 → 落盘 → 清单与报告。"""
    from inaturalist_db_manager import INaturalistDatabaseManager

    out_dir: Path = args.out
    data_dir: Path = args.data_dir
    main_db: Path = args.main_db
    if not main_db.exists():
        print(f"主数据库不存在: {main_db}")
        return 1

    # 解析划分比例
    try:
        parts = [float(x) for x in str(args.splits).split(":")]
        if len(parts) != 3 or sum(parts) <= 0:
            raise ValueError
        total = sum(parts)
        ratios = [p / total for p in parts]
    except ValueError:
        print(f"划分比例格式错误: {args.splits}（应为如 8:1:1）")
        return 1

    license_allow = {x.strip().lower() for x in str(args.license).split(",")
                     if x.strip()}
    # 类群白名单（空=全部）：GUI「训练数据集」页签按勾选类群导出时传入
    raw_ids = str(getattr(args, "taxon_ids", "") or "").strip()
    taxon_allow: set[int] = set()
    if raw_ids:
        for part in raw_ids.split(","):
            part = part.strip()
            if not part.isdigit():
                print(f"--taxon-ids 含非法值: {part}（应为逗号分隔的数字类群 ID）")
                return 1
            taxon_allow.add(int(part))
    rng = random.Random(args.seed)

    manager = INaturalistDatabaseManager(
        main_db.parent / "raw_data.db", main_db)
    try:
        tree = manager.load_taxon_tree()
        taxa_meta = _load_taxa_meta(manager)
        candidates = _fetch_candidates(manager)
    finally:
        manager.close()

    print(f"数据库候选照片: {len(candidates)} 张")

    # 逐条过滤并按类群分组（类 → 观察集合）
    # photos_by_taxon[taxon_id] = {observation_id: [记录, ...]}
    photos_by_taxon: dict[int, dict[int, list[dict[str, Any]]]] = {}
    meta_by_taxon: dict[int, dict[str, str]] = {}
    skip_no_file = skip_license = skip_size = skip_taxon = 0
    for row in candidates:
        taxon_id = row["taxon_id"]
        if taxon_id is None:
            skip_no_file += 1
            continue
        taxon_id = int(taxon_id)
        # 类群白名单过滤（白名单为空 = 不过滤，保持原有 CLI 行为不变）
        if taxon_allow and taxon_id not in taxon_allow:
            skip_taxon += 1
            continue
        # 许可证白名单过滤（白名单为空 = 不过滤）
        if license_allow and row["license_code"].lower() not in license_allow:
            skip_license += 1
            continue
        # 库中尺寸过滤（宽高未知的交由校验阶段实测）
        if row["width"] and args.min_width and row["width"] < args.min_width:
            skip_size += 1
            continue
        if row["height"] and args.min_height and row["height"] < args.min_height:
            skip_size += 1
            continue
        # 定位源文件
        if taxon_id not in meta_by_taxon:
            src_dir = _resolve_taxon_dir(
                data_dir, taxon_id,
                row["scientific_name"], row["common_name"])
            meta_by_taxon[taxon_id] = {"src_dir": str(src_dir) if src_dir else ""}
        src_dir = meta_by_taxon[taxon_id]["src_dir"]
        if not src_dir:
            skip_no_file += 1
            continue
        src = Path(src_dir) / row["local_path"]
        if not src.is_file():
            skip_no_file += 1
            continue
        row["src"] = src
        photos_by_taxon.setdefault(taxon_id, {}).setdefault(
            row["observation_id"], []).append(row)

    # 每类样本量统计与上下限过滤
    class_stats: dict[int, int] = {
        tid: sum(len(v) for v in obs_map.values())
        for tid, obs_map in photos_by_taxon.items()
    }
    dropped_small = [tid for tid, n in class_stats.items()
                     if args.min_per_class and n < args.min_per_class]
    for tid in dropped_small:
        photos_by_taxon.pop(tid, None)
    class_stats = {tid: n for tid, n in class_stats.items() if tid in photos_by_taxon}

    # 观察级划分与导出
    verify = not args.no_verify
    manifest_rows: list[dict[str, Any]] = []
    stats = {"exported": 0, "corrupted": 0, "hardlinks": 0, "copied": 0}
    corrupt_list: list[str] = []
    label_seen: dict[str, int] = {}

    for taxon_id in sorted(photos_by_taxon):
        obs_map = photos_by_taxon[taxon_id]
        sample_row = next(iter(obs_map.values()))[0]
        sci = sample_row["scientific_name"]
        cn = sample_row["common_name"]
        ranks = _ancestor_ranks(tree, taxa_meta, taxon_id)

        # 类别目录名
        if args.label_scheme == "sci":
            label = _clean_label(sci, f"taxon_{taxon_id}")
        elif args.label_scheme == "cn":
            # 优先取真正含中文的俗名（iNaturalist 优先俗名默认随 locale
            # 常为英文），全无中文时回退学名，避免"cn 目录却是英文名"
            cn_tree = (tree.get(taxon_id) or {}).get("name_cn") or ""
            cn_pick = ""
            for cand in (cn_tree, cn):
                if re.search(r"[\u4e00-\u9fff]", cand or ""):
                    cn_pick = cand
                    break
            label = (_clean_label(cn_pick, "")
                     or _clean_label(sci, f"taxon_{taxon_id}"))
        else:
            label = str(taxon_id)
        # 同名类别去重（不同类群可能同名）
        if label in label_seen and label_seen[label] != taxon_id:
            label = f"{label}_{taxon_id}"
        label_seen[label] = taxon_id

        # 观察级上限采样：随机打乱观察，按序取直到够数
        obs_ids = sorted(obs_map)
        rng.shuffle(obs_ids)
        if args.max_per_class and args.max_per_class > 0:
            kept: list[int] = []
            count = 0
            for oid in obs_ids:
                n = len(obs_map[oid])
                if count + n > args.max_per_class and count > 0:
                    continue
                kept.append(oid)
                count += n
                if count >= args.max_per_class:
                    break
            obs_ids = kept

        # 观察级划分（8:1:1 → 观察数按比例切分，同观察照片不跨集合）
        n_obs = len(obs_ids)
        n_val = max(1, round(n_obs * ratios[1])) if n_obs >= 3 else (1 if n_obs == 2 else 0)
        n_test = max(1, round(n_obs * ratios[2])) if n_obs >= 3 else (1 if n_obs == 2 else 0)
        if n_obs < 3:
            n_val = min(n_val, 1)
            n_test = min(n_test, 1)
            if n_val + n_test >= n_obs:
                n_val = n_test = 0 if n_obs < 3 else n_val
        n_train = n_obs - n_val - n_test
        if n_train <= 0:
            # 观察太少无法三分，全部进 train（保证训练可用）
            obs_split = {oid: "train" for oid in obs_ids}
        else:
            obs_split: dict[int, str] = {}
            for oid in obs_ids[:n_train]:
                obs_split[oid] = "train"
            for oid in obs_ids[n_train:n_train + n_val]:
                obs_split[oid] = "val"
            for oid in obs_ids[n_train + n_val:]:
                obs_split[oid] = "test"

        # 逐照片落盘
        for oid, split in obs_split.items():
            for row in obs_map[oid]:
                src: Path = row["src"]
                ext = src.suffix.lower()
                filename = f"obs_{oid}_p{row['photo_id']}{ext}"
                dst = out_dir / split / label / filename
                mode = ""
                width, height = row["width"], row["height"]
                if verify:
                    ok, vw, vh, vmode = _verify_image(src)
                    if not ok:
                        stats["corrupted"] += 1
                        corrupt_list.append(str(src))
                        continue
                    # 库中尺寸未知时用实测值，并补做尺寸过滤
                    if not width:
                        width = vw
                    if not height:
                        height = vh
                    mode = vmode
                    if (args.min_width and width and width < args.min_width) or (
                            args.min_height and height and height < args.min_height):
                        skip_size += 1
                        continue
                used = _link_or_copy(src, dst, args.link)
                if used == "hard":
                    stats["hardlinks"] += 1
                else:
                    stats["copied"] += 1
                stats["exported"] += 1
                manifest_rows.append({
                    "file": dst.relative_to(out_dir).as_posix(),
                    "split": split,
                    "taxon_id": taxon_id,
                    "label": label,
                    "scientific_name": sci,
                    "common_name": cn,
                    "class": ranks["class"],
                    "order": ranks["order"],
                    "family": ranks["family"],
                    "observation_id": oid,
                    "photo_id": row["photo_id"],
                    "license_code": row["license_code"] or "unknown",
                    "attribution": row["attribution"],
                    "width": width,
                    "height": height,
                    "sha256_16": row["fingerprint"][:16],
                    "mode": mode,
                })

    if not manifest_rows:
        print("没有可导出的照片（请检查数据目录与过滤条件）")
        return 1

    # 写出 manifest（csv 用 utf-8-sig 便于 Excel，jsonl 用 utf-8）
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_csv = out_dir / "manifest.csv"
    with manifest_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(manifest_rows)
    manifest_jsonl = out_dir / "manifest.jsonl"
    with manifest_jsonl.open("w", encoding="utf-8") as handle:
        for row in manifest_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 写出 PyTorch 加载示例
    write_train_example(out_dir)

    # 生成质量报告
    report = build_report(out_dir, args, manifest_rows, stats,
                          skip_no_file, skip_license, skip_size,
                          dropped_small, corrupt_list, ratios)
    (out_dir / "report.md").write_text(report, encoding="utf-8")

    # 控制台摘要
    split_counter = Counter(r["split"] for r in manifest_rows)
    print("导出完成:")
    if taxon_allow:
        print(f"  类群过滤: 仅导出勾选的 {len(taxon_allow)} 个类群"
              f"（跳过非勾选记录 {skip_taxon} 条）")
    print(f"  类别数: {len({r['taxon_id'] for r in manifest_rows})}")
    print(f"  照片数: {stats['exported']} "
          f"(train {split_counter.get('train', 0)} / "
          f"val {split_counter.get('val', 0)} / "
          f"test {split_counter.get('test', 0)})")
    print(f"  硬链接 {stats['hardlinks']} / 复制 {stats['copied']} / "
          f"损坏 {stats['corrupted']}")
    print(f"  数据集目录: {out_dir}")
    return 0


def write_train_example(out_dir: Path) -> None:
    """生成 PyTorch 加载示例脚本（随数据集输出，供训练端直接参考）。"""
    example = '''# -*- coding: utf-8 -*-
# PyTorch 数据集加载示例（由 dataset 导出命令自动生成）
# 作者签名：天咏（明月照今日，风摇过两川。）
from pathlib import Path
from PIL import Image
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler

ROOT = Path(__file__).resolve().parent

# 方式一：ImageFolder 直接加载（train/val/test 目录即类别目录）
# from torchvision.datasets import ImageFolder
# train_set = ImageFolder(ROOT / "train", transform=your_transform)

# 方式二：manifest.jsonl 自定义 Dataset（可携带层级标签与许可证）
class BirdManifestDataset(Dataset):
    def __init__(self, split: str, transform=None):
        import json
        self.items = []
        with (ROOT / "manifest.jsonl").open(encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                if row["split"] == split:
                    self.items.append(row)
        self.labels = sorted({r["taxon_id"] for r in self.items})
        self.label_to_idx = {t: i for i, t in enumerate(self.labels)}
        self.transform = transform

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        row = self.items[idx]
        img = Image.open(ROOT / row["file"]).convert("RGB")
        if self.transform:
            img = self.transform(img)
        return img, self.label_to_idx[row["taxon_id"]]

# 类别不平衡时按 manifest 自动计算采样权重：
# ds = BirdManifestDataset("train")
# counts = [sum(1 for r in ds.items if r["taxon_id"] == t) for t in ds.labels]
# weights = [1.0 / counts[ds.label_to_idx[r["taxon_id"]]] for r in ds.items]
# sampler = WeightedRandomSampler(weights, num_samples=len(ds), replacement=True)
# loader = DataLoader(ds, batch_size=32, sampler=sampler)
'''
    (out_dir / "train_example.py").write_text(example, encoding="utf-8")


def build_report(out_dir: Path, args: argparse.Namespace,
                 rows: list[dict[str, Any]], stats: dict[str, int],
                 skip_no_file: int, skip_license: int, skip_size: int,
                 dropped_small: list[int], corrupt_list: list[str],
                 ratios: list[float]) -> str:
    """生成数据质量报告（markdown）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    per_class: dict[str, int] = {}
    per_split: Counter = Counter()
    per_license: Counter = Counter()
    for r in rows:
        per_class[r["label"]] = per_class.get(r["label"], 0) + 1
        per_split[r["split"]] += 1
        per_license[r["license_code"]] += 1
    counts = sorted(per_class.values(), reverse=True)
    total = len(rows)
    lines = [
        "# 训练数据集质量报告",
        "",
        f"- 生成时间: {now}",
        f"- 数据集目录: {out_dir}",
        f"- 划分比例参数: {args.splits}（实际按观察划分，见下方分布）",
        f"- 划分随机种子: {args.seed}",
        f"- 类别命名: {args.label_scheme}",
        "",
        "## 汇总",
        "",
        f"- 类别数: {len(per_class)}",
        f"- 照片总数: {total}",
        f"- train/val/test: {per_split.get('train', 0)} / "
        f"{per_split.get('val', 0)} / {per_split.get('test', 0)}",
        f"- 硬链接 {stats['hardlinks']}，复制 {stats['copied']}",
        f"- 损坏跳过: {stats['corrupted']}",
        "",
        "## 每类样本数分布",
        "",
    ]
    if counts:
        lines += [
            f"- 最多: {counts[0]} 张；最少: {counts[-1]} 张；"
            f"中位数: {counts[len(counts) // 2]} 张",
            "",
            "| 类别 | 照片数 |",
            "| --- | --- |",
        ]
        for label, n in sorted(per_class.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {label} | {n} |")
    lines += [
        "",
        "## 许可证分布",
        "",
        "| 许可证 | 照片数 |",
        "| --- | --- |",
    ]
    for lic, n in per_license.most_common():
        lines.append(f"| {lic} | {n} |")
    lines += [
        "",
        "## 过滤统计",
        "",
        f"- 源文件缺失/类群ID缺失: {skip_no_file}",
        f"- 许可证不在白名单: {skip_license}",
        f"- 尺寸不足: {skip_size}",
        f"- 样本数低于 --min-per-class 被丢弃的类别数: {len(dropped_small)}"
        + (f"（taxon_id: {', '.join(map(str, dropped_small))}）"
           if dropped_small else ""),
    ]
    if corrupt_list:
        lines += ["", "## 损坏文件清单", ""]
        lines += [f"- {p}" for p in corrupt_list[:50]]
        if len(corrupt_list) > 50:
            lines.append(f"- …（共 {len(corrupt_list)} 个，仅列前 50）")
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> int:
    """dataset 子命令入口。"""
    return export_dataset(args)


def main() -> int:
    """独立运行入口：python dataset_exporter.py --out 某目录。"""
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
