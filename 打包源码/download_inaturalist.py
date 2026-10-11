#!/usr/bin/env python3
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-05
"""Download iNaturalist observations, photos, and research metadata.

Uses the official API (https://api.inaturalist.org/v1) rather than HTML scraping.
Default target: taxon_id=781 (Arborophila torqueola / Hill Partridge).

Example:
    python download_inaturalist.py
    python download_inaturalist.py --taxon-id 781 --photo-size original --workers 6
"""

from __future__ import annotations


# ============================================================
# 冻结环境适配：打包为 exe 后，数据目录固定到 exe 所在目录
# ============================================================
def _frozen_base(src_file):
    """打包后返回 exe 所在目录；源码运行返回脚本所在目录。"""
    import sys as _sys
    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(src_file).resolve().parent


import argparse
import csv
import json
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from inaturalist_db_manager import INaturalistDatabaseManager

# Prefer IPv4 to avoid long IPv6 hangs on some networks.
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    records = _orig_getaddrinfo(host, port, family, type, proto, flags)
    ipv4 = [item for item in records if item[0] == socket.AF_INET]
    return ipv4 or records


socket.getaddrinfo = _getaddrinfo_ipv4

API_BASE = "https://api.inaturalist.org/v1"
DEFAULT_TAXON_ID = 781
USER_AGENT = (
    "HillPartridgeDownloader/1.0 "
    "(research; respectful rate limit; contact via local script)"
)
REQUEST_INTERVAL_SEC = 1.1
MAX_RETRIES = 4
PHOTO_TIMEOUT = (10, 40)

PHOTO_CSV_FIELDS = [
    "observation_id",
    "photo_id",
    "position",
    "license_code",
    "attribution",
    "width",
    "height",
    "requested_size",
    "downloaded_size",
    "original_url",
    "local_path",
    "download_ok",
]

OBSERVATION_CSV_FIELDS = [
    "observation_id",
    "uuid",
    "uri",
    "quality_grade",
    "scientific_name",
    "common_name",
    "taxon_id",
    "taxon_rank",
    "species_guess",
    "observed_on",
    "time_observed_at",
    "time_zone",
    "latitude",
    "longitude",
    "place_guess",
    "place_names",
    "positional_accuracy_m",
    "public_positional_accuracy_m",
    "geoprivacy",
    "taxon_geoprivacy",
    "obscured",
    "mappable",
    "captive",
    "description",
    "observation_license",
    "observer_id",
    "observer_login",
    "observer_name",
    "identifications_count",
    "photo_count",
    "sound_count",
    "local_photo_files",
]


def log(message: str) -> None:
    print(message, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download iNaturalist observation photos and research metadata."
    )
    parser.add_argument("--taxon-id", type=int, default=DEFAULT_TAXON_ID)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_frozen_base(__file__) / "data",
    )
    parser.add_argument(
        "--photo-size",
        choices=["original", "large", "medium"],
        default="large",
        help="Preferred photo size. Falls back to a smaller size if needed.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Parallel photo downloads.",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=200,
        help="API page size (max 200).",
    )
    parser.add_argument(
        "--max-observations",
        type=int,
        default=None,
        help="Max observations to fetch (protects against huge taxa like all birds).",
    )
    parser.add_argument(
        "--quality-grade",
        choices=["research", "needs_id", "casual", "any"],
        default="any",
        help="Filter by quality grade (research = community-verified, best images).",
    )
    parser.add_argument(
        "--save-raw-json",
        action="store_true",
        default=False,
        help="保存每页原始 API 数据为 JSONL（默认关闭，节省磁盘）。",
    )
    parser.add_argument(
        "--no-skip-places",
        action="store_true",
        default=False,
        help="启用地名查询（默认关闭，大规模下载可节省大量 API 调用）。",
    )
    parser.add_argument(
        "--db-dir",
        type=Path,
        default=None,
        help="数据库目录（默认 out-dir/db）。",
    )
    parser.add_argument(
        "--raw-db",
        type=Path,
        default=None,
        help="原始数据库路径（默认 db-dir/raw_data.db）。",
    )
    parser.add_argument(
        "--main-db",
        type=Path,
        default=None,
        help="主数据库路径（默认 db-dir/bird_observations.db）。",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        default=False,
        help="禁用数据库存储（默认启用）。",
    )
    return parser.parse_args()


class ApiClient:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
        )

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            self._wait()
            try:
                response = self.session.get(url, params=params, timeout=30)
                self._last = time.monotonic()
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", 5 * attempt))
                    log(f"  rate-limited, waiting {retry_after:.1f}s...")
                    time.sleep(retry_after)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                wait = min(2 ** attempt, 20)
                log(f"  request failed ({exc}); retry {attempt}/{MAX_RETRIES} in {wait}s")
                time.sleep(wait)
        raise RuntimeError(f"Failed GET {url}: {last_error}")


def photo_url_for_size(url: str, size: str) -> str:
    return re.sub(r"/(square|thumb|small|medium|large|original)\.", f"/{size}.", url)


def guess_extension(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        if path.endswith(ext):
            return ext
    return ".jpg"


def size_fallbacks(preferred: str) -> list[str]:
    order = ["original", "large", "medium"]
    if preferred not in order:
        return [preferred]
    start = order.index(preferred)
    return order[start:]


def download_photo_file(url: str, dest: Path) -> bool:
    last_error: Exception | None = None
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(1, MAX_RETRIES + 1):
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with requests.get(url, timeout=PHOTO_TIMEOUT, stream=True, headers=headers) as response:
                if response.status_code == 429:
                    time.sleep(float(response.headers.get("Retry-After", 3 * attempt)))
                    continue
                if response.status_code == 404:
                    return False
                response.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tmp.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            handle.write(chunk)
                if tmp.stat().st_size <= 0:
                    tmp.unlink(missing_ok=True)
                    return False
                tmp.replace(dest)
                return True
        except requests.RequestException as exc:
            last_error = exc
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            time.sleep(min(1.5 * attempt, 8))
    if last_error:
        log(f"  photo failed: {url} ({last_error})")
    return False


def flatten_observation(
    obs: dict[str, Any],
    place_lookup: dict[int, str],
    local_photo_files: list[str],
) -> dict[str, Any]:
    taxon = obs.get("taxon") or {}
    user = obs.get("user") or {}
    geojson = obs.get("geojson") or {}
    coords = geojson.get("coordinates") or []
    longitude = coords[0] if len(coords) >= 2 else None
    latitude = coords[1] if len(coords) >= 2 else None
    if latitude is None and obs.get("location"):
        parts = str(obs["location"]).split(",")
        if len(parts) == 2:
            latitude, longitude = parts[0].strip(), parts[1].strip()

    place_names = []
    for place_id in obs.get("place_ids") or []:
        name = place_lookup.get(int(place_id))
        if name:
            place_names.append(name)

    return {
        "observation_id": obs.get("id"),
        "uuid": obs.get("uuid"),
        "uri": obs.get("uri"),
        "quality_grade": obs.get("quality_grade"),
        "scientific_name": taxon.get("name"),
        "common_name": taxon.get("preferred_common_name"),
        "taxon_id": taxon.get("id"),
        "taxon_rank": taxon.get("rank"),
        "species_guess": obs.get("species_guess"),
        "observed_on": obs.get("observed_on"),
        "time_observed_at": obs.get("time_observed_at"),
        "time_zone": obs.get("observed_time_zone") or obs.get("created_time_zone"),
        "latitude": latitude,
        "longitude": longitude,
        "place_guess": obs.get("place_guess"),
        "place_names": " | ".join(place_names),
        "positional_accuracy_m": obs.get("positional_accuracy"),
        "public_positional_accuracy_m": obs.get("public_positional_accuracy"),
        "geoprivacy": obs.get("geoprivacy"),
        "taxon_geoprivacy": obs.get("taxon_geoprivacy"),
        "obscured": obs.get("obscured"),
        "mappable": obs.get("mappable"),
        "captive": obs.get("captive"),
        "description": obs.get("description"),
        "observation_license": obs.get("license_code"),
        "observer_id": user.get("id"),
        "observer_login": user.get("login"),
        "observer_name": user.get("name"),
        "identifications_count": obs.get("identifications_count"),
        "photo_count": len(obs.get("photos") or []),
        "sound_count": len(obs.get("sounds") or []),
        "local_photo_files": " | ".join(local_photo_files),
    }


def fetch_observations_stream(
    client: ApiClient,
    taxon_id: int,
    per_page: int,
    max_results: int | None = None,
    quality_grade: str | None = None,
):
    """流式分页生成观察记录列表。

    一次只生成一页（默认 200 条），避免一次性加载全部元数据到内存。
    参数：
        max_results: 最多获取的观察记录数量；None 表示不限制
        quality_grade: 质量等级过滤（如 research）
    """
    id_above = 0
    total = None
    page = 1
    yielded = 0
    while True:
        page_size = min(per_page, 200)
        if max_results is not None:
            remaining = max_results - yielded
            if remaining <= 0:
                break
            page_size = min(page_size, remaining)
        params = {
            "taxon_id": taxon_id,
            "order_by": "id",
            "order": "asc",
            "per_page": page_size,
            "id_above": id_above,
        }
        # 按质量等级过滤（research = 研究级，社区已确认物种）
        if quality_grade:
            params["quality_grade"] = quality_grade
        log(f"Fetching observations page {page} (id_above={id_above})...")
        payload = client.get_json(f"{API_BASE}/observations", params)
        if total is None:
            total = payload.get("total_results", 0)
            log(f"API reports {total} observations for taxon_id={taxon_id}")
        results = payload.get("results") or []
        if not results:
            break
        yield results
        yielded += len(results)
        # 达到数量上限时提前停止（大类群保护机制）
        if max_results is not None and yielded >= max_results:
            log(f"Reached max_results={max_results}, stopping pagination.")
            break
        id_above = results[-1]["id"]
        page += 1
        if yielded >= total:
            break


def fetch_places(client: ApiClient, place_ids: list[int]) -> dict[int, str]:
    lookup: dict[int, str] = {}
    unique_ids = sorted(set(place_ids))
    chunk_size = 50
    for i in range(0, len(unique_ids), chunk_size):
        chunk = unique_ids[i : i + chunk_size]
        log(f"Fetching place names {i + 1}-{i + len(chunk)}/{len(unique_ids)}...")
        payload = client.get_json(
            f"{API_BASE}/places/{','.join(str(pid) for pid in chunk)}"
        )
        for place in payload.get("results") or []:
            pid = place.get("id")
            name = place.get("display_name") or place.get("name")
            if pid is not None and name:
                lookup[int(pid)] = name
    return lookup


def init_csv(path: Path, fieldnames: list[str]) -> None:
    """初始化 CSV 文件（写入表头）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()


def append_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    """追加写入 CSV 文件；文件不存在时自动初始化表头。"""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    """覆盖写入 CSV 文件（写入表头和全部数据）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    cleaned = re.sub(r"[-\s]+", "_", cleaned).strip("_")
    return cleaned or "taxon"


def tree_nodes_from_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 iNaturalist taxa API 返回的结果列表转换为 taxon_tree 登记行。

    从每个类群的 ancestry 字段（形如 "48460/1/2/..." 的祖先 ID 链）
    提取父节点，组装为主库 taxon_tree 表的批量登记字典；
    同一节点多次出现时合并字段，中文名优先于英文名。
    """
    merged: dict[int, dict[str, Any]] = {}
    for taxon in payloads:
        if not isinstance(taxon, dict):
            continue
        raw_id = taxon.get("id")
        if raw_id is None:
            continue
        taxon_id = int(raw_id)
        ancestry = str(taxon.get("ancestry") or "").strip("/")
        ancestor_ids = [int(x) for x in ancestry.split("/") if x.strip().isdigit()]
        node = {
            "taxon_id": taxon_id,
            "rank": taxon.get("rank") or "",
            "name_sci": taxon.get("name") or "",
            "name_cn": taxon.get("preferred_common_name") or "",
            "parent_id": ancestor_ids[-1] if ancestor_ids else None,
            "ancestry_ids": ancestry,
        }
        if taxon_id in merged:
            # 同节点合并：中文名优先，缺失字段补全
            old = merged[taxon_id]
            has_cn = lambda s: bool(re.search(r"[\u4e00-\u9fff]", s or ""))
            if not old["name_cn"] or (has_cn(node["name_cn"]) and not has_cn(old["name_cn"])):
                old["name_cn"] = node["name_cn"]
            old["name_sci"] = old["name_sci"] or node["name_sci"]
            old["rank"] = old["rank"] or node["rank"]
            old["parent_id"] = old["parent_id"] if old["parent_id"] is not None else node["parent_id"]
            old["ancestry_ids"] = old["ancestry_ids"] or node["ancestry_ids"]
        else:
            merged[taxon_id] = node
    return list(merged.values())


def sync_taxon_tree(
    api_client: Any,
    taxon_payload: dict[str, Any],
    db_manager: Any,
) -> int:
    """登记当前类群及其全部祖先的层级链（祖先用一次批量 API 请求）。

    祖先请求带 locale=zh-CN 获取中文俗名；当前节点缺中文名时
    补一次带 locale 的详情请求。返回登记节点数。
    """
    ancestry_ids = [
        x for x in str(taxon_payload.get("ancestry") or "").split("/") if x.strip().isdigit()
    ]
    payloads: list[dict[str, Any]] = [taxon_payload]
    if ancestry_ids:
        ancestors = api_client.get_json(
            f"{API_BASE}/taxa/{','.join(ancestry_ids)}?locale=zh-CN")
        payloads.extend(ancestors.get("results") or [])
    # 当前节点补中文俗名（主流程 payload 默认英文 locale，目录命名不受影响）
    if not re.search(r"[\u4e00-\u9fff]",
                     taxon_payload.get("preferred_common_name") or ""):
        try:
            zh = api_client.get_json(
                f"{API_BASE}/taxa/{taxon_payload['id']}?locale=zh-CN")
            zh_taxon = (zh.get("results") or [{}])[0]
            if isinstance(zh_taxon, dict) and zh_taxon.get("id"):
                payloads.insert(0, zh_taxon)
        except Exception:
            pass  # 中文名补全失败不影响层级登记
    nodes = tree_nodes_from_payloads(payloads)
    return db_manager.upsert_taxon_tree_bulk(nodes)


def collect_photo_jobs(
    observations: list[dict[str, Any]],
    photo_dir: Path,
    preferred_size: str,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for obs in observations:
        obs_id = obs.get("id")
        photos = obs.get("observation_photos") or []
        if not photos and obs.get("photos"):
            photos = [{"position": i, "photo": photo} for i, photo in enumerate(obs["photos"])]
        for photo_wrap in photos:
            photo = photo_wrap.get("photo") or photo_wrap
            photo_id = photo.get("id")
            source_url = photo.get("url") or ""
            if not photo_id or not source_url:
                continue
            ext = guess_extension(source_url)
            filename = f"obs_{obs_id}_photo_{photo_id}{ext}"
            dest = photo_dir / filename
            dims = photo.get("original_dimensions") or {}
            jobs.append(
                {
                    "observation_id": obs_id,
                    "photo_id": photo_id,
                    "position": photo_wrap.get("position", 0),
                    "license_code": photo.get("license_code"),
                    "attribution": photo.get("attribution"),
                    "width": dims.get("width"),
                    "height": dims.get("height"),
                    "source_url": source_url,
                    "preferred_size": preferred_size,
                    "dest": dest,
                    "relative": str(Path("photos") / filename),
                }
            )
    return jobs


def run_photo_job(job: dict[str, Any]) -> dict[str, Any]:
    dest: Path = job["dest"]
    relative = job["relative"]
    if dest.exists() and dest.stat().st_size > 0:
        return {
            **{k: v for k, v in job.items() if k not in {"dest", "source_url"}},
            "requested_size": job["preferred_size"],
            "downloaded_size": "existing",
            "original_url": photo_url_for_size(job["source_url"], job["preferred_size"]),
            "local_path": relative,
            "download_ok": True,
            "status": "skipped",
        }

    used_size = None
    used_url = ""
    ok = False
    for size in size_fallbacks(job["preferred_size"]):
        used_size = size
        used_url = photo_url_for_size(job["source_url"], size)
        if download_photo_file(used_url, dest):
            ok = True
            break

    return {
        **{k: v for k, v in job.items() if k not in {"dest", "source_url"}},
        "requested_size": job["preferred_size"],
        "downloaded_size": used_size if ok else "",
        "original_url": used_url,
        "local_path": relative if ok else "",
        "download_ok": ok,
        "status": "downloaded" if ok else "failed",
    }


def download_taxon_data(
    taxon_id: int,
    out_dir: Path,
    photo_size: str = "large",
    workers: int = 6,
    per_page: int = 200,
    max_observations: int | None = None,
    quality_grade: str | None = None,
    save_raw_json: bool = False,
    skip_places: bool = True,
    db_enabled: bool = True,
    raw_db_path: Path | None = None,
    main_db_path: Path | None = None,
    batch_id: int | None = None,
) -> dict[str, Any]:
    """流式下载单个类群的观察元数据与图片，返回汇总信息字典。

    采用分页→下载→追加 CSV 的流式模式，避免一次性加载全部观察数据，
    适合大规模（数万条）鸟类数据下载。
    每个类群的图片自动存放于独立文件夹：out_dir/taxon_{id}_{学名}_{俗名}/photos/
    """
    # 解析数据库路径并初始化管理器
    db_manager: INaturalistDatabaseManager | None = None
    db_batch_id: int | None = batch_id
    if db_enabled:
        db_dir = (out_dir / "db") if raw_db_path is None or main_db_path is None else None
        resolved_raw_db = raw_db_path or (db_dir / "raw_data.db")
        resolved_main_db = main_db_path or (db_dir / "bird_observations.db")
        try:
            db_manager = INaturalistDatabaseManager(resolved_raw_db, resolved_main_db)
            log(f"Database enabled: raw={resolved_raw_db}, main={resolved_main_db}")
        except Exception as exc:
            log(f"  数据库初始化失败，将继续仅输出 CSV/JSON: {exc}")
            db_manager = None

    api_client = ApiClient(REQUEST_INTERVAL_SEC)

    log("Fetching taxon info...")
    taxon_payload = api_client.get_json(f"{API_BASE}/taxa/{taxon_id}")
    taxon = (taxon_payload.get("results") or [{}])[0]
    scientific = taxon.get("name") or f"taxon_{taxon_id}"
    common = taxon.get("preferred_common_name") or scientific
    folder_name = f"taxon_{taxon_id}_{slugify(scientific)}_{slugify(common)}"
    taxon_out_dir = out_dir / folder_name
    photo_dir = taxon_out_dir / "photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    # 清理上次运行中断留下的临时文件
    for leftover in photo_dir.glob("*.part"):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass

    log(f"Taxon: {scientific} ({common})")
    log(f"Output: {taxon_out_dir}")

    # 登记类群信息；单类群运行时自动创建一条批次记录
    if db_manager is not None:
        try:
            db_manager.ensure_taxon(
                taxon_id=taxon_id,
                scientific_name=scientific,
                common_name=common,
                rank=taxon.get("rank", ""),
                label_cn="",
            )
            # 登记纲-目-科-种生物学层级链（祖先一次批量请求，失败不中断爬取）
            try:
                synced = sync_taxon_tree(api_client, taxon, db_manager)
                log(f"Taxonomy tree synced: {synced} nodes")
            except Exception as exc:
                log(f"  类群层级登记失败（不影响爬取）: {exc}")
            if db_batch_id is None:
                db_batch_id = db_manager.create_batch_run(
                    batch_time=datetime.now().isoformat(timespec="seconds"),
                    taxa_requested=1,
                    quality_grade=quality_grade or "any",
                    photo_size=photo_size,
                    max_per_taxon=max_observations or 0,
                    output_dir=str(out_dir),
                )
        except Exception as exc:
            log(f"  数据库类群/批次登记失败: {exc}")

    # 初始化 CSV 文件（空表头）
    observations_csv_path = taxon_out_dir / "observations.csv"
    photos_csv_path = taxon_out_dir / "photos.csv"
    init_csv(observations_csv_path, OBSERVATION_CSV_FIELDS)
    init_csv(photos_csv_path, PHOTO_CSV_FIELDS)

    # 如果需要保存原始 JSON，改为每页追加一行（避免生成超大 JSON）
    raw_jsonl_path = taxon_out_dir / "observations_raw.jsonl" if save_raw_json else None

    # 地名查询可选，大规模下载默认跳过以减少大量额外 API 调用
    place_lookup: dict[int, str] = {}

    total_observations = 0
    total_photos = 0
    total_downloaded = 0
    total_skipped = 0
    total_failed = 0

    # 用于最后补全 local_photo_files 列
    files_by_obs: dict[Any, list[str]] = {}
    # 临时保存每批观察记录，用于最后更新 local_photo_files 后写回 CSV
    all_observation_rows: list[dict[str, Any]] = []

    for page_index, observations in enumerate(
        fetch_observations_stream(
            api_client, taxon_id, per_page,
            max_results=max_observations,
            quality_grade=quality_grade,
        ),
        start=1,
    ):
        total_observations += len(observations)
        log(f"Processing page {page_index}: {len(observations)} observations...")

        # 可选：追加保存原始数据（JSONL 格式，比单个大 JSON 更省内存）
        if raw_jsonl_path is not None:
            with raw_jsonl_path.open("a", encoding="utf-8") as handle:
                for obs in observations:
                    handle.write(json.dumps(obs, ensure_ascii=False) + "\n")

        # 仅当不跳过地名时，才在需要时查询地名
        if not skip_places:
            page_place_ids: list[int] = []
            for obs in observations:
                page_place_ids.extend(int(pid) for pid in (obs.get("place_ids") or []))
            new_ids = [pid for pid in set(page_place_ids) if pid not in place_lookup]
            if new_ids:
                place_lookup.update(fetch_places(api_client, new_ids))

        # 下载本页照片
        jobs = collect_photo_jobs(observations, photo_dir, photo_size)
        total_photos += len(jobs)
        log(
            f"Downloading {len(jobs)} photos with {workers} workers "
            f"({photo_size}, page {page_index})..."
        )

        photo_rows: list[dict[str, Any]] = []
        downloaded = skipped = failed = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(run_photo_job, job) for job in jobs]
            for done_count, future in enumerate(as_completed(futures), start=1):
                row = future.result()
                photo_rows.append(row)
                status = row.pop("status", "")
                if status == "downloaded":
                    downloaded += 1
                elif status == "skipped":
                    skipped += 1
                else:
                    failed += 1
                if row.get("download_ok") and row.get("local_path"):
                    files_by_obs.setdefault(row["observation_id"], []).append(row["local_path"])
                if done_count % 10 == 0 or done_count == len(jobs):
                    log(
                        f"  photos {done_count}/{len(jobs)} "
                        f"(new={downloaded}, existed={skipped}, failed={failed})"
                    )

        total_downloaded += downloaded
        total_skipped += skipped
        total_failed += failed

        # 追加照片 CSV
        photo_rows.sort(key=lambda r: (r.get("observation_id") or 0, r.get("position") or 0))
        append_csv(photos_csv_path, PHOTO_CSV_FIELDS, photo_rows)

        # 追加观察记录 CSV（先不填 local_photo_files，最后统一更新）
        observation_rows = [
            flatten_observation(obs, place_lookup, []) for obs in observations
        ]
        all_observation_rows.extend(observation_rows)
        append_csv(observations_csv_path, OBSERVATION_CSV_FIELDS, observation_rows)

        # 将本页数据写入数据库（失败不影响 CSV 输出）
        if db_manager is not None:
            try:
                crawl_time = datetime.now().isoformat(timespec="seconds")
                db_manager.write_page_data(observation_rows, photo_rows, observations, crawl_time)
                if db_batch_id is not None:
                    for obs_row in observation_rows:
                        db_manager.link_batch_observation(db_batch_id, obs_row["observation_id"])
            except Exception as exc:
                log(f"  数据库写入失败（page {page_index}）: {exc}")

    # 所有分页完成后，补全 local_photo_files 列并重写观察记录 CSV
    for row in all_observation_rows:
        row["local_photo_files"] = " | ".join(files_by_obs.get(row["observation_id"], []))
    write_csv(observations_csv_path, OBSERVATION_CSV_FIELDS, all_observation_rows)

    # 同步更新数据库中的本地照片文件列表
    if db_manager is not None:
        try:
            db_manager.update_observation_local_photo_files(files_by_obs)
        except Exception as exc:
            log(f"  更新数据库本地照片文件列表失败: {exc}")

    log(f"Saved research metadata for {total_observations} observations.")

    summary = {
        "source_url": f"https://www.inaturalist.org/observations?taxon_id={taxon_id}",
        "api": f"{API_BASE}/observations?taxon_id={taxon_id}",
        "taxon_id": taxon_id,
        "scientific_name": scientific,
        "common_name": common,
        "quality_grade": quality_grade or "any",
        "observation_count": total_observations,
        "photo_count": total_photos,
        "photos_downloaded": total_downloaded,
        "photos_skipped_existing": total_skipped,
        "photos_failed": total_failed,
        "photo_size": photo_size,
        "workers": workers,
        "output_dir": str(taxon_out_dir),
        "save_raw_json": save_raw_json,
        "skip_places": skip_places,
    }
    (taxon_out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 写入本类群在本次批次中的统计，并关闭数据库连接
    if db_manager is not None and db_batch_id is not None:
        try:
            db_manager.record_batch_taxon(
                batch_id=db_batch_id,
                taxon_id=taxon_id,
                observation_count=total_observations,
                photo_count=total_photos,
                photos_downloaded=total_downloaded,
                photos_failed=total_failed,
            )
            db_manager.update_batch_run(
                batch_id=db_batch_id,
                taxa_succeeded=1,
                total_observations=total_observations,
                total_photos=total_photos,
                total_downloaded=total_downloaded,
                total_failed=total_failed,
            )
        except Exception as exc:
            log(f"  数据库批次统计写入失败: {exc}")
        finally:
            db_manager.close()

    log("\nDone.")
    log(json.dumps(summary, ensure_ascii=False, indent=2))
    summary["batch_id"] = db_batch_id
    if db_manager is not None:
        summary["raw_db_path"] = str(resolved_raw_db)
        summary["main_db_path"] = str(resolved_main_db)
    return summary


def main() -> int:
    args = parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # 解析数据库路径参数
    db_dir = args.db_dir or (args.out_dir / "db")
    raw_db = args.raw_db or (db_dir / "raw_data.db")
    main_db = args.main_db or (db_dir / "bird_observations.db")

    summary = download_taxon_data(
        taxon_id=args.taxon_id,
        out_dir=args.out_dir,
        photo_size=args.photo_size,
        workers=args.workers,
        per_page=args.per_page,
        max_observations=args.max_observations,
        quality_grade=None if args.quality_grade == "any" else args.quality_grade,
        save_raw_json=args.save_raw_json,
        skip_places=not args.no_skip_places,
        db_enabled=not args.no_db,
        raw_db_path=raw_db,
        main_db_path=main_db,
    )
    return 0 if summary["photos_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
