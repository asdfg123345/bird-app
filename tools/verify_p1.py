"""P1 正确性隐患专项验证。

覆盖：
  P1-1 download_inaturalist 的 resolved_raw_db 跨分支引用
  P1-2 dataset_exporter 的 split 循环变量覆盖 + id(row) 字典键
  P1-3 socket.getaddrinfo 的 import 期全局 monkeypatch
  P1-4 关键路径静默吞异常改为留痕
"""

import shutil
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


# ======================================================================
# P1-3 先测：模块导入不得改变 socket.getaddrinfo
# ======================================================================
print("=" * 70)
print("P1-3 import 期全局 DNS 副作用")
print("=" * 70)

import download_inaturalist as di  # noqa: E402

check("导入模块后 getaddrinfo 未被替换",
      socket.getaddrinfo is di._orig_getaddrinfo,
      f"当前={getattr(socket.getaddrinfo, '__name__', '?')}")
check("prefer_ipv4_resolution 初始未启用", di._ipv4_preference_applied is False)

enabled = di.prefer_ipv4_resolution()
check("显式调用后启用 IPv4 优先", enabled is True and socket.getaddrinfo is di._getaddrinfo_ipv4)
# 幂等
check("重复调用幂等", di.prefer_ipv4_resolution() is True)

# 验证包装函数本身行为：把 IPv4 记录排到前面/只留 IPv4
_real = di._orig_getaddrinfo
try:
    di._orig_getaddrinfo = lambda *a, **k: [
        (socket.AF_INET6, 1, 6, "", ("::1", 1, 0, 0)),
        (socket.AF_INET, 2, 6, "", ("127.0.0.1", 1)),
    ]
    fams = [r[0] for r in di._getaddrinfo_ipv4("x", 1)]
    check("IPv4 优先解析仅返回 AF_INET 记录",
          fams == [socket.AF_INET], str(fams))
    # 全 IPv6 时回退原始结果，不能返回空
    di._orig_getaddrinfo = lambda *a, **k: [
        (socket.AF_INET6, 1, 6, "", ("::1", 1, 0, 0)),
    ]
    fams2 = [r[0] for r in di._getaddrinfo_ipv4("x", 1)]
    check("无 IPv4 时回退原始结果（不返回空）",
          fams2 == [socket.AF_INET6], str(fams2))
finally:
    di._orig_getaddrinfo = _real


# ======================================================================
# P1-1 resolved_raw_db 跨分支引用
# ======================================================================
print()
print("=" * 70)
print("P1-1 resolved_raw_db 跨分支引用")
print("=" * 70)

import json  # noqa: E402
import types  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="p1_"))
try:
    # 用假 ApiClient 完全离线跑 download_taxon_data：
    # db_enabled=False 时旧代码会在末尾组装 summary 处抛 NameError
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def get_json(self, url, params=None):
            if "/taxa/" in url:
                return {"results": [{"id": 20967, "name": "Upupa epops",
                                     "preferred_common_name": "戴胜",
                                     "rank": "species", "ancestry": "48460/1/2"}]}
            if "/observations" in url:
                return {"total_results": 0, "results": []}
            return {"results": []}

    orig_client = di.ApiClient
    orig_log = di.log
    di.ApiClient = _FakeClient
    di.log = lambda *a, **k: None
    try:
        out_no_db = tmp / "no_db"
        s1 = di.download_taxon_data(
            taxon_id=20967, out_dir=out_no_db, db_enabled=False)
        check("db_enabled=False 不再抛 NameError", isinstance(s1, dict),
              f"observation_count={s1.get('observation_count')}")
        check("db_enabled=False 时不附带数据库路径",
              "raw_db_path" not in s1 and "main_db_path" not in s1)

        out_db = tmp / "with_db"
        s2 = di.download_taxon_data(
            taxon_id=20967, out_dir=out_db, db_enabled=True)
        check("db_enabled=True 正常写入并返回数据库路径",
              s2.get("raw_db_path", "").endswith("raw_data.db")
              and s2.get("main_db_path", "").endswith("bird_observations.db"),
              f"{Path(s2.get('raw_db_path','')).name}/{Path(s2.get('main_db_path','')).name}")
        check("数据库文件已创建",
              (out_db / "db" / "bird_observations.db").is_file(),
              str(out_db / "db"))

        # 显式传路径且 db_enabled=False 也不能因路径未解析而失败
        s3 = di.download_taxon_data(
            taxon_id=20967, out_dir=tmp / "explicit",
            db_enabled=False,
            raw_db_path=tmp / "custom_raw.db",
            main_db_path=tmp / "custom_main.db")
        check("显式传库路径 + 禁用数据库也不报错", isinstance(s3, dict))
    finally:
        di.ApiClient = orig_client
        di.log = orig_log

    # ==================================================================
    # P1-2 dataset_exporter 划分变量
    # ==================================================================
    print()
    print("=" * 70)
    print("P1-2 dataset_exporter 划分变量覆盖与 id() 键")
    print("=" * 70)

    import argparse  # noqa: E402
    import csv  # noqa: E402

    from PIL import Image  # noqa: E402
    from dataset_exporter import export_dataset  # noqa: E402
    from inaturalist_db_manager import INaturalistDatabaseManager  # noqa: E402

    ds_tmp = tmp / "ds_case"
    mgr = INaturalistDatabaseManager(ds_tmp / "db" / "raw_data.db",
                                     ds_tmp / "db" / "bird_observations.db")
    mgr.ensure_taxon(12345, "Testus avis", "测试鸟", "family")
    # 单个观察配 5 张照片：观察级划分必然使 val 为空，触发「图片级兜底」路径
    photo_dir = ds_tmp / "taxon_12345_Testus_avis_测试鸟" / "photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    obs_id = 777
    photos = []
    for i in range(5):
        fn = f"obs_{obs_id}_photo_{9000 + i}.jpg"
        Image.new("RGB", (640, 480), (40 * i, 90, 160)).save(photo_dir / fn)
        photos.append({"photo_id": 9000 + i, "observation_id": obs_id,
                       "position": i, "license_code": "cc-by",
                       "width": 640, "height": 480,
                       "original_url": f"https://x/{i}.jpg",
                       "local_path": f"photos/{fn}", "download_ok": 1,
                       "requested_size": "large", "downloaded_size": "large"})
    obs_row = {"observation_id": obs_id, "uuid": "u", "taxon_id": 12345,
               "scientific_name": "Testus avis", "common_name": "测试鸟",
               "taxon_rank": "species", "observed_on": "2026-01-01",
               "photo_count": 5}
    # 观察与照片必须同批写入：原始库 raw_photos 有指向 raw_observations 的外键
    mgr.write_page_data([obs_row], photos, None, "2026-01-01T00:00:00")
    mgr.close()

    args = argparse.Namespace(out=ds_tmp / "out", taxon_ids="", data_dir=ds_tmp,
                              main_db=ds_tmp / "db" / "bird_observations.db",
                              splits="8:1:1", seed=42, label_scheme="id",
                              min_per_class=0, max_per_class=0,
                              min_width=0, min_height=0, license="",
                              link="copy", no_verify=True)
    rc = export_dataset(args)
    man = args.out / "manifest.csv"
    check("图片级兜底路径导出成功", rc == 0 and man.is_file(), f"rc={rc}")

    rows = list(csv.DictReader(man.open(encoding="utf-8-sig"))) if man.is_file() else []
    by_split = {}
    for r in rows:
        by_split.setdefault(r["split"], []).append(r["photo_id"])
    # 代码的实际保证：n>=3 时 train/val 非空；n>=6 才额外切 test
    check("兜底后训练集与验证集均非空（n=5，test 按设计可为空）",
          bool(by_split.get("train")) and bool(by_split.get("val")),
          f"train={len(by_split.get('train', []))} "
          f"val={len(by_split.get('val', []))} test={len(by_split.get('test', []))}")
    check("每张照片恰好出现一次（无重复/无遗漏）",
          len(rows) == 5 and len({r["photo_id"] for r in rows}) == 5,
          f"manifest 行数={len(rows)}")
    # 同一照片的磁盘落点与 manifest 的 split 必须一致（变量覆盖 bug 会让两者错位）
    mismatch = [r["file"] for r in rows
                if not (args.out / r["file"]).is_file()
                or (args.out / r["file"]).parts[-3] != r["split"]]
    check("落盘目录与 manifest 的 split 一致", not mismatch, f"错位 {len(mismatch)} 项")

    # 再跑一次同样的导出：随机种子相同应得到完全相同的划分（可复现）
    args2 = argparse.Namespace(**{**vars(args), "out": ds_tmp / "out2"})
    export_dataset(args2)
    rows2 = list(csv.DictReader((args2.out / "manifest.csv").open(encoding="utf-8-sig")))
    same = {(r["photo_id"], r["split"]) for r in rows} == \
           {(r["photo_id"], r["split"]) for r in rows2}
    check("同种子导出可复现（划分一致）", same)

    # n>=6 的场景：兜底应能额外切出检测集，三集合均非空
    ds6 = tmp / "ds_case6"
    mgr6 = INaturalistDatabaseManager(ds6 / "db" / "raw_data.db",
                                      ds6 / "db" / "bird_observations.db")
    mgr6.ensure_taxon(54321, "Testus avis", "测试鸟", "family")
    pdir6 = ds6 / "taxon_54321_Testus_avis_测试鸟" / "photos"
    pdir6.mkdir(parents=True, exist_ok=True)
    photos6 = []
    for i in range(8):
        fn = f"obs_888_photo_{9500 + i}.jpg"
        Image.new("RGB", (320, 240), (30, 60 * i, 120)).save(pdir6 / fn)
        photos6.append({"photo_id": 9500 + i, "observation_id": 888,
                        "position": i, "license_code": "cc-by",
                        "width": 320, "height": 240,
                        "original_url": f"https://x/{i}.jpg",
                        "local_path": f"photos/{fn}", "download_ok": 1,
                        "requested_size": "large", "downloaded_size": "large"})
    mgr6.write_page_data([{"observation_id": 888, "uuid": "u6", "taxon_id": 54321,
                           "scientific_name": "Testus avis",
                           "common_name": "测试鸟", "taxon_rank": "species",
                           "observed_on": "2026-01-02", "photo_count": 8}],
                         photos6, None, "2026-01-02T00:00:00")
    mgr6.close()
    args6 = argparse.Namespace(**{**vars(args), "out": ds6 / "out",
                                  "data_dir": ds6,
                                  "main_db": ds6 / "db" / "bird_observations.db"})
    export_dataset(args6)
    rows6 = list(csv.DictReader((args6.out / "manifest.csv").open(encoding="utf-8-sig")))
    sp6 = {r["split"] for r in rows6}
    check("n=8 时三集合均有样本", {"train", "val", "test"} <= sp6, str(sorted(sp6)))

    # ==================================================================
    # P1-4 静默吞异常留痕
    # ==================================================================
    print()
    print("=" * 70)
    print("P1-4 静默降级留痕")
    print("=" * 70)

    import drop_crawl_interface as dci  # noqa: E402
    import inspect  # noqa: E402

    src_text = inspect.getsource(dci._import_images_for_kind)
    check("taxon_tree 登记失败会写入处理报告（不再是裸 pass）",
          "层级字段在数据集导出时将退化为占位符" in src_text)

    di_text = inspect.getsource(di.sync_taxon_tree)
    check("中文俗名补全失败会留痕", "中文俗名补全失败" in di_text)

    import inaturalist_db_initializer as ini  # noqa: E402
    ini_text = inspect.getsource(ini._backup_corrupted_db)
    check("损坏库备份失败会告警", "备份失败" in ini_text and "警告" in ini_text)

    # 实际行为验证：让备份失败，确认有告警输出且原文件未被删除
    import contextlib
    import io
    bad_db = tmp / "corrupt.db"
    bad_db.write_bytes(b"not a sqlite database at all" * 50)
    buf = io.StringIO()
    orig_copy = ini.shutil.copy2
    ini.shutil.copy2 = lambda *a, **k: (_ for _ in ()).throw(OSError("模拟磁盘只读"))
    try:
        with contextlib.redirect_stdout(buf):
            ini._backup_corrupted_db(bad_db)
    finally:
        ini.shutil.copy2 = orig_copy
    out_text = buf.getvalue()
    check("备份失败时输出警告", "警告" in out_text and "备份失败" in out_text,
          out_text.strip()[:90])
    check("备份失败时保留原文件不删除", bad_db.is_file())
finally:
    shutil.rmtree(tmp, ignore_errors=True)

fails = [r for r in results if not r[1]]
print()
print("=" * 70)
print(f"结果：{len(results) - len(fails)}/{len(results)} 通过")
for n, _, d in fails:
    print(f"  FAIL {n}  {d}")
print("=" * 70)
sys.exit(1 if fails else 0)
