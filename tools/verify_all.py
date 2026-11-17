"""回归基线脚本：改动前后各跑一次，用于证明业务逻辑未回归。

用法：
    python _verify.py            # 全量
    python _verify.py quick      # 跳过 dataset 导出
"""

import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # tools/ 的上一级即仓库根
SRC = ROOT / "src"
PY = sys.executable

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def run(args, cwd=SRC, timeout=180):
    return subprocess.run([PY, *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="full")
    mode = ap.parse_args().mode

    print("=" * 68)
    print("1) 语法编译检查（src 全部 .py）")
    print("=" * 68)
    rc = subprocess.run([PY, "-m", "compileall", "-q", str(SRC)],
                        capture_output=True, text=True)
    check("compileall src/", rc.returncode == 0, (rc.stderr or "").strip()[:200])

    print()
    print("=" * 68)
    print("2) 关键模块导入")
    print("=" * 68)
    mods = ["download_inaturalist", "inaturalist_db_initializer",
            "inaturalist_db_manager", "inaturalist_db_migrator",
            "batch_download_birds", "bird_db_panel", "db_health_check",
            "dataset_exporter", "dataset_registry", "training_dataset_importer",
            "cleanup_misimported_observations", "model_library",
            "drop_crawl_interface", "bird_crawler_gui", "model_trainer"]
    for m in mods:
        rc = run(["-c", f"import {m}"])
        check(f"import {m}", rc.returncode == 0,
              (rc.stderr or "").strip().splitlines()[-1][:120] if rc.returncode else "")

    print()
    print("=" * 68)
    print("3) 数据库全链路：init-db → 写入 → 去重 → stats → health")
    print("=" * 68)
    tmp = Path(tempfile.mkdtemp(prefix="bird_verify_"))
    try:
        rc = run(["main.py", "init-db", "--out-dir", str(tmp)])
        check("init-db", rc.returncode == 0, (rc.stderr or "").strip()[:160])

        probe = f"""
import sys
sys.path.insert(0, r"{SRC}")
from inaturalist_db_manager import INaturalistDatabaseManager
from pathlib import Path
tmp = Path(r"{tmp}")
m = INaturalistDatabaseManager(tmp/'db'/'raw_data.db', tmp/'db'/'bird_observations.db')
m.ensure_taxon(20967, 'Upupa epops', 'Hoopoe', 'family')
bid = m.create_batch_run(taxa_requested=1)
# 造真实照片文件，供 dataset 导出的硬链接与校验走通
photo_dir = tmp/'taxon_20967_Upupa_epops_Hoopoe'/'photos'
photo_dir.mkdir(parents=True, exist_ok=True)
try:
    from PIL import Image
    for i in range(5):
        Image.new('RGB', (800, 600), (120, 160, 200)).save(
            photo_dir/f'obs_{{1000+i}}_photo_{{2000+i}}.jpg')
except ImportError:
    for i in range(5):
        (photo_dir/f'obs_{{1000+i}}_photo_{{2000+i}}.jpg').write_bytes(b'\\xff\\xd8\\xff')
obs = [{{'observation_id': 1000+i, 'uuid': f'u{{i}}', 'quality_grade': 'research',
        'scientific_name': 'Upupa epops', 'common_name': 'Hoopoe', 'taxon_id': 20967,
        'taxon_rank': 'species', 'observed_on': '2026-01-01', 'latitude': 1.0,
        'longitude': 2.0, 'photo_count': 1,
        'local_photo_files': f'photos/obs_{{1000+i}}_photo_{{2000+i}}.jpg'}}
       for i in range(5)]
pho = [{{'photo_id': 2000+i, 'observation_id': 1000+i, 'position': 0,
        'license_code': 'cc-by', 'width': 800, 'height': 600,
        'original_url': f'https://x/{{i}}.jpg',
        'local_path': f'photos/obs_{{1000+i}}_photo_{{2000+i}}.jpg',
        'download_ok': 1, 'requested_size': 'large', 'downloaded_size': 'large'}}
       for i in range(5)]
m.write_page_data(obs, pho, [{{'id': 1000+i}} for i in range(5)], '2026-01-01T00:00:00')
print('OBS', m.get_observation_count())
print('PHO', m.get_photo_count())
# 幂等性：再写一次不应增加
m.write_page_data(obs, pho, [{{'id': 1000+i}} for i in range(5)], '2026-01-01T00:00:01')
print('OBS2', m.get_observation_count())
print('PHO2', m.get_photo_count())
# 批次关联（批量接口）
n = m.link_batch_observations_bulk(bid, [o['observation_id'] for o in obs])
print('LINK', n)
m.close()
"""
        rc = run(["-c", probe])
        out = (rc.stdout or "")
        check("写入 5 观察 + 5 照片", "OBS 5" in out and "PHO 5" in out, out.strip()[:200])
        check("重复写入幂等（upsert 不增行）",
              "OBS2 5" in out and "PHO2 5" in out, out.strip()[:200])
        check("批量批次关联 link_batch_observations_bulk",
              "LINK 5" in out, out.strip()[:200])

        rc = run(["main.py", "stats", "--out-dir", str(tmp)])
        check("stats 子命令", rc.returncode == 0 and "观察记录总数: 5" in (rc.stdout or ""),
              (rc.stdout or "").strip().splitlines()[:3])

        rc = run(["main.py", "health", "--out-dir", str(tmp)])
        check("health 子命令", rc.returncode == 0, (rc.stdout or "").strip()[:160])

        if mode != "quick":
            ds = tmp / "ds"
            rc = run(["main.py", "dataset", "--out", str(ds), "--out-dir", str(tmp),
                      "--min-per-class", "0", "--no-verify"])
            man = ds / "manifest.csv"
            ok = rc.returncode == 0 and man.is_file()
            splits = {}
            if ok:
                import csv as _csv
                with man.open(encoding="utf-8-sig") as fh:
                    for row in _csv.DictReader(fh):
                        splits[row["split"]] = splits.get(row["split"], 0) + 1
            check("dataset 导出（5 张照片 -> train/val/test）", ok,
                  f"manifest 分布 {splits}" if ok else (rc.stdout or "").strip()[:160])
            # 观察级防泄漏：同一 observation 不应跨 split
            leak = False
            if ok:
                obs_splits = {}
                with man.open(encoding="utf-8-sig") as fh:
                    for row in _csv.DictReader(fh):
                        obs_splits.setdefault(row["observation_id"], set()).add(row["split"])
                leak = any(len(v) > 1 for v in obs_splits.values())
            check("观察级划分无泄漏（同一观察不跨集合）", ok and not leak)
            check("manifest 必备文件齐全",
                  ok and (ds / "manifest.jsonl").is_file()
                  and (ds / "report.md").is_file()
                  and (ds / "train_example.py").is_file())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 68)
    print("4) dev/tests 既有测试")
    print("=" * 68)
    dev = ROOT / "dev"
    if dev.is_dir():
        rc = run(["-m", "unittest", "tests.test_model_library"], cwd=dev)
        check("unittest tests.test_model_library", rc.returncode == 0,
              (rc.stderr or "").strip().splitlines()[-1][:120] if rc.returncode else "")
        rc = run(["-m", "pytest", "tests/test_trainer_perf.py", "-q"], cwd=dev)
        check("pytest tests/test_trainer_perf.py",
              rc.returncode == 0 and "passed" in (rc.stdout or ""),
              (rc.stdout or "").strip().splitlines()[-1][:80] if rc.returncode == 0 else "")

    print()
    fails = [r for r in results if not r[1]]
    print("=" * 68)
    print(f"结果：{len(results) - len(fails)}/{len(results)} 通过")
    if fails:
        print("失败项：")
        for n, _, d in fails:
            print(f"  - {n}  {d}")
    print("=" * 68)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
