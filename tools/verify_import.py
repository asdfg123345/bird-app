"""本地导入：检查点分批提交的吞吐与「中途失败保留已入库部分」验证。

验证两件事：
  1. 吞吐：每 N 张一次提交 vs 每张一次提交的耗时差异
  2. 语义：中途抛异常后，已过检查点的照片必须仍在库里（不回滚），
     重跑时按内容指纹自动跳过
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from inaturalist_db_manager import INaturalistDatabaseManager  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


N = 300            # 模拟 300 张图
EVERY = 50         # 与 drop_crawl_interface.IMPORT_COMMIT_EVERY 一致


def run_case(tmp, commit_every, fail_at=None):
    """模拟导入 N 张图；commit_every=None 表示每张提交（旧行为）。

    fail_at 非空时在第 fail_at 张抛异常——此时不再做扫尾提交、直接关闭
    （模拟异常上抛到调用方后连接关闭回滚），用于验证「已过检查点的部分保留」。
    """
    db = tmp / ("db_" + str(commit_every) + "_" + str(fail_at))
    mgr = INaturalistDatabaseManager(db / "raw_data.db",
                                     db / "bird_observations.db")
    obs_id = mgr.upsert_local_observation(
        taxon_id=20967, scientific_name="Upupa epops", common_name="Hoopoe",
        source_name="bench")
    t0 = time.perf_counter()
    inserted = 0
    crashed = False
    try:
        for i in range(1, N + 1):
            if fail_at is not None and i == fail_at:
                raise RuntimeError("模拟中断")
            per = (commit_every is None)   # 旧行为：每张提交
            mgr.upsert_local_photo(
                photo_id=-i, observation_id=obs_id,
                content_sha=f"sha_{i:04d}", original_name=f"{i}.jpg",
                dest_relative=f"photos/{i}.jpg", file_size=1000,
                width=600, height=400, position=i, commit=per)
            inserted += 1
            if commit_every is not None and i % commit_every == 0:
                mgr.commit_all()
    except RuntimeError:
        crashed = True                 # 异常上抛：不做扫尾提交
    if commit_every is not None and not crashed:
        mgr.commit_all()
    mgr.close()

    # 重新开库读取，只统计真正落盘的数据（排除未提交事务）
    mgr2 = INaturalistDatabaseManager(db / "raw_data.db",
                                      db / "bird_observations.db")
    n_photos = mgr2.get_photo_count()
    mgr2.close()
    return time.perf_counter() - t0, inserted, n_photos, db, obs_id


tmp = Path(tempfile.mkdtemp(prefix="import_bench_"))
try:
    print("=" * 72)
    print(f"吞吐对比（{N} 张图，WAL）")
    print("=" * 72)
    t_old, ins_old, n_old, _, _ = run_case(tmp, commit_every=None)
    t_new, ins_new, n_new, _, _ = run_case(tmp, commit_every=EVERY)
    print(f"  逐张提交      : {t_old * 1000:8.1f} ms  入库 {n_old}")
    print(f"  每 {EVERY} 张提交 : {t_new * 1000:8.1f} ms  入库 {n_new}")
    speed = t_old / max(t_new, 1e-9)
    print(f"  加速          : {speed:.1f}x")
    check("分批提交后数据完整（无遗漏）", n_new == N, f"{n_new} / {N}")
    check("分批提交带来实质加速", speed > 2.0, f"{speed:.1f}x")

    print()
    print("=" * 72)
    print("中断语义：中途失败应保留已过检查点的数据")
    print("=" * 72)
    fail_at = 130          # 第 130 张中断：已过 2 个检查点（100 张）
    _, ins_partial, n_partial, db, obs_id = run_case(
        tmp, commit_every=EVERY, fail_at=fail_at)
    expected_min = (fail_at - 1) // EVERY * EVERY     # 恰好 100 张已提交
    print(f"  中断于第 {fail_at} 张：已提交检查点 {expected_min} 张，"
          f"库内实际 {n_partial} 张")
    check("中途失败保留已入库部分（未被整体回滚）",
          n_partial >= expected_min, f"库内 {n_partial} >= 检查点 {expected_min}")
    check("中断点之后的照片未入库", n_partial < N, f"库内 {n_partial} < 总数 {N}")

    # 重跑：已入库的应按内容指纹跳过，只补剩余部分
    print()
    print("=" * 72)
    print("重跑续导：按内容指纹跳过已入库，只补增量")
    print("=" * 72)
    mgr = INaturalistDatabaseManager(db / "raw_data.db",
                                     db / "bird_observations.db")
    skipped = 0
    added = 0
    for i in range(1, N + 1):
        if mgr.has_photo_fingerprint(f"sha_{i:04d}"):
            skipped += 1
            continue
        pid = mgr.next_local_photo_id()
        mgr.upsert_local_photo(
            photo_id=pid, observation_id=obs_id, content_sha=f"sha_{i:04d}",
            original_name=f"{i}.jpg", dest_relative=f"photos/{i}.jpg",
            file_size=1000, width=600, height=400, position=i, commit=False)
        added += 1
        if added % EVERY == 0:
            mgr.commit_all()
    mgr.commit_all()
    total_after = mgr.get_photo_count()
    mgr.close()
    print(f"  重跑跳过 {skipped} 张（已在库），新补 {added} 张，"
          f"最终 {total_after} 张")
    check("重跑只补增量（跳过已入库）", skipped >= expected_min and added > 0,
          f"跳过 {skipped} / 新补 {added}")
    check("重跑后总量正确", total_after == N, f"{total_after} / {N}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

fails = [r for r in results if not r[1]]
print()
print("=" * 72)
print(f"结果：{len(results) - len(fails)}/{len(results)} 通过")
for n, _, d in fails:
    print(f"  FAIL {n}  {d}")
print("=" * 72)
sys.exit(1 if fails else 0)
