# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================
"""数据库健康检查与自愈模块。

背景（2026-10-16 全量核查结论）：
    数据库本身无虚假记录，但存在若干"多余存储"与残留问题：
    1. 下载中断产生的 .part 残留文件；
    2. taxon 目录中存在但未入库的散图（磁盘有文件、库无记录）；
    3. 个别无照片的观察记录（库有记录、无照片支撑）；
    4. 空批次历史记录（total_photos=0 等无意义批次）；
    5. 跨类群同名照片文件导致的文件名索引歧义；
    6. 需要持续监控的重复指纹 / 两库不一致风险。

本模块提供 8 项检查与对应的安全修复（自愈）能力：
    检查（只读，不改动任何数据）：
        C1 .part 残留文件
        C2 taxon 目录未入库散图
        C3 无照片的观察记录 / 无观察的照片 / 无观察的类群
        C4 空批次记录
        C5 重复照片指纹
        C6 照片路径可解析性（库内路径能否对应实际文件）
        C7 跨类群同名文件歧义
        C8 主库与原始库照片记录一致性
    修复（apply，执行前自动整库备份）：
        R1 删除 .part 残留文件
        R2 散图移动至 data/orphan_pending/<时间戳>/ 待人工处理
           （不自动入库，避免历史上"回退层级名误导入"同类风险）
        R3 删除无照片的观察记录（主库 + 原始库同步删除）
        R4 删除空批次记录及其关联行
        C5/C7 仅报告不自动处理，C6 缺失文件仅报告。

用法：
    只检查（默认）：python db_health_check.py
    检查+修复：python db_health_check.py --apply
    自定义路径：
        python db_health_check.py --data-dir D:\\...\\data \\
            --db 主库.db --raw-db 原始库.db
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# 图片扩展名集合（与 bird_crawler_gui 的照片索引保持一致）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@dataclass
class HealthReport:
    """健康检查结果报告（检查项 -> 明细列表）。"""
    issues: dict[str, list[str]] = field(default_factory=dict)
    stats: list[str] = field(default_factory=list)

    def add(self, key: str, line: str) -> None:
        """追加一条问题明细。"""
        self.issues.setdefault(key, []).append(line)

    def add_stat(self, line: str) -> None:
        """追加一条统计信息（非问题）。"""
        self.stats.append(line)

    @property
    def total_issues(self) -> int:
        """问题明细总条数。"""
        return sum(len(v) for v in self.issues.values())

    def render(self) -> str:
        """渲染为可读文本报告。"""
        names = {
            "C1_part_residue": "C1 下载残留 .part 文件",
            "C2_orphan_images": "C2 taxon 目录未入库散图",
            "C3_db_orphans": "C3 库内孤儿记录（无照片观察/无观察照片/无观察类群）",
            "C4_empty_batches": "C4 空批次记录",
            "C5_dup_fingerprints": "C5 重复照片指纹",
            "C6_missing_files": "C6 路径无法解析到实际文件",
            "C7_name_collision": "C7 跨类群同名文件歧义",
            "C8_db_inconsistency": "C8 主库与原始库不一致",
        }
        lines = ["=== 数据库健康检查报告 ==="]
        lines += [f"  {s}" for s in self.stats]
        if not self.issues:
            lines.append("\n结论：全部检查项通过，未发现问题。")
            return "\n".join(lines)
        lines.append("")
        for key, title in names.items():
            items = self.issues.get(key)
            if not items:
                continue
            lines.append(f"--- {title}（{len(items)} 项）---")
            show = items[:20]
            lines += [f"  {x}" for x in show]
            if len(items) > len(show):
                lines.append(f"  ……其余 {len(items) - len(show)} 项略")
        lines.append(f"\n结论：共发现 {self.total_issues} 项问题，"
                     "可执行 --apply 进行安全修复（C5/C6/C7 仅报告）。")
        return "\n".join(lines)


def _connect_ro(db: Path) -> sqlite3.Connection:
    """只读方式连接数据库（检查阶段绝不写库）。"""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _iter_taxon_image_files(data_dir: Path):
    """遍历所有 taxon_* 目录下的图片文件（yield 路径）。"""
    for taxon in data_dir.glob("taxon_*"):
        if not taxon.is_dir():
            continue
        for item in taxon.rglob("*"):
            if item.is_file() and item.suffix.lower() in IMAGE_EXTS:
                yield item


def run_checks(data_dir: Path, main_db: Path, raw_db: Path) -> HealthReport:
    """执行全部 8 项检查（只读，无任何写入）。"""
    rep = HealthReport()
    t0 = time.time()
    rep.add_stat(f"数据目录: {data_dir}")
    rep.add_stat(f"主库: {main_db}")
    rep.add_stat(f"原始库: {raw_db}")

    con = _connect_ro(main_db)
    con.execute("ATTACH DATABASE ? AS rawdb", (str(raw_db),))

    # ---- C1: .part 残留文件 ----
    part_files = sorted(data_dir.glob("taxon_*/**/*.part"))
    for p in part_files:
        rep.add("C1_part_residue", str(p.relative_to(data_dir)))

    # ---- 引用文件名集合（供 C2/C7 复用）----
    ref_names: set[str] = set()
    for (lp,) in con.execute("SELECT local_path FROM photos WHERE local_path != ''"):
        ref_names.add(Path(lp).name)

    # ---- 一次性构建 磁盘文件名 -> 类群目录集合 映射（供 C2/C6/C7 复用）----
    name_to_taxons: dict[str, set[str]] = defaultdict(set)
    for f in _iter_taxon_image_files(data_dir):
        name_to_taxons[f.name].add(f.parent.parent.name)

    # ---- C2: taxon 目录未入库散图 ----
    for f in _iter_taxon_image_files(data_dir):
        if f.name not in ref_names:
            rep.add("C2_orphan_images", str(f.relative_to(data_dir)))

    # ---- C3: 库内孤儿记录 ----
    for r in con.execute(
            """SELECT observation_id, scientific_name, common_name FROM observations o
               WHERE NOT EXISTS (SELECT 1 FROM photos p
                                 WHERE p.observation_id = o.observation_id)"""):
        rep.add("C3_db_orphans",
                f"无照片观察 observation_id={r['observation_id']} "
                f"({r['scientific_name']})")
    n_photo_no_obs = con.execute(
        """SELECT COUNT(*) FROM photos p WHERE p.observation_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM observations o
                           WHERE o.observation_id = p.observation_id)""").fetchone()[0]
    if n_photo_no_obs:
        rep.add("C3_db_orphans", f"无对应观察的照片记录 {n_photo_no_obs} 条")
    for r in con.execute(
            """SELECT taxon_id, scientific_name FROM taxa t
               WHERE NOT EXISTS (SELECT 1 FROM observations o
                                 WHERE o.taxon_id = t.taxon_id)"""):
        rep.add("C3_db_orphans",
                f"无观察类群 taxon_id={r['taxon_id']} ({r['scientific_name']})")

    # ---- C4: 空批次记录（无照片、无观察、无下载量、且无关联行）----
    for r in con.execute(
            """SELECT id, batch_time FROM batch_runs b
               WHERE COALESCE(total_photos,0)=0 AND COALESCE(total_observations,0)=0
                 AND COALESCE(total_downloaded,0)=0
                 AND NOT EXISTS (SELECT 1 FROM batch_observations bo
                                 WHERE bo.batch_id=b.id)
                 AND NOT EXISTS (SELECT 1 FROM batch_taxa bt
                                 WHERE bt.batch_id=b.id)"""):
        rep.add("C4_empty_batches",
                f"空批次 id={r['id']} time={r['batch_time']}")

    # ---- C5: 重复照片指纹 ----
    for r in con.execute(
            """SELECT fingerprint, COUNT(*) c FROM photos
               GROUP BY fingerprint HAVING c > 1 ORDER BY c DESC LIMIT 50"""):
        rep.add("C5_dup_fingerprints",
                f"指纹 {r['fingerprint'][:16]}… 重复 {r['c']} 次")

    # ---- C6: 路径可解析性（文件名在数据目录中是否存在）----
    # 说明：与 GUI 的文件名索引逻辑一致，按文件名在 taxon 目录中定位。
    n_missing = 0
    missing_samples: list[str] = []
    for (lp,) in con.execute(
            "SELECT local_path FROM photos WHERE download_ok=1 AND local_path != ''"):
        if Path(lp).name not in name_to_taxons:
            n_missing += 1
            if len(missing_samples) < 10:
                missing_samples.append(lp)
    for lp in missing_samples:
        rep.add("C6_missing_files", lp)
    if n_missing > len(missing_samples):
        rep.add("C6_missing_files", f"……其余 {n_missing - len(missing_samples)} 项略")

    # ---- C7: 跨类群同名文件歧义 ----
    for name, taxons in sorted(name_to_taxons.items()):
        if len(taxons) > 1:
            rep.add("C7_name_collision", f"{name} 同时存在于 {sorted(taxons)}")

    # ---- C8: 主库与原始库一致性 ----
    n_main = con.execute("SELECT COUNT(*) FROM main.photos").fetchone()[0]
    n_raw = con.execute("SELECT COUNT(*) FROM rawdb.raw_photos").fetchone()[0]
    rep.add_stat(f"照片记录数: 主库 {n_main} / 原始库 {n_raw}")
    if n_main != n_raw:
        rep.add("C8_db_inconsistency",
                f"两库照片记录数不一致: 主库 {n_main} vs 原始库 {n_raw}")
    n_match = con.execute(
        """SELECT COUNT(*) FROM main.photos p WHERE EXISTS (
               SELECT 1 FROM rawdb.raw_photos r
               WHERE r.照片ID = p.photo_id AND r.本地路径 = p.local_path)""").fetchone()[0]
    if n_match != n_main:
        rep.add("C8_db_inconsistency",
                f"两库照片 ID/路径均一致仅 {n_match}/{n_main} 条")

    con.close()
    rep.add_stat(f"检查耗时: {time.time() - t0:.1f} 秒")
    return rep


def _backup(db: Path) -> Path:
    """修复前整库备份（同目录 backup 子目录，带时间戳）。"""
    bdir = db.parent / "backup"
    bdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = bdir / f"{db.stem}_{stamp}{db.suffix}"
    shutil.copy2(db, dest)
    return dest


def apply_fixes(data_dir: Path, main_db: Path, raw_db: Path,
                rep: HealthReport) -> list[str]:
    """按检查报告执行安全修复（返回修复动作日志）。

    修复范围：R1 删 .part / R2 散图移入待处理区 / R3 删无照片观察 /
    R4 删空批次。C5/C6/C7 仅报告项不自动处理。
    """
    logs: list[str] = []
    # ---- 修复前整库备份 ----
    for db in (main_db, raw_db):
        dest = _backup(db)
        logs.append(f"备份 {db.name} -> {dest.name}")

    # ---- R1: 删除 .part 残留 ----
    n_part = 0
    for p in data_dir.glob("taxon_*/**/*.part"):
        p.unlink()
        n_part += 1
    logs.append(f"R1 已删除 .part 残留文件 {n_part} 个")

    # ---- R2: 散图移动至待处理区（不自动入库）----
    ref_names: set[str] = set()
    con = sqlite3.connect(main_db)
    for (lp,) in con.execute("SELECT local_path FROM photos WHERE local_path != ''"):
        ref_names.add(Path(lp).name)
    pending = data_dir / "orphan_pending" / time.strftime("%Y%m%d_%H%M%S")
    # 先收集再移动，避免遍历过程中移动文件导致目录遍历异常
    orphans = [f for f in _iter_taxon_image_files(data_dir)
               if f.name not in ref_names]
    n_moved = 0
    for f in orphans:
        pending.mkdir(parents=True, exist_ok=True)
        # 目标名加类群前缀，避免多类群散图同名覆盖
        dest = pending / f"{f.parent.parent.name}_{f.name}"
        shutil.move(str(f), dest)
        n_moved += 1
    logs.append(f"R2 未入库散图已移至 {pending}（共 {n_moved} 个，待人工确认）")

    # ---- R3: 删除无照片的观察记录（主库 + 原始库 + 关联清理）----
    rows = con.execute(
        """SELECT observation_id, taxon_id FROM observations o
           WHERE NOT EXISTS (SELECT 1 FROM photos p
                             WHERE p.observation_id = o.observation_id)""").fetchall()
    obs_ids = [r[0] for r in rows]
    affected_taxa = {r[1] for r in rows if r[1] is not None}
    n_taxa = 0  # 无删除时也为 0，避免日志引用未赋值变量
    if obs_ids:
        ph = ",".join("?" * len(obs_ids))
        # 原始库同步删除（观察与照片留存行）
        con.execute("ATTACH DATABASE ? AS rawdb", (str(raw_db),))
        con.execute(f"DELETE FROM rawdb.raw_observations WHERE 观察记录ID IN ({ph})", obs_ids)
        con.execute(f"DELETE FROM rawdb.raw_photos WHERE 观察记录ID IN ({ph})", obs_ids)
        con.execute(f"DELETE FROM batch_observations WHERE observation_id IN ({ph})", obs_ids)
        con.execute(f"DELETE FROM observations WHERE observation_id IN ({ph})", obs_ids)
        # 同步清理"因本次删除而变空"的类群（分类树 taxa 与 taxon_tree 一并删除，
        # 避免复查时出现新的无观察类群；原本就无观察的类群不受影响）
        for tid in affected_taxa:
            left = con.execute(
                "SELECT COUNT(*) FROM observations WHERE taxon_id = ?", (tid,)).fetchone()[0]
            if left == 0:
                con.execute("DELETE FROM taxa WHERE taxon_id = ?", (tid,))
                con.execute("DELETE FROM taxon_tree WHERE taxon_id = ?", (tid,))
                n_taxa += 1
        con.commit()
    logs.append(f"R3 已删除无照片的观察记录 {len(obs_ids)} 条（主库+原始库同步），"
                f"并清理因此变空的类群 {n_taxa} 条")

    # ---- R4: 删除空批次及其关联行 ----
    empty_ids = [r[0] for r in con.execute(
        """SELECT b.id FROM batch_runs b
           WHERE COALESCE(total_photos,0)=0 AND COALESCE(total_observations,0)=0
             AND COALESCE(total_downloaded,0)=0
             AND NOT EXISTS (SELECT 1 FROM batch_observations bo WHERE bo.batch_id=b.id)
             AND NOT EXISTS (SELECT 1 FROM batch_taxa bt WHERE bt.batch_id=b.id)""")]
    if empty_ids:
        ph = ",".join("?" * len(empty_ids))
        con.execute(f"DELETE FROM batch_observations WHERE batch_id IN ({ph})", empty_ids)
        con.execute(f"DELETE FROM batch_taxa WHERE batch_id IN ({ph})", empty_ids)
        con.execute(f"DELETE FROM batch_runs WHERE id IN ({ph})", empty_ids)
        con.commit()
    logs.append(f"R4 已删除空批次记录 {len(empty_ids)} 条")

    con.close()
    return logs


def _default_paths() -> tuple[Path, Path, Path]:
    """从脚本位置推导默认路径（不硬编码绝对路径）。"""
    base = Path(__file__).resolve().parent
    data_dir = base / "data"
    db_dir = data_dir / "db"
    return data_dir, db_dir / "bird_observations.db", db_dir / "raw_data.db"


def main(argv: list[str] | None = None) -> int:
    """命令行入口：--check 只检查，--apply 检查并修复。"""
    parser = argparse.ArgumentParser(description="数据库健康检查与自愈")
    parser.add_argument("--apply", action="store_true", help="执行安全修复（默认只检查）")
    parser.add_argument("--data-dir", help="数据目录（默认：脚本所在目录/data）")
    parser.add_argument("--db", help="主库路径（默认：data/db/bird_observations.db）")
    parser.add_argument("--raw-db", help="原始库路径（默认：data/db/raw_data.db）")
    args = parser.parse_args(argv)

    d_data, d_main, d_raw = _default_paths()
    data_dir = Path(args.data_dir) if args.data_dir else d_data
    main_db = Path(args.db) if args.db else d_main
    raw_db = Path(args.raw_db) if args.raw_db else d_raw

    if not main_db.exists() or not raw_db.exists():
        print(f"数据库不存在: {main_db} / {raw_db}", file=sys.stderr)
        return 2

    rep = run_checks(data_dir, main_db, raw_db)
    print(rep.render())

    if args.apply and rep.issues:
        print("\n=== 执行修复 ===")
        for line in apply_fixes(data_dir, main_db, raw_db, rep):
            print(f"  {line}")
        print("修复完成，复查结果：")
        rep2 = run_checks(data_dir, main_db, raw_db)
        print(rep2.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
