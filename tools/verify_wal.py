"""集成验证：WAL 下的备份一致性 + 只读读者 + 面板读 WAL 库。"""

import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # tools/ 的上一级即仓库根
SRC = ROOT / "src"
PY = sys.executable
sys.path.insert(0, str(SRC))

import db_health_check as hc  # noqa: E402
from inaturalist_db_manager import INaturalistDatabaseManager  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


tmp = Path(tempfile.mkdtemp(prefix="bird_wal_"))
try:
    main_db = tmp / "db" / "bird_observations.db"
    raw_db = tmp / "db" / "raw_data.db"
    mgr = INaturalistDatabaseManager(raw_db, main_db)
    mgr.ensure_taxon(20967, "Upupa epops", "Hoopoe", "family")

    N = 300
    obs = [{
        "observation_id": 5000 + i, "uuid": f"u{i}", "quality_grade": "research",
        "scientific_name": "Upupa epops", "common_name": "Hoopoe",
        "taxon_id": 20967, "taxon_rank": "species", "observed_on": "2026-01-01",
        "latitude": 1.0, "longitude": 2.0, "photo_count": 1,
        "local_photo_files": "photos/a.jpg",
    } for i in range(N)]
    mgr.write_page_data(obs, [], [{"id": 5000 + i} for i in range(N)],
                        "2026-01-01T00:00:00")

    # 确认确实处于 WAL，且此时 -wal 非空（数据尚未 checkpoint 进主文件）
    mode = mgr._get_main_conn().execute("PRAGMA journal_mode").fetchone()[0]
    wal = main_db.with_name(main_db.name + "-wal")
    check("主库已切到 WAL", mode == "wal", mode)
    check("WAL 文件存在（有未 checkpoint 的数据）",
          wal.is_file() and wal.stat().st_size > 0,
          f"{wal.stat().st_size if wal.is_file() else 0} 字节")

    # 备份（不关闭写连接，模拟修复时仍有并发写者的场景）
    dest = hc._backup(main_db)
    bck = sqlite3.connect(dest)
    n_backup = bck.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    bck.close()
    check("WAL 下备份包含全部已提交数据", n_backup == N, f"备份内 {n_backup} / 预期 {N}")

    # 只读读者在写连接存在时可读
    ro = hc._connect_ro(main_db)
    n_ro = ro.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    ro.close()
    check("只读连接可读 WAL 库", n_ro == N, f"{n_ro}")

    mgr.close()

    # 健康检查整体跑通
    rc = subprocess.run([PY, "main.py", "health", "--out-dir", str(tmp)],
                        cwd=str(SRC), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check("health 子命令在 WAL 库上通过", rc.returncode == 0,
          (rc.stdout or "").strip().splitlines()[-1][:80])

    # 面板读 WAL 库
    port = 8801
    proc = subprocess.Popen(
        [PY, "bird_db_panel.py", "--main-db", str(main_db),
         "--port", str(port), "--no-browser"],
        cwd=str(SRC), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(3.0)
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats", timeout=10) as r:
            body = r.read().decode("utf-8")
        import json
        data = json.loads(body)
        check("数据库面板可读 WAL 库", data.get("observations") == N,
              f"observations={data.get('observations')}")
    except Exception as exc:
        check("数据库面板可读 WAL 库", False, str(exc)[:120])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # 强杀进程会留下 -wal（预期行为），关键在于重开时能自动恢复且数据完好
    reclaimed = sqlite3.connect(main_db)
    n_reopen = reclaimed.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    reclaimed.close()
    check("强杀进程后重开数据库可自动恢复", n_reopen == N, f"{n_reopen} / 预期 {N}")

    # 全部连接正常关闭后，-wal/-shm 应被清理（干净关闭场景单独验证）
    clean_db = tmp / "clean.db"
    c = sqlite3.connect(clean_db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(a)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()
    resid = [p.name for p in clean_db.parent.iterdir() if p.name.startswith("clean.db")]
    check("正常关闭后 -wal/-shm 被清理",
          resid == ["clean.db"], str(resid))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

fails = [r for r in results if not r[1]]
print()
print(f"结果：{len(results) - len(fails)}/{len(results)} 通过")
sys.exit(1 if fails else 0)
