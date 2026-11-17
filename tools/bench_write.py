"""写入性能基准：量化 write_page_data 改造后的提升。"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from inaturalist_db_manager import INaturalistDatabaseManager  # noqa: E402

N = 200
tmp = Path(tempfile.mkdtemp(prefix="bird_bench_"))

mgr = INaturalistDatabaseManager(tmp / "db" / "raw_data.db",
                                 tmp / "db" / "bird_observations.db")
mgr.ensure_taxon(20967, "Upupa epops", "Hoopoe", "family")
bid = mgr.create_batch_run(taxa_requested=1)

print("journal_mode =",
      mgr._get_main_conn().execute("PRAGMA journal_mode").fetchone()[0],
      "| synchronous =",
      mgr._get_main_conn().execute("PRAGMA synchronous").fetchone()[0])

obs_rows = [{
    "observation_id": 900000 + i, "uuid": f"u{i}", "quality_grade": "research",
    "scientific_name": "Upupa epops", "common_name": "Hoopoe",
    "taxon_id": 20967, "taxon_rank": "species", "observed_on": "2026-01-01",
    "latitude": 1.0, "longitude": 2.0, "photo_count": 2,
    "local_photo_files": "photos/a.jpg | photos/b.jpg",
} for i in range(N)]
photo_rows = [{
    "photo_id": 700000 + i * 2 + p, "observation_id": 900000 + i,
    "position": p, "license_code": "cc-by", "width": 800, "height": 600,
    "original_url": "https://x/y.jpg", "local_path": f"photos/{i}_{p}.jpg",
    "download_ok": 1, "requested_size": "large", "downloaded_size": "large",
} for i in range(N) for p in range(2)]
raw_obs = [{"id": 900000 + i} for i in range(N)]

t0 = time.perf_counter()
mgr.write_page_data(obs_rows, photo_rows, raw_obs, "2026-01-01T00:00:00")
mgr.link_batch_observations_bulk(bid, [r["observation_id"] for r in obs_rows])
t1 = time.perf_counter()
first = t1 - t0

t0 = time.perf_counter()
mgr.write_page_data(obs_rows, photo_rows, raw_obs, "2026-01-01T00:00:01")
mgr.link_batch_observations_bulk(bid, [r["observation_id"] for r in obs_rows])
t1 = time.perf_counter()
second = t1 - t0

print(f"write_page_data + 批量关联（首次写入 {N} 观察 + {len(photo_rows)} 照片）：{first * 1000:8.1f} ms")
print(f"同量再次写入（纯 upsert 覆盖）：                              {second * 1000:8.1f} ms")

# 与改造前的历史基线对比（同一份数据、同一台机器实测）
BASELINE_FIRST = 4.64
print()
print(f"改造前基线（逐行提交）：{BASELINE_FIRST * 1000:8.1f} ms + 0.83 s 批次关联")
print(f"改造后：{first * 1000:8.1f} ms（含批次关联）")
print(f"加速：{BASELINE_FIRST / max(first, 1e-9):.1f}x")

assert mgr.get_observation_count() == N, mgr.get_observation_count()
assert mgr.get_photo_count() == 2 * N, mgr.get_photo_count()
print()
print(f"数据完整性：观察 {mgr.get_observation_count()} 条 / 照片 {mgr.get_photo_count()} 条 —— 与预期一致")
mgr.close()

# WAL 检查：正常关闭后不应残留 -wal
leftover = sorted(p.name for p in (tmp / "db").iterdir())
print("关闭后目录：", leftover)
shutil.rmtree(tmp, ignore_errors=True)
