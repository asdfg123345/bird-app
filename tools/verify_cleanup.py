"""收尾清理专项验证。

覆盖：
  清理-1 upsert_local_photo 的完整性错误分类：
          - 指纹 UNIQUE 冲突 → 返回 "duplicate"
          - 外键失败        → 上抛（不再被静默当成重复）
          - photo_id 冲突   → 上抛
  清理-2 两个导入循环把单张失败记入报告并继续
  清理-3 seen_in_session 死代码已彻底移除
"""

import inspect
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import drop_crawl_interface as dci  # noqa: E402
import training_dataset_importer as tdi  # noqa: E402
from inaturalist_db_manager import (  # noqa: E402
    INaturalistDatabaseManager,
    _is_fingerprint_conflict,
)

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


tmp = Path(tempfile.mkdtemp(prefix="cleanup_"))
try:
    print("=" * 72)
    print("清理-1 完整性错误分类")
    print("=" * 72)

    mgr = INaturalistDatabaseManager(tmp / "db" / "raw_data.db",
                                     tmp / "db" / "bird_observations.db")
    obs_id = mgr.upsert_local_observation(
        taxon_id=20967, scientific_name="Upupa epops", common_name="Hoopoe",
        source_name="cleanup")

    # (a) 正常插入
    st = mgr.upsert_local_photo(
        photo_id=-1, observation_id=obs_id, content_sha="sha_a",
        original_name="a.jpg", dest_relative="photos/a.jpg")
    check("正常插入返回 inserted", st == "inserted", st)

    # (b) 同指纹再插一次（换 photo_id）→ 真正的内容重复
    st = mgr.upsert_local_photo(
        photo_id=-2, observation_id=obs_id, content_sha="sha_a",
        original_name="a_copy.jpg", dest_relative="photos/a_copy.jpg")
    check("指纹 UNIQUE 冲突返回 duplicate", st == "duplicate", st)

    # (c) 外键失败（观察不存在）→ 必须上抛，不得静默当成重复
    raised = None
    try:
        mgr.upsert_local_photo(
            photo_id=-3, observation_id=999999, content_sha="sha_fk",
            original_name="fk.jpg", dest_relative="photos/fk.jpg")
    except sqlite3.IntegrityError as exc:
        raised = exc
    check("外键失败上抛 IntegrityError（不再静默丢失）",
          raised is not None and "FOREIGN KEY" in str(raised),
          str(raised)[:60] if raised else "未抛出")
    check("外键失败没有写入照片",
          not mgr.has_photo_fingerprint("sha_fk"))

    # (d) photo_id 冲突（不同内容却复用同一 photo_id）→ 上抛
    raised2 = None
    try:
        mgr.upsert_local_photo(
            photo_id=-1, observation_id=obs_id, content_sha="sha_b",
            original_name="b.jpg", dest_relative="photos/b.jpg")
    except sqlite3.IntegrityError as exc:
        raised2 = exc
    check("photo_id 冲突上抛 IntegrityError", raised2 is not None,
          str(raised2)[:60] if raised2 else "未抛出")

    # (e) 分类函数本身
    check("_is_fingerprint_conflict 只认指纹列",
          _is_fingerprint_conflict(
              sqlite3.IntegrityError("UNIQUE constraint failed: photos.fingerprint"))
          and not _is_fingerprint_conflict(
              sqlite3.IntegrityError("FOREIGN KEY constraint failed"))
          and not _is_fingerprint_conflict(
              sqlite3.IntegrityError("UNIQUE constraint failed: photos.photo_id")))
    mgr.close()

    # ==================================================================
    print()
    print("=" * 72)
    print("清理-2 导入循环把单张失败计入报告并继续")
    print("=" * 72)

    drop_src = inspect.getsource(dci._import_images_for_kind)
    check("drop 导入循环捕获 IntegrityError 并计入失败",
          "except sqlite3.IntegrityError" in drop_src
          and "入库失败" in drop_src
          and "fail += 1" in drop_src)
    check("drop 捕获后 continue（不中断整批）",
          "入库失败" in drop_src and "continue" in drop_src)

    imp_src = inspect.getsource(tdi.import_one_class)
    check("training_dataset_importer 同样捕获并记账",
          "except sqlite3.IntegrityError" in imp_src
          and "入库失败" in imp_src)

    # ==================================================================
    print()
    print("=" * 72)
    print("清理-3 seen_in_session 死代码已移除")
    print("=" * 72)

    hits = []
    for f in ("drop_crawl_interface.py", "training_dataset_importer.py"):
        text = (SRC / f).read_text(encoding="utf-8")
        if "seen_in_session" in text:
            hits.append(f)
    check("源码中不再出现 seen_in_session", not hits, str(hits))

    check("_import_images_for_kind 签名已无该参数",
          "seen_in_session" not in inspect.signature(
              dci._import_images_for_kind).parameters)
    check("import_one_class 签名已无该参数",
          "seen_in_session" not in inspect.signature(
              tdi.import_one_class).parameters)

    # 函数仍可正常调用（签名改动未破坏调用点）
    try:
        sig = inspect.signature(dci._import_images_for_kind)
        sig.bind("kind", [], None, 1, Path("."), [])
        bind_ok = True
        bind_err = ""
    except TypeError as exc:
        bind_ok, bind_err = False, str(exc)
    check("_import_images_for_kind 调用参数可正确绑定", bind_ok, bind_err)

    try:
        sig2 = inspect.signature(tdi.import_one_class)
        sig2.bind("cls", {}, {}, None, 1, Path("."), [], 1)
        bind2_ok = True
        bind2_err = ""
    except TypeError as exc:
        bind2_ok, bind2_err = False, str(exc)
    check("import_one_class 调用参数可正确绑定", bind2_ok, bind2_err)

    # ==================================================================
    print()
    print("=" * 72)
    print("端到端：真实 process_drop_folder 导入（仅把网络函数打桩）")
    print("=" * 72)
    # 目的：签名改动最危险的地方是调用点，这里完整走一遍真实导入流程
    from PIL import Image  # noqa: E402

    e2e = tmp / "e2e"
    src_dir = e2e / "戴胜_Pica_test"
    src_dir.mkdir(parents=True, exist_ok=True)
    for i in range(7):
        Image.new("RGB", (64, 48), (10 * i, 80, 140)).save(src_dir / f"img_{i}.jpg")

    orig_resolve = dci.resolve_taxon_info
    orig_tree = dci.fetch_taxon_tree_nodes
    orig_info = dci.resolve_taxon_id
    dci.resolve_taxon_info = lambda token, **k: (
        20967, "Upupa epops", "戴胜", f"「{token}」→ Upupa epops")
    dci.resolve_taxon_id = lambda token, **k: (20967, "直接使用 ID")
    dci.fetch_taxon_tree_nodes = lambda tid, **k: []
    try:
        report = dci.process_drop_folder(src_dir, e2e / "data", e2e / "data" / "db")
    finally:
        dci.resolve_taxon_info = orig_resolve
        dci.fetch_taxon_tree_nodes = orig_tree
        dci.resolve_taxon_id = orig_info

    check("process_drop_folder 正常返回报告", isinstance(report, str) and report)
    check("报告含「新入库 7」", "新入库 7" in report,
          [ln for ln in report.splitlines() if "小计" in ln][:1])

    m2 = INaturalistDatabaseManager(e2e / "data" / "db" / "raw_data.db",
                                    e2e / "data" / "db" / "bird_observations.db")
    n = m2.get_photo_count()
    m2.close()
    check("7 张图片全部入库", n == 7, f"{n} / 7")
    check("层级链登记失败已留痕（不再静默）",
          "层级字段在数据集导出时将退化为占位符" in report)
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
