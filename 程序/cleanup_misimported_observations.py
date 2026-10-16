# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from pathlib import Path

# 报告行解析正则
# 种类块头：[3/14] 种类「黑顶蛙口夜鹰」共 19 张
_v0d720ecc = re.compile(r"^\[(\d+)/(\d+)\] 种类「(.+?)」共 (\d+) 张")
# 小结行：小计 taxon_351629_Neolentinus_lepideus_Trainwrecker: 新入库 56, 重复 0, 失败 0, 本地观察ID -213
_v1b945b10 = re.compile(
    r"小计 taxon_(\d+?)_.+: 新入库 (\d+), 重复 (\d+), 失败 (\d+), 本地观察ID (-?\d+)")
# 回退命中行：回退层级名「train」命中: ...
_v181cc191 = re.compile(r"回退层级名「(.+?)」命中")
# 数据集分割/功能目录名黑名单（与 drop_crawl_interface 保持一致）
_v754e9f8e = {
    "train", "val", "valid", "validation", "test", "eval",
    "images", "image", "imgs", "photos", "photo", "pictures",
    "labels", "label", "annotations", "dataset", "data", "raw",
}
# 数据集按属级分组的文件夹名特征（如「中亚鸽同属（英文名：Bolle's Pigeon）」）
_v8380df01 = "同属（英文名"


def _vaaaec1bf(reports_dir: Path) -> list[dict]:
    found: dict[int, dict] = {}
    for report in sorted(reports_dir.glob("*_导入报告_*.txt")):
        try:
            text = report.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[警告] 无法读取报告 {report.name}: {exc}")
            continue
        cur_kind = None
        cur_fallback = False
        for line in text.splitlines():
            head = _v0d720ecc.match(line.strip())
            if head:
                cur_kind = head.group(3)
                cur_fallback = False
                continue
            fb = _v181cc191.search(line)
            if fb:
                # 仅回退到分割目录名（train/val/test 等）才视为误匹配
                cur_fallback = fb.group(1).strip().lower() in _v754e9f8e
                continue
            m = _v1b945b10.search(line)
            if m and cur_fallback and cur_kind:
                taxon_id = int(m.group(1))
                obs_id = int(m.group(5))
                if obs_id >= 0:
                    # 正数观察 ID 属于爬取数据，不应出现在本地导入报告中
                    continue
                if obs_id not in found:
                    found[obs_id] = {
                        "report": report.name,
                        "kind": cur_kind,
                        "taxon_id": taxon_id,
                        "obs_id": obs_id,
                        "new_photos": int(m.group(2)),
                    }
                cur_kind = None
                cur_fallback = False
    return list(found.values())


def _v640546a2(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT observation_id, taxon_id, species_guess FROM observations "
        "WHERE observation_id < 0 AND species_guess LIKE ?",
        (f"%{_v8380df01}%",)).fetchall()
    return [{"report": "（数据库扫描：同属分组文件夹）",
             "kind": r[2], "taxon_id": r[1], "obs_id": r[0],
             "new_photos": -1} for r in rows]


def _v6f5fe057() -> int:
    parser = argparse.ArgumentParser(
        description="清理拖入导入时回退误匹配入库的观察与照片")
    parser.add_argument("--db", required=True, help="主库 bird_observations.db 路径")
    parser.add_argument("--raw-db", required=True, help="原始库 raw_data.db 路径")
    parser.add_argument("--reports", required=True, help="导入报告所在目录")
    parser.add_argument("--data-dir", required=True,
                        help="taxon_* 照片目录所在的数据目录")
    parser.add_argument("--apply", action="store_true",
                        help="实际执行删除（缺省为预览模式）")
    args = parser.parse_args()

    db_path = Path(args.db)
    raw_path = Path(args.raw_db)
    reports_dir = Path(args.reports)
    data_dir = Path(args.data_dir)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    raw_conn = sqlite3.connect(raw_path)

    # 两条线索合并：报告解析（回退到分割名）+ 数据库扫描（同属分组文件夹）
    entries = _vaaaec1bf(reports_dir)
    db_entries = _v640546a2(conn)
    merged: dict[int, dict] = {}
    for e in entries + db_entries:
        merged.setdefault(e["obs_id"], e)
    entries = list(merged.values())

    if not entries:
        print("未发现回退误匹配的本地观察记录，无需清理。")
        conn.close()
        raw_conn.close()
        return 0

    print(f"共发现 {len(entries)} 条误匹配的本地观察记录：\n")
    for e in sorted(entries, key=lambda x: x["obs_id"]):
        print(f"  观察 {e['obs_id']}: 「{e['kind']}」→ 误归入 taxon_{e['taxon_id']}"
              f" 来源 {e['report']}")

    mode = "执行删除" if args.apply else "预览模式（未写库，加 --apply 执行）"
    print(f"\n模式：{mode}\n")

    # 逐条核对主库后删除：照片文件 → 照片行 → 关联行 → 观察行
    deleted_files = deleted_photos = deleted_obs = 0
    affected_taxa: set[int] = set()
    # 各类群将被删除的观察数（预览模式下用于模拟删除后计数）
    taxa_deleted_count: dict[int, int] = {}
    for e in entries:
        row = conn.execute(
            "SELECT observation_id, taxon_id FROM observations "
            "WHERE observation_id = ? AND observation_id < 0",
            (e["obs_id"],)).fetchone()
        if row is None:
            print(f"  [跳过] 观察 {e['obs_id']} 不在主库中（可能已清理）")
            continue
        if int(row["taxon_id"]) != e["taxon_id"]:
            # 报告与库不一致时以库为准，同时修正待清理类群
            e["taxon_id"] = int(row["taxon_id"])
        affected_taxa.add(e["taxon_id"])
        taxa_deleted_count[e["taxon_id"]] = (
            taxa_deleted_count.get(e["taxon_id"], 0) + 1)

        photos = conn.execute(
            "SELECT photo_id, local_path FROM photos WHERE observation_id = ?",
            (e["obs_id"],)).fetchall()
        for p in photos:
            rel = p["local_path"] or ""
            if rel:
                # local_path 形如 photos/obs_-213_photo_-4633.jpg，
                # 位于 data_dir/taxon_<ID>_<名称>/ 之下
                for taxon_dir in data_dir.glob(f"taxon_{e['taxon_id']}_*"):
                    target = taxon_dir / rel
                    if target.is_file():
                        if args.apply:
                            target.unlink()
                        deleted_files += 1
                        break
            if args.apply:
                conn.execute("DELETE FROM photos WHERE photo_id = ?",
                             (p["photo_id"],))
            deleted_photos += 1
        if args.apply:
            conn.execute("DELETE FROM batch_observations WHERE observation_id = ?",
                         (e["obs_id"],))
            conn.execute("DELETE FROM observations WHERE observation_id = ?",
                         (e["obs_id"],))
            # 原始层同步清理（本地导入在原始层的留存）
            raw_conn.execute(
                'DELETE FROM raw_observations WHERE "观察记录ID" = ?',
                (e["obs_id"],))
            raw_conn.execute(
                'DELETE FROM raw_photos WHERE "观察记录ID" = ?',
                (e["obs_id"],))
        deleted_obs += 1

    # 清理后再无任何观察的类群：删 taxa / taxon_tree 行与空目录
    removed_taxa: list[int] = []
    for taxon_id in sorted(affected_taxa):
        if args.apply:
            # 已实际删除，直接查库
            remain = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE taxon_id = ?",
                (taxon_id,)).fetchone()[0]
        else:
            # 预览模式：总观察数减去将被删除的观察数
            total = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE taxon_id = ?",
                (taxon_id,)).fetchone()[0]
            remain = total - taxa_deleted_count.get(taxon_id, 0)
        if remain == 0:
            taxon_row = conn.execute(
                "SELECT common_name, scientific_name FROM taxa WHERE taxon_id = ?",
                (taxon_id,)).fetchone()
            name = (f"{taxon_row['common_name']} {taxon_row['scientific_name']}"
                    if taxon_row else f"taxon_{taxon_id}")
            print(f"  类群 {name} (#{taxon_id}) 已无任何观察，删除该类群")
            removed_taxa.append(taxon_id)
            if args.apply:
                conn.execute("DELETE FROM taxa WHERE taxon_id = ?", (taxon_id,))
                conn.execute("DELETE FROM taxon_tree WHERE taxon_id = ?", (taxon_id,))
                for taxon_dir in list(data_dir.glob(f"taxon_{taxon_id}_*")):
                    # 目录内已无照片文件时整个移除（保留非空目录以防误删）
                    if not any(taxon_dir.rglob("photos/*")):
                        shutil.rmtree(taxon_dir, ignore_errors=True)

    if args.apply:
        conn.commit()
        raw_conn.commit()
        print("\n已提交删除。")
    else:
        print("\n预览完成，未改动任何数据。")

    # 前后统计对比
    obs_n = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    photo_n = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    taxa_n = conn.execute("SELECT COUNT(*) FROM taxa").fetchone()[0]
    print(f"\n当前主库统计：观察 {obs_n} 条，照片 {photo_n} 张，类群 {taxa_n} 个")
    print(f"本次清理：删除观察 {deleted_obs} 条，照片行 {deleted_photos} 行，"
          f"照片文件 {deleted_files} 个，空类群 {len(removed_taxa)} 个")

    conn.close()
    raw_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(_v6f5fe057())
