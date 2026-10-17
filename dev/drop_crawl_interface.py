#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================
"""拖入文件自动爬取接口（支持文本清单与本地图片入库）。

用途：
    用户将文件/文件夹拖放到「拖入文件自动爬取.bat」上
    （或放入 data/drop_inbox 目录、或通过 GUI 选择），本接口自动处理：

    1. 文本清单（.txt/.csv/.list）：
       每行一个类群 ID 或物种名，逐个类群爬取数据与图片，
       全部写入两层数据库（与主爬虫共用同一套指纹去重机制）。

    2. 图片文件/文件夹（.jpg/.jpeg/.png/.webp/.gif）：
       按种类区分后本地入库，依次进行指纹编码：
       - 种类判定：递归下钻整棵目录树，每个"直接包含图片"的最深层
         文件夹自成一种（纲-目-科-种等多级结构自动定位到种级），
         文件夹名（物种名、中文名或类群 ID）自动查询官方 API 解析；
       - 种文件夹内的说明文本（.txt）内容并入该种观察记录的备注，
         纲-目-科层级路径一并记录，可随时在面板追溯；
       - 依次编码：每张图按文件名顺序分配递增的本地照片 ID（负数，
         与 iNaturalist 正数 ID 天然区分），文件名沿用
         obs_{观察ID}_photo_{照片ID}.ext 约定；
       - 指纹编码：以文件内容 SHA-256 为唯一指纹（UNIQUE 约束），
         相同内容的图片无论文件名是否相同都只入库一次；
       - 存储：照片复制到 data/taxon_{ID}_{学名}_{俗名}/photos/，
         源文件保持原位不动（常驻监视模式除外）。

文件格式（每行一条，# 开头为注释）：
    20967              ← 直接使用 iNaturalist 类群 ID
    Upupa epops        ← 物种名，自动查询 API 转为类群 ID
    戴胜               ← 中文名同样支持

三种使用方式：
    1. 一次性处理：将文件/文件夹拖到 bat 上，或
       python drop_crawl_interface.py --file 某文件.txt --file 某图片夹
       （可加 --taxon 戴胜 为未按物种命名的图片统一指定类群）
    2. 常驻监视：python drop_crawl_interface.py --watch
       自动处理 data/drop_inbox 目录中新增的文件与文件夹。
    3. GUI：桌面控制台「拖入文件爬取」标签页。

处理完成的文本文件会被移动到 data/drop_inbox/processed_已处理/ 目录，
并生成一份「文件名_爬取报告.txt」；图片源文件始终保留原位。
"""

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
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_DIR = DATA_DIR / "db"
DEFAULT_INBOX = DATA_DIR / "drop_inbox"
PROCESSED_DIRNAME = "processed_已处理"

# 支持的拖入文本文件类型
SUPPORTED_SUFFIXES = {".txt", ".csv", ".list"}
# 支持的拖入图片类型
IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# 种文件夹内说明文本的类型（内容并入观察备注）
TEXT_SUFFIXES = {".txt"}
# 常驻监视模式中文件夹处理完成后的标记文件名
IMPORT_MARKER = "_已导入_指纹编码完成.txt"

# 数据集常见分割/功能目录名黑名单：这类名字不是物种名，禁止参与类群解析，
# 避免「train」「test」等被 iNaturalist 接口模糊匹配到无关类群
# （实测「train」曾被误匹配到真菌 Trainwrecker，2026-10-14 用户反馈）
FALLBACK_NAME_BLACKLIST = {
    "train", "val", "valid", "validation", "test", "eval",
    "images", "image", "imgs", "photos", "photo", "pictures",
    "labels", "label", "annotations", "dataset", "data", "raw",
    # 常见非物种含义的目录/桌面路径名：作为回退候选只会误匹配无关类群
    "drop_inbox", "processed_已处理", "desktop", "downloads",
    "documents", "照片", "图片", "备份", "新建文件夹",
}


def _dragged_root_ancestors(path: Path, max_levels: int = 3) -> list[str]:
    """收集拖入根目录的上级文件夹名（自近及远，最多 max_levels 层）。

    拖入的叶层文件夹常以「BIRD1445照片」等编号命名，真正的物种名
    （如「青藏喜鹊_Pica bottanensis」）在其上级目录；这些上级名不在
    collect_leaf_kinds 的层级路径内，需单独提取参与回退解析。
    黑名单名与盘符根自动跳过。
    """
    ancestors: list[str] = []
    for parent in path.absolute().parents:
        name = parent.name.strip()
        if not name:  # 已到盘符根（如 D:\）
            break
        if name.lower() in FALLBACK_NAME_BLACKLIST:
            continue
        ancestors.append(name)
        if len(ancestors) >= max_levels:
            break
    return ancestors


def _open_json_with_retry(api: str, timeout: int = 15,
                          attempts: int = 3) -> dict:
    """请求 JSON 接口，网络瞬断时自动重试（指数退避 1s/2s）。

    大批量拖入时 iNaturalist 接口偶发 Remote end closed / SSL EOF，
    单次请求失败即跳过种类会白白损失图片入库机会，故加入重试。
    只重试网络异常；「未找到结果」属正常响应，由调用方处理。
    """
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


def resolve_taxon_id(token: str, timeout: int = 15) -> tuple[int | None, str]:
    """将一行内容解析为类群 ID。

    纯数字直接视为 ID；否则调用 iNaturalist 自动补全 API 按名称解析。
    返回 (类群ID, 说明文字)。
    """
    token = token.strip()
    if not token:
        return None, "空行"
    if token.isdigit():
        return int(token), "直接使用 ID"
    # 名称解析：调用官方 taxa/autocomplete 接口，取第一个结果
    api = ("https://api.inaturalist.org/v1/taxa/autocomplete?q="
           + urllib.parse.quote(token))
    try:
        data = _open_json_with_retry(api, timeout=timeout)
        results = data.get("results") or []
        if not results:
            return None, f"未找到与「{token}」匹配的类群"
        first = results[0]
        return int(first["id"]), f"「{token}」→ {first.get('name')} (rank={first.get('rank')})"
    except Exception as exc:  # 重试后仍失败才放弃，不中断整体流程
        return None, f"解析「{token}」失败: {exc}"


def resolve_taxon_info(token: str, timeout: int = 15,
                       strict: bool = False) -> tuple[int | None, str, str, str]:
    """将名称/ID 解析为完整类群信息（用于图片按种类入库）。

    返回 (类群ID, 学名, 俗名, 说明文字)。
    纯数字按 ID 查询官方接口补全名称，保证与主爬虫目录命名一致。
    strict=True 时启用严格匹配校验（回退候选专用）：接口返回结果必须与
    候选名语义相关（学名/俗名互相包含）才接受，防止「fix」「train」等
    非物种含义的名字被模糊匹配到无关类群；同时带 locale=zh-CN 请求，
    使中文俗名参与校验。纯数字 ID 不受严格校验限制。
    """
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
        data = _open_json_with_retry(api, timeout=timeout)
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


def _taxon_name_candidates(kind_name: str, rel_path: str,
                           root_ancestors: list[str] | None = None) -> list[str]:
    """构造叶名未命中时的回退候选类群名序列（自近及远）。

    顺序：叶名按下划线拆分 → 层级路径上层文件夹名自近及远 → 拖入根目录的
    上级文件夹名自近及远（叶层以编号命名时物种名在拖入根之外），每个名字
    先整名、再按下划线拆分（如「青藏喜鹊_Pica bottanensis」→ 青藏喜鹊、
    Pica bottanensis）。自动剔除空白、含盘符冒号与重复的候选；
    叶名整名已试过故不重复列入。
    """
    candidates: list[str] = []

    def _push(text: str) -> None:
        text = text.strip()
        # 黑名单内的目录名（train/val/test 等）不是物种名，不得作为回退候选
        if (text and ":" not in text and text not in candidates
                and text.lower() not in FALLBACK_NAME_BLACKLIST):
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


def fetch_taxon_tree_nodes(taxon_id: int, timeout: int = 15) -> list[dict]:
    """获取一个类群及其全部祖先的层级节点（用于登记 taxon_tree 表）。

    调用官方 /taxa/{id} 详情接口（含 ancestry 祖先链，locale=zh-CN
    获取中文俗名），把当前类群与全部祖先组装为主库 taxon_tree 表的
    登记字典；同一节点多次出现时合并字段、中文名优先；失败返回空列表。
    """
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


def parse_drop_file(path: Path) -> list[str]:
    """读取拖入文件，返回有效行（去注释、去空行）。"""
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def read_note_text(path: Path, max_chars: int = 20000) -> str:
    """读取种文件夹内的说明文本：UTF-8 优先，GBK 兜底，仍失败则替换模式容错。

    超长文本截断保护，避免异常大文件拖慢入库。
    """
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


def collect_leaf_kinds(root: Path) -> list[tuple[str, list[Path], str, str]]:
    """自适应下钻收集：递归整棵目录树，每个"直接包含图片"的最深层文件夹自成一类（种）。

    纲-目-科-种等多级结构会自动定位到种级文件夹，而非只认第一层子文件夹；
    同时收集该文件夹内的说明文本（.txt，内容并入观察备注）与层级路径。
    返回 (类名, 图片列表, 层级路径, 说明文本拼接)；层级路径形如 纲/目/科/种。
    """
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
                if suffix in IMG_SUFFIXES:
                    images.append(item)
                elif suffix in TEXT_SUFFIXES and item.name != IMPORT_MARKER:
                    # 排除常驻监视的完成标记，只收集真正的说明文本
                    content = read_note_text(item)
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

def _sha256_of_file(path: Path, chunk: int = 1 << 20) -> str:
    """计算文件内容的 SHA-256 指纹（分块读取，支持大文件）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _image_size(path: Path) -> tuple[int | None, int | None]:
    """读取图片宽高（需 Pillow，未安装时返回 None，不影响导入）。"""
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as im:
            return im.width, im.height
    except Exception:
        return None, None


def _import_images_for_kind(kind_name: str, images: list[Path],
                            manager, batch_id: int, out_dir: Path,
                            seen_in_session: set[str],
                            report_lines: list[str],
                            rel_path: str = "",
                            texts: str = "",
                            progress_base: int = 0,
                            progress_total: int = 0,
                            root_ancestors: list[str] | None = None,
                            ) -> tuple[int, int, int, int]:
    """将一个种类的图片依次指纹编码入库（种类内部复用一条本地观察）。

    rel_path 为纲-目-科层级路径，texts 为种文件夹内的说明文本，
    二者并入该种观察记录的备注。progress_base/progress_total 用于向
    stdout 输出全局照片级进度行（[进度] 照片 i/n），供 GUI 进度条消费。
    返回 (新导入数, 重复跳过数, 失败数, 跳过数)。
    注意：跳过（分割目录名/未匹配类群，图片未处理）与失败（处理出错）
    分开统计，避免用户把大量正常跳过误读为导入故障。
    """
    # 叶名本身是数据集分割目录名（train/val/test 等）时直接视为未匹配，
    # 不调用接口解析，防止被模糊匹配到无关类群
    if kind_name.strip().lower() in FALLBACK_NAME_BLACKLIST:
        taxon_id, sci, common, note = None, "", "", (
            f"  种类名「{kind_name}」为数据集分割目录名，不参与类群解析")
    else:
        # 「xx同属（英文名：XXX）」格式：整名无法解析，但括号内为具体
        # 物种英文名，提取后直接解析（一个文件夹对应一个物种）
        m_en = re.search(r"同属（英文名：(.+?)）", kind_name)
        if m_en:
            en_name = m_en.group(1).strip()
            taxon_id, sci, common, note = resolve_taxon_info(en_name)
            if taxon_id is not None:
                # 解析成功：报告同时记录原始文件夹名与英文名
                report_lines.append(
                    f"  「{kind_name}」按英文名「{en_name}」解析")
        else:
            taxon_id, sci, common, note = resolve_taxon_info(kind_name)
    report_lines.append(f"  {note}")
    if rel_path and rel_path != kind_name:
        report_lines.append(f"  层级: {rel_path}")
    if taxon_id is None:
        # 叶名（直接含图片的最深层文件夹名）未命中时按层级回退解析，
        # 适应「物种_学名/来源子文件夹/图片」等以来源命名叶层的组织方式
        # （含拖入根之外的上级目录名，适应叶层以编号命名的拖入结构）
        for cand in _taxon_name_candidates(kind_name, rel_path, root_ancestors):
            # 回退候选一律用严格匹配校验：解析结果必须与候选名相关，
            # 防止路径中的非物种名（fix/train/编号等）被模糊匹配到无关类群
            tid, s2, c2, n2 = resolve_taxon_info(cand, strict=True)
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
    from download_inaturalist import slugify
    folder_name = f"taxon_{taxon_id}_{slugify(sci)}_{slugify(common)}"
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
        tree_nodes = fetch_taxon_tree_nodes(taxon_id)
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
        sha = _sha256_of_file(img)
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
        width, height = _image_size(dest)
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


def process_drop_folder(path: Path, out_dir: Path, db_dir: Path,
                        taxon_token: str = "") -> str:
    """处理拖入的图片文件夹：按种类区分 → 依次指纹编码入库。

    文件夹结构支持：
        1. 多级结构（如 纲-目-科-种）：自动下钻到每个"直接包含图片"
           的最深层文件夹（种），以文件夹名解析类群、分别建 taxon_ 目录；
        2. 单种类：根层直接放图片，以根文件夹名解析类群；
        3. 指定 taxon_token 时，所有图片统一归入该类群（此时忽略说明文本）。
    """
    from inaturalist_db_manager import INaturalistDatabaseManager

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
                          if p.is_file() and p.suffix.lower() in IMG_SUFFIXES)
        if all_imgs:
            kinds.append((taxon_token, all_imgs, "", ""))
    else:
        kinds = collect_leaf_kinds(path)

    if not kinds:
        report_lines.append("未发现图片文件（支持 jpg/jpeg/png/webp/gif）。")
        report_lines.append("用法：包含图片的最深层文件夹名应为物种名/中文名/类群ID，")
        report_lines.append("      支持纲-目-科-种等多级结构自动下钻到种级，")
        report_lines.append("      或在调用时用 --taxon 统一指定类群。")
        return "\n".join(report_lines)

    manager = INaturalistDatabaseManager(db_dir / "raw_data.db",
                                         db_dir / "bird_observations.db")
    batch_id = manager.create_batch_run(
        quality_grade="local_import", photo_size="original",
        output_dir=str(out_dir))
    # 拖入根目录的上级文件夹名：叶层以编号命名（如 BIRD1445照片）时，
    # 物种名在拖入根之外，作为回退解析的最后一级候选
    root_ancestors = _dragged_root_ancestors(path)
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
            new, dup, fail, skip = _import_images_for_kind(
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


def process_drop_images(paths: list[Path], out_dir: Path, db_dir: Path,
                        taxon_token: str = "") -> str:
    """处理拖入的零散图片文件：按所属文件夹名区分类群后入库。

    paths 为用户一次拖入/选择的若干图片文件；指定 taxon_token 时
    全部统一归入该类群，否则以各自父文件夹名解析。
    """
    from inaturalist_db_manager import INaturalistDatabaseManager

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

    manager = INaturalistDatabaseManager(db_dir / "raw_data.db",
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
            new, dup, fail, skip = _import_images_for_kind(
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

def process_drop_file(path: Path, max_per_taxon: int, workers: int,
                      photo_size: str, quality_grade: str) -> str:
    """处理单个拖入文件：解析 → 逐类群爬取 → 返回报告文本。"""
    # 延迟导入，保持模块加载轻量
    from download_inaturalist import download_taxon_data

    report_lines = [
        f"拖入文件爬取报告",
        f"源文件: {path.name}",
        f"处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"参数: 每类群上限={max_per_taxon}, 尺寸={photo_size}, "
        f"质量={quality_grade}, 线程={workers}",
        "-" * 56,
    ]

    rows = parse_drop_file(path)
    if not rows:
        report_lines.append("文件中没有有效内容（应为每行一个类群 ID 或物种名）。")
        return "\n".join(report_lines)

    for i, row in enumerate(rows, 1):
        # 种类级进度行：GUI 进度条按此刷新（爬取模式无照片级进度）
        print(f"[进度] 种类 {i}/{len(rows)}「{row}」", flush=True)
        taxon_id, note = resolve_taxon_id(row)
        report_lines.append(f"[{i}/{len(rows)}] {note}")
        if taxon_id is None:
            continue
        try:
            summary = download_taxon_data(
                taxon_id=taxon_id,
                out_dir=DATA_DIR,
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

def handle_file(path: Path, args: argparse.Namespace) -> None:
    """处理一个拖入文本文件：爬取 → 保存报告 → 移动到已处理目录。"""
    print(f"\n>>> 检测到拖入文件: {path}")
    processed_dir = Path(args.inbox) / PROCESSED_DIRNAME
    processed_dir.mkdir(parents=True, exist_ok=True)

    try:
        report = process_drop_file(
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


def handle_folder(path: Path, args: argparse.Namespace,
                  write_marker: bool = False,
                  move_to_processed: bool = False) -> None:
    """处理拖入的图片文件夹：按种类导入 → 保存报告。

    write_marker: 在文件夹内写完成标记（供常驻监视防重复处理）；
    move_to_processed: 处理后把文件夹移动到已处理目录（仅监视模式使用，
    避免投放目录被重复扫描；拖入的用户原始文件夹始终保留原位）。
    """
    print(f"\n>>> 检测到拖入文件夹: {path}")
    processed_dir = Path(args.inbox) / PROCESSED_DIRNAME
    processed_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = process_drop_folder(
            path, out_dir=Path(args.out_dir), db_dir=Path(args.db_dir),
            taxon_token=(args.taxon or "").strip())
        print(report)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = processed_dir / f"{path.name}_导入报告_{stamp}.txt"
        report_path.write_text(report, encoding="utf-8")
        print(f"报告已保存: {report_path.name}")
        if write_marker:
            try:
                (path / IMPORT_MARKER).write_text(report, encoding="utf-8")
            except OSError:
                pass
        if move_to_processed:
            shutil.move(str(path), str(processed_dir / f"{stamp}_{path.name}"))
    except Exception as exc:
        print(f"处理失败: {exc}")
    print("<<< 处理结束，等待下一个文件...\n")


def handle_images(paths: list[Path], args: argparse.Namespace,
                  move_to_processed: bool = False) -> None:
    """处理拖入的零散图片：按所属文件夹名/指定类群入库 → 保存报告。"""
    print(f"\n>>> 检测到拖入图片 {len(paths)} 张")
    processed_dir = Path(args.inbox) / PROCESSED_DIRNAME
    processed_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = process_drop_images(
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


def _dir_snapshot(folder: Path) -> tuple[int, int]:
    """递归统计文件夹的条目数与总字节数，用于判断复制是否仍在进行。"""
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


def watch_inbox(args: argparse.Namespace) -> None:
    """常驻监视投放目录，自动处理新增的文本文件、图片与文件夹。

    文件夹采用「快照稳定」判定：连续 3 轮扫描（约 6 秒）条目数与总大小
    都不变，才认为复制已完成并开始处理，避免大文件夹复制中途被抢跑
    （2026-10-14 用户实测：200 类群数据集复制中途被处理，仅入库一小部分）。
    """
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
            if item.name == PROCESSED_DIRNAME:
                continue
            seen_now.add(item)
            if item.is_dir():
                # 文件夹：跳过已写入完成标记的
                if (item / IMPORT_MARKER).exists():
                    continue
                snap = _dir_snapshot(item)
                prev = stable_marks.get(item)
                rounds = prev[1] + 1 if prev and prev[0] == snap else 1
                stable_marks[item] = (snap, rounds)
                # 连续 3 轮快照不变（约 6 秒无变化）才开始处理
                if rounds < 3:
                    continue
                print(f"    （{item.name} 快照已稳定 {rounds} 轮，开始处理）")
                handle_folder(item, args, write_marker=True,
                              move_to_processed=True)
                stable_marks.pop(item, None)
            elif item.is_file():
                if item.suffix.lower() in SUPPORTED_SUFFIXES:
                    handle_file(item, args)
                elif item.suffix.lower() in IMG_SUFFIXES:
                    handle_images([item], args, move_to_processed=True)
        # 清理已消失（被处理/移走/删除）条目的稳定计数，防止字典膨胀
        for gone in [k for k in stable_marks if k not in seen_now]:
            stable_marks.pop(gone, None)
        time.sleep(2)


def run_cli(args: argparse.Namespace) -> int:
    """命令行入口：一次性处理或常驻监视。

    --file 可传多个：文件夹走按种类导入；文本清单走爬取；
    图片按所属文件夹名（或 --taxon）区分类群后依次指纹编码入库。
    """
    files = [Path(p) for p in (args.file or [])]
    if not files:
        if args.watch:
            try:
                watch_inbox(args)
            except KeyboardInterrupt:
                print("\n监视已停止。")
            return 0
        print("请指定 --file 文件/文件夹路径（可多个）或 --watch 常驻监视。")
        return 1

    image_group: list[Path] = []
    for path in files:
        if path.is_dir():
            handle_folder(path, args)
        elif path.is_file():
            if path.suffix.lower() in IMG_SUFFIXES:
                image_group.append(path)
            elif path.suffix.lower() in SUPPORTED_SUFFIXES:
                handle_file(path, args)
            else:
                print(f"不支持的文件类型（仅支持 txt/csv/list 与图片）: {path}")
        else:
            print(f"路径不存在: {path}")
    if image_group:
        handle_images(image_group, args)
    return 0


def main() -> int:
    """独立运行入口。"""
    parser = argparse.ArgumentParser(description="拖入文件自动爬取接口")
    parser.add_argument("--file", nargs="*", default=[],
                        help="要处理的文件/文件夹路径，可一次传多个（拖到 bat 上时自动传入）")
    parser.add_argument("--watch", action="store_true", help="常驻监视 drop_inbox 目录")
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX, help="投放目录")
    parser.add_argument("--out-dir", type=Path, default=DATA_DIR, help="输出根目录")
    parser.add_argument("--db-dir", type=Path, default=DB_DIR, help="数据库目录")
    parser.add_argument("--taxon", default="",
                        help="统一归属类群（物种名/中文名/ID），覆盖文件夹名判定")
    parser.add_argument("--max-per-taxon", type=int, default=50, help="每个类群爬取上限（默认 50）")
    parser.add_argument("--workers", type=int, default=6, help="图片下载线程数")
    parser.add_argument("--photo-size", choices=["original", "large", "medium"], default="large")
    parser.add_argument("--quality-grade", choices=["research", "needs_id", "casual", "any"],
                        default="research")
    return run_cli(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
