#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# 路径相对脚本目录解析，避免硬编码
_vfca465fc = Path(__file__).resolve().parent
_v2c5114ec = _vfca465fc / "data"
_v1fbee72f = _v2c5114ec / "db"
_v719a029f = _v2c5114ec / "drop_inbox"
_v6d86c951 = "processed_已处理"

# 支持的拖入文本文件类型
_vb4b783d5 = {".txt", ".csv", ".list"}
# 支持的拖入图片类型
_vaa115322 = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# 种文件夹内说明文本的类型（内容并入观察备注）
_v3d3f5cfc = {".txt"}
# 常驻监视模式中文件夹处理完成后的标记文件名
_v6dd2a3c7 = "_已导入_指纹编码完成.txt"

# 数据集常见分割/功能目录名黑名单：这类名字不是物种名，禁止参与类群解析，
# 避免「train」「test」等被 iNaturalist 接口模糊匹配到无关类群
# （实测「train」曾被误匹配到真菌 Trainwrecker，2026-10-14 用户反馈）
_v754e9f8e = {
    "train", "val", "valid", "validation", "test", "eval",
    "images", "image", "imgs", "photos", "photo", "pictures",
    "labels", "label", "annotations", "dataset", "data", "raw",
    # 常见非物种含义的目录/桌面路径名：作为回退候选只会误匹配无关类群
    "drop_inbox", "processed_已处理", "desktop", "downloads",
    "documents", "照片", "图片", "备份", "新建文件夹",
}


def _vc3671187(path: Path, max_levels: int = 3) -> list[str]:
    ancestors: list[str] = []
    for parent in path.absolute().parents:
        name = parent.name.strip()
        if not name:  # 已到盘符根（如 D:\）
            break
        if name.lower() in _v754e9f8e:
            continue
        ancestors.append(name)
        if len(ancestors) >= max_levels:
            break
    return ancestors


def _v400cf3ac(api: str, timeout: int = 15,
                          attempts: int = 3) -> dict:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(api, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)  # 1s, 2s 退避
    raise last_exc  # 重试耗尽，抛给调用方按原有流程记录


def _v339fce5e(token: str, timeout: int = 15) -> tuple[int | None, str]:
    token = token.strip()
    if not token:
        return None, "空行"
    if token.isdigit():
        return int(token), "直接使用 ID"
    # 名称解析：调用官方 taxa/autocomplete 接口，取第一个结果
    api = ("https://api.inaturalist.org/v1/taxa/autocomplete?q="
           + urllib.parse.quote(token))
    try:
        data = _v400cf3ac(api, timeout=timeout)
        results = data.get("results") or []
        if not results:
            return None, f"未找到与「{token}」匹配的类群"
        first = results[0]
        return int(first["id"]), f"「{token}」→ {first.get('name')} (rank={first.get('rank')})"
    except Exception as exc:  # 重试后仍失败才放弃，不中断整体流程
        return None, f"解析「{token}」失败: {exc}"


def _v3808b166(token: str, timeout: int = 15,
                       strict: bool = False) -> tuple[int | None, str, str, str]:
    token = token.strip()
    if not token:
        return None, "", "", "空行"
    if token.isdigit():
        api = f"https://api.inaturalist.org/v1/taxa/{token}"
    else:
        api = ("https://api.inaturalist.org/v1/taxa/autocomplete?q="
               + urllib.parse.quote(token))
        if strict:
            api += "&locale=zh-CN"
    try:
        data = _v400cf3ac(api, timeout=timeout)
        results = data.get("results") or []
        if not results:
            return None, "", "", f"未找到与「{token}」匹配的类群"
        chosen = None
        for item in results:
            if not strict:
                chosen = item
                break
            # 严格校验（词级匹配，忽略大小写），满足其一才算相关：
            # 1) 候选名与学名/俗名整词相等（如「青藏喜鹊」= 俗名）；
            # 2) 候选名是学名或俗名中的一个完整单词（如「Pica」为
            #    「Pica bottanensis」的单词；「fix」不是「Fixsenia」的
            #    单词，被拒绝——防子串误匹配）；
            # 3) 完整学名/俗名短语包含在候选名里（如「青藏喜鹊_Pica
            #    bottanensis」包含学名短语「Pica bottanensis」）。
            name = (item.get("name") or "").casefold()
            common = (item.get("preferred_common_name") or "").casefold()
            tok = token.casefold()
            name_words = name.split()
            common_words = common.split()
            if (tok == name or tok == common
                    or tok in name_words or tok in common_words
                    or (name and name in tok) or (common and common in tok)):
                chosen = item
                break
        if chosen is None:
            return None, "", "", (
                f"「{token}」的候选结果均与名称不符，严格校验拒绝（防误匹配）")
        taxon_id = int(chosen["id"])
        sci = chosen.get("name") or ""
        common = chosen.get("preferred_common_name") or ""
        rank = chosen.get("rank") or ""
        note = f"「{token}」→ {sci} / {common} (id={taxon_id}, rank={rank})"
        return taxon_id, sci, common, note
    except Exception as exc:  # 重试后仍失败才放弃，不中断整体流程
        return None, "", "", f"解析「{token}」失败: {exc}"


def _v7c8a5b06(kind_name: str, rel_path: str,
                           root_ancestors: list[str] | None = None) -> list[str]:
    candidates: list[str] = []

    def _push(text: str) -> None:
        text = text.strip()
        # 黑名单内的目录名（train/val/test 等）不是物种名，不得作为回退候选
        if (text and ":" not in text and text not in candidates
                and text.lower() not in _v754e9f8e):
            candidates.append(text)

    for part in (p.strip() for p in kind_name.split("_")):
        if part and part != kind_name:
            _push(part)
    segments = [s.strip() for s in rel_path.split("/") if s.strip()]
    # 最后一段即叶名本身，已试过；从父级起自近及远回退
    for seg in reversed(segments[:-1] if len(segments) > 1 else []):
        _push(seg)
        for part in (p.strip() for p in seg.split("_")):
            if part != seg:
                _push(part)
    # 拖入根之外的上级目录名（同样整名 + 下划线拆分，自近及远）
    for anc in (root_ancestors or []):
        _push(anc)
        for part in (p.strip() for p in anc.split("_")):
            if part != anc:
                _push(part)
    return candidates


def _v55bbbd86(taxon_id: int, timeout: int = 15) -> list[dict]:
    api = f"https://api.inaturalist.org/v1/taxa/{taxon_id}?locale=zh-CN"
    try:
        with urllib.request.urlopen(api, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        payloads = [t for t in (data.get("results") or []) if isinstance(t, dict)]
        if not payloads:
            return []
        main_taxon = payloads[0]
        ancestor_ids = [
            x for x in str(main_taxon.get("ancestry") or "").split("/")
            if x.strip().isdigit()
        ]
        if ancestor_ids:
            # 祖先批量接口一次取回，避免逐个请求
            bulk_api = ("https://api.inaturalist.org/v1/taxa/"
                        + ",".join(ancestor_ids) + "?locale=zh-CN")
            try:
                with urllib.request.urlopen(bulk_api, timeout=timeout) as resp:
                    bulk = json.loads(resp.read().decode("utf-8"))
                payloads.extend(t for t in (bulk.get("results") or [])
                                if isinstance(t, dict))
            except Exception:
                pass  # 祖先获取失败时至少登记当前节点
        merged: dict[int, dict] = {}
        for taxon in payloads:
            tid = taxon.get("id")
            if tid is None:
                continue
            tid = int(tid)
            ancestry = str(taxon.get("ancestry") or "").strip("/")
            chain = [int(x) for x in ancestry.split("/") if x.strip().isdigit()]
            node = {
                "taxon_id": tid,
                "rank": taxon.get("rank") or "",
                "name_sci": taxon.get("name") or "",
                "name_cn": taxon.get("preferred_common_name") or "",
                "parent_id": chain[-1] if chain else None,
                "ancestry_ids": ancestry,
            }
            if tid in merged:
                # 同节点合并：中文名优先，缺失字段补全
                old = merged[tid]
                has_cn = lambda s: bool(re.search(r"[\u4e00-\u9fff]", s or ""))
                if not old["name_cn"] or (has_cn(node["name_cn"])
                                          and not has_cn(old["name_cn"])):
                    old["name_cn"] = node["name_cn"]
                old["name_sci"] = old["name_sci"] or node["name_sci"]
                old["rank"] = old["rank"] or node["rank"]
                old["parent_id"] = (old["parent_id"]
                                    if old["parent_id"] is not None
                                    else node["parent_id"])
                old["ancestry_ids"] = old["ancestry_ids"] or node["ancestry_ids"]
            else:
                merged[tid] = node
        return list(merged.values())
    except Exception:
        return []


def _vb39f9138(path: Path) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def _v7ea032c0(path: Path, max_chars: int = 20000) -> str:
    text: str | None = None
    for enc in ("utf-8-sig", "gbk"):
        try:
            text = path.read_text(encoding=enc)
            break
        except (UnicodeDecodeError, OSError):
            continue
    if text is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "…（说明文本过长，已截断）"
    return text


def _v58aeb61a(root: Path) -> list[tuple[str, list[Path], str, str]]:
    kinds: list[tuple[str, list[Path], str, str]] = []

    def _walk(folder: Path, rel: str) -> None:
        images: list[Path] = []
        texts: list[str] = []
        subdirs: list[Path] = []
        for item in sorted(folder.iterdir()):
            if item.is_dir():
                subdirs.append(item)
            elif item.is_file():
                suffix = item.suffix.lower()
                if suffix in _vaa115322:
                    images.append(item)
                elif suffix in _v3d3f5cfc and item.name != _v6dd2a3c7:
                    # 排除常驻监视的完成标记，只收集真正的说明文本
                    content = _v7ea032c0(item)
                    if content:
                        texts.append(f"【{item.name}】{content}")
        if images:
            # 说明文本与层级路径交由导入函数并入观察备注
            kinds.append((folder.name, images, rel or folder.name,
                          "\n".join(texts)))
        for sub in subdirs:
            _walk(sub, f"{rel}/{sub.name}" if rel else sub.name)

    _walk(root, root.name)
    return kinds


# ================= 图片本地入库：依次指纹编码 =================

def _v6129f938(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _v61f86667(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as im:
            return im.width, im.height
    except Exception:
        return None, None


def _v35b5c37e(kind_name: str, images: list[Path],
                            manager, batch_id: int, out_dir: Path,
                            seen_in_session: set[str],
                            report_lines: list[str],
                            rel_path: str = "",
                            texts: str = "",
                            progress_base: int = 0,
                            progress_total: int = 0,
                            root_ancestors: list[str] | None = None,
                            ) -> tuple[int, int, int, int]:
    # 叶名本身是数据集分割目录名（train/val/test 等）时直接视为未匹配，
    # 不调用接口解析，防止被模糊匹配到无关类群
    if kind_name.strip().lower() in _v754e9f8e:
        taxon_id, sci, common, note = None, "", "", (
            f"  种类名「{kind_name}」为数据集分割目录名，不参与类群解析")
    else:
        # 「xx同属（英文名：XXX）」格式：整名无法解析，但括号内为具体
        # 物种英文名，提取后直接解析（一个文件夹对应一个物种）
        m_en = re.search(r"同属（英文名：(.+?)）", kind_name)
        if m_en:
            en_name = m_en.group(1).strip()
            taxon_id, sci, common, note = _v3808b166(en_name)
            if taxon_id is not None:
                # 解析成功：报告同时记录原始文件夹名与英文名
                report_lines.append(
                    f"  「{kind_name}」按英文名「{en_name}」解析")
        else:
            taxon_id, sci, common, note = _v3808b166(kind_name)
    report_lines.append(f"  {note}")
    if rel_path and rel_path != kind_name:
        report_lines.append(f"  层级: {rel_path}")
    if taxon_id is None:
        # 叶名（直接含图片的最深层文件夹名）未命中时按层级回退解析，
        # 适应「物种_学名/来源子文件夹/图片」等以来源命名叶层的组织方式
        # （含拖入根之外的上级目录名，适应叶层以编号命名的拖入结构）
        for cand in _v7c8a5b06(kind_name, rel_path, root_ancestors):
            # 回退候选一律用严格匹配校验：解析结果必须与候选名相关，
            # 防止路径中的非物种名（fix/train/编号等）被模糊匹配到无关类群
            tid, s2, c2, n2 = _v3808b166(cand, strict=True)
            if tid is not None:
                taxon_id, sci, common = tid, s2, c2
                report_lines.append(f"  回退层级名「{cand}」命中: {n2}")
                break
    if taxon_id is None:
        # 明确报告未匹配并跳过，绝不静默归入错误类群；
        # 此类数据集建议改用「训练数据集」页签按物种清单映射导入
        report_lines.append(
            f"  !! 「{kind_name}」未匹配到类群，跳过本种类"
            f"（{len(images)} 张图片未入库，不计入失败）")
        return 0, 0, 0, len(images)

    # 目录命名与主爬虫完全一致，保证与爬取数据合并存储
    from download_inaturalist import _vca17059a
    folder_name = f"taxon_{taxon_id}_{_vca17059a(sci)}_{_vca17059a(common)}"
    photo_dir = out_dir / folder_name / "photos"
    photo_dir.mkdir(parents=True, exist_ok=True)

    # 观察备注：来源 + 纲目科层级路径 + 种文件夹内的说明文本
    note_parts = [f"本地导入来源: {kind_name}"]
    if rel_path and rel_path != kind_name:
        note_parts.append(f"层级: {rel_path}")
    if texts:
        note_parts.append(texts)

    # 登记 iNaturalist 官方纲-目-科-种层级链（供训练导出使用，失败不中断导入）
    try:
        tree_nodes = _v55bbbd86(taxon_id)
        if tree_nodes:
            manager.upsert_taxon_tree_bulk(tree_nodes)
    except Exception:
        pass  # 层级登记失败不影响图片入库

    obs_id = manager.upsert_local_observation(
        taxon_id=taxon_id, scientific_name=sci, common_name=common,
        source_name=kind_name, note="\n".join(note_parts))

    new = dup = fail = 0
    for position, img in enumerate(images, 1):
        # 全局照片级进度行：GUI 进度条按此刷新（种类内也保持细粒度反馈）
        if progress_total > 0:
            print(f"[进度] 照片 {progress_base + position}/{progress_total}",
                  flush=True)
        sha = _v6129f938(img)
        seen_in_session.add(sha)
        if manager.has_photo_fingerprint(sha):
            dup += 1
            report_lines.append(
                f"    [{position}] {img.name} → 指纹 {sha[:16]}… 内容重复，跳过")
            continue
        photo_id = manager.next_local_photo_id()
        filename = f"obs_{obs_id}_photo_{photo_id}{img.suffix.lower()}"
        dest = photo_dir / filename
        try:
            shutil.copy2(img, dest)
        except OSError as exc:
            fail += 1
            report_lines.append(f"    [{position}] {img.name} → 复制失败: {exc}")
            continue
        width, height = _v61f86667(dest)
        status = manager.upsert_local_photo(
            photo_id=photo_id, observation_id=obs_id, content_sha=sha,
            original_name=img.name,
            dest_relative=str(Path("photos") / filename),
            file_size=dest.stat().st_size,
            width=width, height=height, position=position)
        if status == "inserted":
            new += 1
            report_lines.append(
                f"    [{position}] {img.name} → {filename} "
                f"指纹 {sha[:16]}… 已编码入库")
        else:
            dup += 1
            dest.unlink(missing_ok=True)
            report_lines.append(
                f"    [{position}] {img.name} → 指纹重复，跳过")

    manager.refresh_local_observation_stats(obs_id)
    manager.record_batch_taxon(
        batch_id=batch_id, taxon_id=taxon_id,
        observation_count=1, photo_count=len(images),
        photos_downloaded=new, photos_failed=fail)
    manager.link_batch_observation(batch_id=batch_id, observation_id=obs_id)
    report_lines.append(
        f"  小计 {folder_name}: 新入库 {new}, 重复 {dup}, 失败 {fail}, "
        f"本地观察ID {obs_id}")
    return new, dup, fail, 0


def _vb6f0a2ac(path: Path, out_dir: Path, db_dir: Path,
                        taxon_token: str = "") -> str:
    from inaturalist_db_manager import _vc1f32a11

    report_lines = [
        "拖入文件夹导入报告（按种类指纹编码）",
        f"源文件夹: {path}",
        f"处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "-" * 56,
    ]

    # 收集图片：--taxon 统一指定时全树图片归一类；
    # 否则自适应下钻，每个直接包含图片的最深层文件夹（种）各成一类，
    # 同时收集该文件夹内的说明文本与纲-目-科层级路径
    kinds: list[tuple[str, list[Path], str, str]] = []
    if taxon_token:
        all_imgs = sorted(p for p in path.rglob("*")
                          if p.is_file() and p.suffix.lower() in _vaa115322)
        if all_imgs:
            kinds.append((taxon_token, all_imgs, "", ""))
    else:
        kinds = _v58aeb61a(path)

    if not kinds:
        report_lines.append("未发现图片文件（支持 jpg/jpeg/png/webp/gif）。")
        report_lines.append("用法：包含图片的最深层文件夹名应为物种名/中文名/类群ID，")
        report_lines.append("      支持纲-目-科-种等多级结构自动下钻到种级，")
        report_lines.append("      或在调用时用 --taxon 统一指定类群。")
        return "\n".join(report_lines)

    manager = _vc1f32a11(db_dir / "raw_data.db",
                                         db_dir / "bird_observations.db")
    batch_id = manager.create_batch_run(
        quality_grade="local_import", photo_size="original",
        output_dir=str(out_dir))
    # 拖入根目录的上级文件夹名：叶层以编号命名（如 BIRD1445照片）时，
    # 物种名在拖入根之外，作为回退解析的最后一级候选
    root_ancestors = _vc3671187(path)
    try:
        seen: set[str] = set()
        total_new = total_dup = total_fail = total_skip = 0
        # 全局照片总数：进度条的照片级分母（所有种类图片数之和）
        total_photos = sum(len(imgs) for _, imgs, _, _ in kinds)
        photo_base = 0
        for k, (kind_name, images, rel_path, texts) in enumerate(kinds, 1):
            # 种类级进度行：GUI 进度条标签按此刷新
            print(f"[进度] 种类 {k}/{len(kinds)}「{kind_name}」", flush=True)
            report_lines.append(f"[{k}/{len(kinds)}] 种类「{kind_name}」共 {len(images)} 张")
            new, dup, fail, skip = _v35b5c37e(
                kind_name, images, manager, batch_id, out_dir, seen, report_lines,
                rel_path=rel_path, texts=texts,
                progress_base=photo_base, progress_total=total_photos,
                root_ancestors=root_ancestors)
            photo_base += len(images)
            total_new += new
            total_dup += dup
            total_fail += fail
            total_skip += skip
        if total_new + total_dup == 0:
            # 本次未入库任何照片（全部未匹配/失败）：不保留空批次记录，
            # 避免健康检查反复报告 C4 空批次
            manager.delete_batch_run(batch_id)
            report_lines.append("本次未导入任何照片，已自动清除空批次记录。")
        else:
            manager.update_batch_run(
                batch_id, taxa_succeeded=len(kinds),
                total_photos=total_new + total_dup,
                total_downloaded=total_new, total_failed=total_fail)
    finally:
        manager.close()

    report_lines.append("-" * 56)
    if total_skip:
        report_lines.append(
            f"导入完成: 新入库 {total_new} 张, 重复跳过 {total_dup} 张, "
            f"跳过 {total_skip} 张（分割目录/未匹配类群，未处理非故障）, "
            f"失败 {total_fail} 张。")
    else:
        report_lines.append(
            f"导入完成: 新入库 {total_new} 张, 重复跳过 {total_dup} 张, "
            f"失败 {total_fail} 张。")
    report_lines.append("源文件夹保持原位未动；照片已按种类存入对应 taxon_ 文件夹。")
    return "\n".join(report_lines)


def _vc77ddc71(paths: list[Path], out_dir: Path, db_dir: Path,
                        taxon_token: str = "") -> str:
    from inaturalist_db_manager import _vc1f32a11

    report_lines = [
        "拖入图片导入报告（按种类指纹编码）",
        f"图片数量: {len(paths)}",
        f"处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "-" * 56,
    ]
    if taxon_token:
        kinds: list[tuple[str, list[Path], str, str]] = [
            (taxon_token, sorted(paths), "", "")]
    else:
        groups: dict[str, list[Path]] = {}
        for p in paths:
            groups.setdefault(p.parent.name, []).append(p)
        kinds = [(name, sorted(imgs), name, "") for name, imgs in groups.items()]
        if len(kinds) > 1:
            report_lines.append(f"检测到 {len(kinds)} 个来源文件夹，将按文件夹名分别解析类群。")

    manager = _vc1f32a11(db_dir / "raw_data.db",
                                         db_dir / "bird_observations.db")
    batch_id = manager.create_batch_run(
        quality_grade="local_import", photo_size="original",
        output_dir=str(out_dir))
    try:
        seen: set[str] = set()
        total_new = total_dup = total_fail = total_skip = 0
        # 全局照片总数：进度条的照片级分母（所有种类图片数之和）
        total_photos = sum(len(imgs) for _, imgs, _, _ in kinds)
        photo_base = 0
        for k, (kind_name, images, rel_path, texts) in enumerate(kinds, 1):
            # 种类级进度行：GUI 进度条标签按此刷新
            print(f"[进度] 种类 {k}/{len(kinds)}「{kind_name}」", flush=True)
            report_lines.append(f"[{k}/{len(kinds)}] 种类「{kind_name}」共 {len(images)} 张")
            new, dup, fail, skip = _v35b5c37e(
                kind_name, images, manager, batch_id, out_dir, seen, report_lines,
                rel_path=rel_path, texts=texts,
                progress_base=photo_base, progress_total=total_photos)
            photo_base += len(images)
            total_new += new
            total_dup += dup
            total_fail += fail
            total_skip += skip
        if total_new + total_dup == 0:
            # 本次未入库任何照片（全部未匹配/失败）：不保留空批次记录，
            # 避免健康检查反复报告 C4 空批次
            manager.delete_batch_run(batch_id)
            report_lines.append("本次未导入任何照片，已自动清除空批次记录。")
        else:
            manager.update_batch_run(
                batch_id, taxa_succeeded=len(kinds),
                total_photos=total_new + total_dup,
                total_downloaded=total_new, total_failed=total_fail)
    finally:
        manager.close()

    report_lines.append("-" * 56)
    if total_skip:
        report_lines.append(
            f"导入完成: 新入库 {total_new} 张, 重复跳过 {total_dup} 张, "
            f"跳过 {total_skip} 张（分割目录/未匹配类群，未处理非故障）, "
            f"失败 {total_fail} 张。")
    else:
        report_lines.append(
            f"导入完成: 新入库 {total_new} 张, 重复跳过 {total_dup} 张, "
            f"失败 {total_fail} 张。")
    report_lines.append("源图片保持原位未动。")
    return "\n".join(report_lines)


# ================= 文本清单爬取（原有流程） =================

def _vd6532b1d(path: Path, max_per_taxon: int, workers: int,
                      photo_size: str, quality_grade: str) -> str:
    # 延迟导入，保持模块加载轻量
    from download_inaturalist import _v4ec0b3e2

    report_lines = [
        f"拖入文件爬取报告",
        f"源文件: {path.name}",
        f"处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"参数: 每类群上限={max_per_taxon}, 尺寸={photo_size}, "
        f"质量={quality_grade}, 线程={workers}",
        "-" * 56,
    ]

    rows = _vb39f9138(path)
    if not rows:
        report_lines.append("文件中没有有效内容（应为每行一个类群 ID 或物种名）。")
        return "\n".join(report_lines)

    for i, row in enumerate(rows, 1):
        # 种类级进度行：GUI 进度条按此刷新（爬取模式无照片级进度）
        print(f"[进度] 种类 {i}/{len(rows)}「{row}」", flush=True)
        taxon_id, note = _v339fce5e(row)
        report_lines.append(f"[{i}/{len(rows)}] {note}")
        if taxon_id is None:
            continue
        try:
            summary = _v4ec0b3e2(
                taxon_id=taxon_id,
                out_dir=_v2c5114ec,
                photo_size=photo_size,
                workers=workers,
                max_observations=max_per_taxon,
                quality_grade=None if quality_grade == "any" else quality_grade,
            )
            report_lines.append(
                f"     完成: 观察 {summary['observation_count']} 条, "
                f"照片 {summary['photo_count']} 张 "
                f"(下载 {summary['photos_downloaded']}, "
                f"已存在 {summary['photos_skipped_existing']}, "
                f"失败 {summary['photos_failed']})")
            report_lines.append(f"     输出: {summary['output_dir']}")
        except Exception as exc:
            report_lines.append(f"     爬取失败: {exc}")

    report_lines.append("-" * 56)
    report_lines.append("全部条目处理完毕。")
    return "\n".join(report_lines)


# ================= 入口调度 =================

def _vf4cb3e0e(path: Path, args: argparse.Namespace) -> None:
    print(f"\n>>> 检测到拖入文件: {path}")
    processed_dir = Path(args.inbox) / _v6d86c951
    processed_dir.mkdir(parents=True, exist_ok=True)

    try:
        report = _vd6532b1d(
            path, max_per_taxon=args.max_per_taxon, workers=args.workers,
            photo_size=args.photo_size, quality_grade=args.quality_grade)
        print(report)
        # 保存爬取报告
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = processed_dir / f"{path.stem}_爬取报告_{stamp}.txt"
        report_path.write_text(report, encoding="utf-8")
        print(f"报告已保存: {report_path.name}")
        # 移动源文件到已处理目录（避免重复处理）
        shutil.move(str(path), str(processed_dir / f"{stamp}_{path.name}"))
    except Exception as exc:
        print(f"处理失败: {exc}")
    print("<<< 处理结束，等待下一个文件...\n")


def _v4146a645(path: Path, args: argparse.Namespace,
                  write_marker: bool = False,
                  move_to_processed: bool = False) -> None:
    print(f"\n>>> 检测到拖入文件夹: {path}")
    processed_dir = Path(args.inbox) / _v6d86c951
    processed_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = _vb6f0a2ac(
            path, out_dir=Path(args.out_dir), db_dir=Path(args.db_dir),
            taxon_token=(args.taxon or "").strip())
        print(report)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = processed_dir / f"{path.name}_导入报告_{stamp}.txt"
        report_path.write_text(report, encoding="utf-8")
        print(f"报告已保存: {report_path.name}")
        if write_marker:
            try:
                (path / _v6dd2a3c7).write_text(report, encoding="utf-8")
            except OSError:
                pass
        if move_to_processed:
            shutil.move(str(path), str(processed_dir / f"{stamp}_{path.name}"))
    except Exception as exc:
        print(f"处理失败: {exc}")
    print("<<< 处理结束，等待下一个文件...\n")


def _va7325efe(paths: list[Path], args: argparse.Namespace,
                  move_to_processed: bool = False) -> None:
    print(f"\n>>> 检测到拖入图片 {len(paths)} 张")
    processed_dir = Path(args.inbox) / _v6d86c951
    processed_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = _vc77ddc71(
            paths, out_dir=Path(args.out_dir), db_dir=Path(args.db_dir),
            taxon_token=(args.taxon or "").strip())
        print(report)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = processed_dir / f"图片导入报告_{stamp}.txt"
        report_path.write_text(report, encoding="utf-8")
        print(f"报告已保存: {report_path.name}")
        if move_to_processed:
            # 仅常驻监视模式：导入成功后把投放目录里的图片移入已处理
            for p in paths:
                try:
                    shutil.move(str(p), str(processed_dir / f"{stamp}_{p.name}"))
                except OSError:
                    pass
    except Exception as exc:
        print(f"处理失败: {exc}")
    print("<<< 处理结束，等待下一个文件...\n")


def _va68e1604(folder: Path) -> tuple[int, int]:
    count = 0
    total = 0
    try:
        for item in folder.rglob("*"):
            count += 1
            if item.is_file():
                total += item.stat().st_size
    except OSError:
        pass  # 复制过程中个别文件暂不可访问属正常，跳过即可
    return count, total


def _v91b0c62c(args: argparse.Namespace) -> None:
    inbox = Path(args.inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    print(f"拖入监视已启动: {inbox}")
    print("将 txt/csv 清单、图片或图片文件夹放入该目录即可自动处理。按 Ctrl+C 退出。\n")
    # 记录每个待定文件夹的 (快照, 连续稳定轮数)
    stable_marks: dict[Path, tuple[tuple[int, int], int]] = {}
    while True:
        seen_now: set[Path] = set()
        for item in sorted(inbox.iterdir()):
            # 跳过已处理归档目录本身：它常驻监视目录内且无导入完成标记，
            # 不排除会被每轮当作新输入处理，移动到自己内部报错
            # （Cannot move a directory ... into itself，2026-10-14 用户实测）
            if item.name == _v6d86c951:
                continue
            seen_now.add(item)
            if item.is_dir():
                # 文件夹：跳过已写入完成标记的
                if (item / _v6dd2a3c7).exists():
                    continue
                snap = _va68e1604(item)
                prev = stable_marks.get(item)
                rounds = prev[1] + 1 if prev and prev[0] == snap else 1
                stable_marks[item] = (snap, rounds)
                # 连续 3 轮快照不变（约 6 秒无变化）才开始处理
                if rounds < 3:
                    continue
                print(f"    （{item.name} 快照已稳定 {rounds} 轮，开始处理）")
                _v4146a645(item, args, write_marker=True,
                              move_to_processed=True)
                stable_marks.pop(item, None)
            elif item.is_file():
                if item.suffix.lower() in _vb4b783d5:
                    _vf4cb3e0e(item, args)
                elif item.suffix.lower() in _vaa115322:
                    _va7325efe([item], args, move_to_processed=True)
        # 清理已消失（被处理/移走/删除）条目的稳定计数，防止字典膨胀
        for gone in [k for k in stable_marks if k not in seen_now]:
            stable_marks.pop(gone, None)
        time.sleep(2)


def _va0fedbb1(args: argparse.Namespace) -> int:
    files = [Path(p) for p in (args.file or [])]
    if not files:
        if args.watch:
            try:
                _v91b0c62c(args)
            except KeyboardInterrupt:
                print("\n监视已停止。")
            return 0
        print("请指定 --file 文件/文件夹路径（可多个）或 --watch 常驻监视。")
        return 1

    image_group: list[Path] = []
    for path in files:
        if path.is_dir():
            _v4146a645(path, args)
        elif path.is_file():
            if path.suffix.lower() in _vaa115322:
                image_group.append(path)
            elif path.suffix.lower() in _vb4b783d5:
                _vf4cb3e0e(path, args)
            else:
                print(f"不支持的文件类型（仅支持 txt/csv/list 与图片）: {path}")
        else:
            print(f"路径不存在: {path}")
    if image_group:
        _va7325efe(image_group, args)
    return 0


def _v6f5fe057() -> int:
    parser = argparse.ArgumentParser(description="拖入文件自动爬取接口")
    parser.add_argument("--file", nargs="*", default=[],
                        help="要处理的文件/文件夹路径，可一次传多个（拖到 bat 上时自动传入）")
    parser.add_argument("--watch", action="store_true", help="常驻监视 drop_inbox 目录")
    parser.add_argument("--inbox", type=Path, default=_v719a029f, help="投放目录")
    parser.add_argument("--out-dir", type=Path, default=_v2c5114ec, help="输出根目录")
    parser.add_argument("--db-dir", type=Path, default=_v1fbee72f, help="数据库目录")
    parser.add_argument("--taxon", default="",
                        help="统一归属类群（物种名/中文名/ID），覆盖文件夹名判定")
    parser.add_argument("--max-per-taxon", type=int, default=50, help="每个类群爬取上限（默认 50）")
    parser.add_argument("--workers", type=int, default=6, help="图片下载线程数")
    parser.add_argument("--photo-size", choices=["original", "large", "medium"], default="large")
    parser.add_argument("--quality-grade", choices=["research", "needs_id", "casual", "any"],
                        default="research")
    return _va0fedbb1(parser.parse_args())


if __name__ == "__main__":
    sys.exit(_v6f5fe057())
