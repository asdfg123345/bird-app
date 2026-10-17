#!/usr/bin/env python3
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-07
"""iNaturalist 鸟类下载项目数据库管理模块。

封装两层数据库（raw_data.db + bird_observations.db）的所有写入与查询操作，
包括类群登记、批次创建、原始/规范化数据写入、去重与常用查询接口。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from inaturalist_db_initializer import init_all_databases


def _normalize_text(text: Any) -> str:
    """文本标准化：去括号内容、去标点、转小写、去空白。"""
    if text is None:
        return ""
    text = str(text)
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"[^\w\s]", "", text)
    text = text.lower()
    text = re.sub(r"\s+", "", text)
    return text


def generate_observation_fingerprint(obs: dict[str, Any]) -> str:
    """基于观察记录关键字段生成 SHA-256 指纹。"""
    parts = [
        str(obs.get("observation_id", "")),
        _normalize_text(obs.get("scientific_name", "")),
        _normalize_text(obs.get("observed_on", "")),
        str(obs.get("latitude", "")),
        str(obs.get("longitude", "")),
        _normalize_text(obs.get("description", "")),
    ]
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def generate_photo_fingerprint(photo: dict[str, Any]) -> str:
    """基于照片关键字段生成 SHA-256 指纹。"""
    parts = [
        str(photo.get("photo_id", "")),
        str(photo.get("observation_id", "")),
        str(photo.get("original_url", "")),
        str(photo.get("local_path", "")),
    ]
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _json_fingerprint(obj: Any) -> str:
    """对任意对象按 key 排序后生成 JSON 并计算 SHA-256。"""
    canonical = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_int(value: Any) -> int | None:
    """将值安全转换为整数，失败返回 None。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _to_float(value: Any) -> float | None:
    """将值安全转换为浮点数，失败返回 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _to_bool_int(value: Any) -> int | None:
    """将布尔值或文本转换为 0/1 整数，失败返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value else 0
    if str(value).lower() in ("true", "yes", "1", "是"):
        return 1
    if str(value).lower() in ("false", "no", "0", "否"):
        return 0
    return None


class INaturalistDatabaseManager:
    """iNaturalist 两层数据库管理器。"""

    def __init__(
        self,
        raw_db_path: Path | str,
        main_db_path: Path | str,
        batch_uuid: str | None = None,
    ) -> None:
        """初始化管理器，自动创建并连接两个数据库。"""
        self.raw_db_path = Path(raw_db_path)
        self.main_db_path = Path(main_db_path)
        self.batch_uuid = batch_uuid or str(uuid.uuid4())
        self._ensure_databases()
        self._raw_conn: sqlite3.Connection | None = None
        self._main_conn: sqlite3.Connection | None = None

    def _ensure_databases(self) -> None:
        """确保两个数据库已初始化。"""
        init_all_databases(self.raw_db_path, self.main_db_path)

    def _get_raw_conn(self) -> sqlite3.Connection:
        """获取原始库连接。"""
        if self._raw_conn is None:
            self._raw_conn = sqlite3.connect(self.raw_db_path)
            self._raw_conn.execute("PRAGMA foreign_keys = ON")
        return self._raw_conn

    def _get_main_conn(self) -> sqlite3.Connection:
        """获取主库连接。"""
        if self._main_conn is None:
            self._main_conn = sqlite3.connect(self.main_db_path)
            self._main_conn.execute("PRAGMA foreign_keys = ON")
        return self._main_conn

    def close(self) -> None:
        """关闭两个数据库连接。"""
        if self._raw_conn is not None:
            self._raw_conn.close()
            self._raw_conn = None
        if self._main_conn is not None:
            self._main_conn.close()
            self._main_conn = None

    def __enter__(self) -> "INaturalistDatabaseManager":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 类群管理
    # ------------------------------------------------------------------
    def ensure_taxon(
        self,
        taxon_id: int,
        scientific_name: str,
        common_name: str,
        rank: str,
        label_cn: str = "",
    ) -> None:
        """登记或更新类群信息。"""
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._get_main_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO taxa (taxon_id, scientific_name, common_name, rank, label_cn, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(taxon_id) DO UPDATE SET
                scientific_name = excluded.scientific_name,
                common_name = excluded.common_name,
                rank = excluded.rank,
                label_cn = COALESCE(NULLIF(excluded.label_cn, ''), taxa.label_cn),
                last_seen_at = excluded.last_seen_at
            """,
            (taxon_id, scientific_name, common_name, rank, label_cn, now, now),
        )
        conn.commit()

    def upsert_taxon_tree_bulk(self, nodes: list[dict[str, Any]]) -> int:
        """批量登记类群生物学层级节点（来自 iNaturalist 官方 ancestry 链）。

        每个 node 字典需含 taxon_id/rank/name_sci/name_cn/parent_id/ancestry_ids。
        返回登记节点数。
        """
        if not nodes:
            return 0
        conn = self._get_main_conn()
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT INTO taxon_tree (taxon_id, rank, name_sci, name_cn, parent_id, ancestry_ids, fetched_at)
            VALUES (:taxon_id, :rank, :name_sci, :name_cn, :parent_id, :ancestry_ids, CURRENT_TIMESTAMP)
            ON CONFLICT(taxon_id) DO UPDATE SET
                rank = excluded.rank,
                name_sci = excluded.name_sci,
                name_cn = COALESCE(NULLIF(excluded.name_cn, ''), taxon_tree.name_cn),
                parent_id = excluded.parent_id,
                ancestry_ids = excluded.ancestry_ids,
                fetched_at = CURRENT_TIMESTAMP
            """,
            nodes,
        )
        conn.commit()
        return len(nodes)

    def load_taxon_tree(self) -> dict[int, dict[str, Any]]:
        """加载全部分类层级节点，返回 {taxon_id: 节点字典}。"""
        conn = self._get_main_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT taxon_id, rank, name_sci, name_cn, parent_id, ancestry_ids FROM taxon_tree"
        )
        result: dict[int, dict[str, Any]] = {}
        for row in cursor.fetchall():
            result[int(row[0])] = {
                "taxon_id": int(row[0]),
                "rank": row[1] or "",
                "name_sci": row[2] or "",
                "name_cn": row[3] or "",
                "parent_id": row[4],
                "ancestry_ids": row[5] or "",
            }
        return result

    # ------------------------------------------------------------------
    # 批次管理
    # ------------------------------------------------------------------
    def create_batch_run(
        self,
        batch_time: str | None = None,
        taxa_requested: int = 0,
        quality_grade: str = "",
        photo_size: str = "",
        max_per_taxon: int = 0,
        output_dir: str = "",
        total_observations: int = 0,
        total_photos: int = 0,
        total_downloaded: int = 0,
        total_failed: int = 0,
    ) -> int:
        """创建批次运行记录，返回 batch_id。"""
        conn = self._get_main_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO batch_runs
            (batch_uuid, batch_time, taxa_requested, taxa_succeeded, quality_grade,
             photo_size, max_per_taxon, output_dir, total_observations, total_photos,
             total_downloaded, total_failed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.batch_uuid,
                batch_time or datetime.now().isoformat(timespec="seconds"),
                taxa_requested,
                0,
                quality_grade,
                photo_size,
                max_per_taxon,
                output_dir,
                total_observations,
                total_photos,
                total_downloaded,
                total_failed,
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def update_batch_run(
        self,
        batch_id: int,
        taxa_succeeded: int | None = None,
        total_observations: int | None = None,
        total_photos: int | None = None,
        total_downloaded: int | None = None,
        total_failed: int | None = None,
    ) -> None:
        """更新批次汇总统计。"""
        conn = self._get_main_conn()
        cursor = conn.cursor()
        fields: list[str] = []
        values: list[Any] = []
        for name, value in (
            ("taxa_succeeded", taxa_succeeded),
            ("total_observations", total_observations),
            ("total_photos", total_photos),
            ("total_downloaded", total_downloaded),
            ("total_failed", total_failed),
        ):
            if value is not None:
                fields.append(f"{name} = ?")
                values.append(value)
        if not fields:
            return
        values.append(batch_id)
        cursor.execute(
            f"UPDATE batch_runs SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        conn.commit()

    def record_batch_taxon(
        self,
        batch_id: int,
        taxon_id: int,
        observation_count: int,
        photo_count: int,
        photos_downloaded: int,
        photos_failed: int,
    ) -> None:
        """记录每批次每个类群的统计。"""
        conn = self._get_main_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO batch_taxa (batch_id, taxon_id, observation_count, photo_count, photos_downloaded, photos_failed)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(batch_id, taxon_id) DO UPDATE SET
                observation_count = excluded.observation_count,
                photo_count = excluded.photo_count,
                photos_downloaded = excluded.photos_downloaded,
                photos_failed = excluded.photos_failed
            """,
            (batch_id, taxon_id, observation_count, photo_count, photos_downloaded, photos_failed),
        )
        conn.commit()

    def link_batch_observation(
        self,
        batch_id: int,
        observation_id: int,
    ) -> None:
        """建立批次与观察记录的关联。"""
        conn = self._get_main_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO batch_observations (batch_id, observation_id)
            VALUES (?, ?)
            """,
            (batch_id, observation_id),
        )
        conn.commit()

    def delete_batch_run(self, batch_id: int) -> None:
        """删除批次运行记录及其关联行（batch_taxa / batch_observations）。

        供拖入导入在本次未入库任何照片时调用，避免遗留空批次记录
        （健康检查 C4 项）。
        """
        conn = self._get_main_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM batch_taxa WHERE batch_id = ?", (batch_id,))
        cursor.execute("DELETE FROM batch_observations WHERE batch_id = ?",
                       (batch_id,))
        cursor.execute("DELETE FROM batch_runs WHERE id = ?", (batch_id,))
        conn.commit()

    # ------------------------------------------------------------------
    # 观察记录与照片写入
    # ------------------------------------------------------------------
    def insert_raw_observation(
        self,
        obs: dict[str, Any],
        raw_json: dict[str, Any],
        crawl_time: str,
    ) -> None:
        """写入原始观察记录（INSERT OR IGNORE）。"""
        # 原始层指纹：结合业务主键与原始 JSON，避免空 JSON 导致重复
        fingerprint = _json_fingerprint(
            {
                "observation_id": obs.get("observation_id"),
                "uuid": obs.get("uuid"),
                "raw": raw_json,
            }
        )
        conn = self._get_raw_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO raw_observations (
                观察记录ID, UUID, 链接, 质量等级, 学名, 俗名, 类群ID, 类群等级,
                猜测种名, 观察日期, 观察时间, 时区, 纬度, 经度, 地点猜测, 地点名称,
                位置精度, 公开位置精度, 地理隐私, 类群地理隐私, 是否模糊, 是否可映射,
                是否圈养, 描述, 观察许可证, 观察者ID, 观察者登录名, 观察者姓名,
                鉴定数, 照片数, 声音数, 本地照片文件, 原始JSON, 采集时间, 批次ID, fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _to_int(obs.get("observation_id")),
                obs.get("uuid"),
                obs.get("uri"),
                obs.get("quality_grade"),
                obs.get("scientific_name"),
                obs.get("common_name"),
                _to_int(obs.get("taxon_id")),
                obs.get("taxon_rank"),
                obs.get("species_guess"),
                obs.get("observed_on"),
                obs.get("time_observed_at"),
                obs.get("time_zone"),
                _to_float(obs.get("latitude")),
                _to_float(obs.get("longitude")),
                obs.get("place_guess"),
                obs.get("place_names"),
                _to_int(obs.get("positional_accuracy_m")),
                _to_int(obs.get("public_positional_accuracy_m")),
                obs.get("geoprivacy"),
                obs.get("taxon_geoprivacy"),
                _to_bool_int(obs.get("obscured")),
                _to_bool_int(obs.get("mappable")),
                _to_bool_int(obs.get("captive")),
                obs.get("description"),
                obs.get("observation_license"),
                _to_int(obs.get("observer_id")),
                obs.get("observer_login"),
                obs.get("observer_name"),
                _to_int(obs.get("identifications_count")),
                _to_int(obs.get("photo_count")),
                _to_int(obs.get("sound_count")),
                obs.get("local_photo_files"),
                json.dumps(raw_json, ensure_ascii=False, default=str),
                crawl_time,
                self.batch_uuid,
                fingerprint,
            ),
        )
        conn.commit()

    def insert_raw_photo(
        self,
        photo: dict[str, Any],
        raw_json: dict[str, Any],
        crawl_time: str,
    ) -> None:
        """写入原始照片（INSERT OR IGNORE）。"""
        # 原始层指纹：结合业务主键与原始 JSON，避免空 JSON 导致重复
        fingerprint = _json_fingerprint(
            {
                "photo_id": photo.get("photo_id"),
                "observation_id": photo.get("observation_id"),
                "raw": raw_json,
            }
        )
        conn = self._get_raw_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO raw_photos (
                观察记录ID, 照片ID, 位置, 许可协议, 署名, 宽度, 高度, 请求尺寸,
                实际下载尺寸, 原始URL, 本地路径, 下载成功, 原始JSON, 采集时间, 批次ID, fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _to_int(photo.get("observation_id")),
                _to_int(photo.get("photo_id")),
                _to_int(photo.get("position")),
                photo.get("license_code"),
                photo.get("attribution"),
                _to_int(photo.get("width")),
                _to_int(photo.get("height")),
                photo.get("requested_size"),
                photo.get("downloaded_size"),
                photo.get("original_url"),
                photo.get("local_path"),
                _to_bool_int(photo.get("download_ok")),
                json.dumps(raw_json, ensure_ascii=False, default=str),
                crawl_time,
                self.batch_uuid,
                fingerprint,
            ),
        )
        conn.commit()

    def upsert_observation(self, obs: dict[str, Any]) -> None:
        """写入或更新规范化观察记录（upsert）。"""
        # 如果观察记录带有类群信息，先确保类群已登记
        taxon_id = _to_int(obs.get("taxon_id"))
        if taxon_id is not None:
            self.ensure_taxon(
                taxon_id=taxon_id,
                scientific_name=obs.get("scientific_name", ""),
                common_name=obs.get("common_name", ""),
                rank=obs.get("taxon_rank", ""),
                label_cn="",
            )
        fingerprint = generate_observation_fingerprint(obs)
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._get_main_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO observations (
                observation_id, uuid, uri, quality_grade, scientific_name, common_name,
                taxon_id, taxon_rank, species_guess, observed_on, time_observed_at, time_zone,
                latitude, longitude, place_guess, place_names, positional_accuracy_m,
                public_positional_accuracy_m, geoprivacy, taxon_geoprivacy, obscured, mappable,
                captive, description, observation_license, observer_id, observer_login,
                observer_name, identifications_count, photo_count, sound_count, local_photo_files,
                fingerprint, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(observation_id) DO UPDATE SET
                uuid = excluded.uuid,
                uri = excluded.uri,
                quality_grade = excluded.quality_grade,
                scientific_name = excluded.scientific_name,
                common_name = excluded.common_name,
                taxon_id = excluded.taxon_id,
                taxon_rank = excluded.taxon_rank,
                species_guess = excluded.species_guess,
                observed_on = excluded.observed_on,
                time_observed_at = excluded.time_observed_at,
                time_zone = excluded.time_zone,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                place_guess = excluded.place_guess,
                place_names = excluded.place_names,
                positional_accuracy_m = excluded.positional_accuracy_m,
                public_positional_accuracy_m = excluded.public_positional_accuracy_m,
                geoprivacy = excluded.geoprivacy,
                taxon_geoprivacy = excluded.taxon_geoprivacy,
                obscured = excluded.obscured,
                mappable = excluded.mappable,
                captive = excluded.captive,
                description = excluded.description,
                observation_license = excluded.observation_license,
                observer_id = excluded.observer_id,
                observer_login = excluded.observer_login,
                observer_name = excluded.observer_name,
                identifications_count = excluded.identifications_count,
                photo_count = excluded.photo_count,
                sound_count = excluded.sound_count,
                local_photo_files = excluded.local_photo_files,
                fingerprint = excluded.fingerprint,
                updated_at = excluded.updated_at
            """,
            (
                _to_int(obs.get("observation_id")),
                obs.get("uuid"),
                obs.get("uri"),
                obs.get("quality_grade"),
                obs.get("scientific_name"),
                obs.get("common_name"),
                _to_int(obs.get("taxon_id")),
                obs.get("taxon_rank"),
                obs.get("species_guess"),
                obs.get("observed_on"),
                obs.get("time_observed_at"),
                obs.get("time_zone"),
                _to_float(obs.get("latitude")),
                _to_float(obs.get("longitude")),
                obs.get("place_guess"),
                obs.get("place_names"),
                _to_int(obs.get("positional_accuracy_m")),
                _to_int(obs.get("public_positional_accuracy_m")),
                obs.get("geoprivacy"),
                obs.get("taxon_geoprivacy"),
                _to_bool_int(obs.get("obscured")),
                _to_bool_int(obs.get("mappable")),
                _to_bool_int(obs.get("captive")),
                obs.get("description"),
                obs.get("observation_license"),
                _to_int(obs.get("observer_id")),
                obs.get("observer_login"),
                obs.get("observer_name"),
                _to_int(obs.get("identifications_count")),
                _to_int(obs.get("photo_count")),
                _to_int(obs.get("sound_count")),
                obs.get("local_photo_files"),
                fingerprint,
                now,
                now,
            ),
        )
        conn.commit()

    def upsert_photo(self, photo: dict[str, Any]) -> None:
        """写入或更新规范化照片（upsert）。"""
        fingerprint = generate_photo_fingerprint(photo)
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._get_main_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO photos (
                photo_id, observation_id, position, license_code, attribution,
                width, height, requested_size, downloaded_size, original_url,
                local_path, download_ok, fingerprint, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(photo_id) DO UPDATE SET
                observation_id = excluded.observation_id,
                position = excluded.position,
                license_code = excluded.license_code,
                attribution = excluded.attribution,
                width = excluded.width,
                height = excluded.height,
                requested_size = excluded.requested_size,
                downloaded_size = excluded.downloaded_size,
                original_url = excluded.original_url,
                local_path = excluded.local_path,
                download_ok = excluded.download_ok,
                fingerprint = excluded.fingerprint,
                updated_at = excluded.updated_at
            """,
            (
                _to_int(photo.get("photo_id")),
                _to_int(photo.get("observation_id")),
                _to_int(photo.get("position")),
                photo.get("license_code"),
                photo.get("attribution"),
                _to_int(photo.get("width")),
                _to_int(photo.get("height")),
                photo.get("requested_size"),
                photo.get("downloaded_size"),
                photo.get("original_url"),
                photo.get("local_path"),
                _to_bool_int(photo.get("download_ok")),
                fingerprint,
                now,
                now,
            ),
        )
        conn.commit()

    def write_page_data(
        self,
        observations: list[dict[str, Any]],
        photo_rows: list[dict[str, Any]],
        raw_observations: list[dict[str, Any]] | None = None,
        crawl_time: str | None = None,
    ) -> None:
        """批量写入一页观察记录与照片到两层数据库。

        参数：
            observations: 规范化观察记录行列表
            photo_rows: 规范化照片行列表
            raw_observations: 原始 API observation 对象列表（可选）
            crawl_time: 采集时间戳
        """
        now = crawl_time or datetime.now().isoformat(timespec="seconds")
        raw_lookup: dict[int, dict[str, Any]] = {}
        if raw_observations:
            for raw in raw_observations:
                oid = raw.get("id")
                if oid is not None:
                    raw_lookup[int(oid)] = raw

        for obs in observations:
            self.upsert_observation(obs)
            raw_json = raw_lookup.get(int(obs.get("observation_id") or 0), {})
            self.insert_raw_observation(obs, raw_json, now)

        for photo in photo_rows:
            self.upsert_photo(photo)
            raw_json = raw_lookup.get(int(photo.get("observation_id") or 0), {})
            self.insert_raw_photo(photo, raw_json, now)

    # ------------------------------------------------------------------
    # 常用查询接口
    # ------------------------------------------------------------------
    def get_observation_count(self) -> int:
        """获取主库观察记录总数。"""
        conn = self._get_main_conn()
        row = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
        return row[0] if row else 0

    def get_photo_count(self) -> int:
        """获取主库照片总数。"""
        conn = self._get_main_conn()
        row = conn.execute("SELECT COUNT(*) FROM photos").fetchone()
        return row[0] if row else 0

    def get_taxon_stats(self) -> list[dict[str, Any]]:
        """按类群统计观察记录数与照片数。"""
        conn = self._get_main_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT t.taxon_id, t.scientific_name, t.common_name, t.label_cn,
                   COUNT(DISTINCT o.observation_id) AS observation_count,
                   COUNT(DISTINCT p.photo_id) AS photo_count
            FROM taxa t
            LEFT JOIN observations o ON o.taxon_id = t.taxon_id
            LEFT JOIN photos p ON p.observation_id = o.observation_id
            GROUP BY t.taxon_id
            ORDER BY observation_count DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_failed_photos(self, limit: int = 100) -> list[dict[str, Any]]:
        """获取下载失败的照片列表。"""
        conn = self._get_main_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM photos WHERE download_ok = 0 OR download_ok IS NULL LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_batch_summary(self) -> list[dict[str, Any]]:
        """获取所有批次运行汇总。"""
        conn = self._get_main_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM batch_runs ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def update_observation_local_photo_files(
        self,
        files_by_obs: dict[Any, list[str]],
    ) -> None:
        """批量更新观察记录的本地照片文件列表。

        参数：
            files_by_obs: 键为 observation_id，值为本地照片相对路径列表
        """
        if not files_by_obs:
            return
        conn = self._get_main_conn()
        cursor = conn.cursor()
        now = datetime.now().isoformat(timespec="seconds")
        for observation_id, files in files_by_obs.items():
            local_photo_files = " | ".join(files)
            cursor.execute(
                """
                UPDATE observations
                SET local_photo_files = ?, updated_at = ?
                WHERE observation_id = ?
                """,
                (local_photo_files, now, int(observation_id)),
            )
        conn.commit()


    # ------------------------------------------------------------------
    # 本地图片导入（拖入文件夹按种类入库，依次指纹编码）
    # ------------------------------------------------------------------
    def upsert_local_observation(
        self,
        taxon_id: int,
        scientific_name: str,
        common_name: str,
        taxon_rank: str = "species",
        source_name: str = "",
        note: str = "",
    ) -> int:
        """登记或复用本地导入的容器观察记录，返回本地观察 ID。

        本地观察使用负数 ID（依次递减编码），与 iNaturalist 正数 ID 天然区分；
        同一「类群 + 来源文件夹」只建一条本地观察，重复导入时自动复用。
        """
        fingerprint = hashlib.sha256(
            f"local|{taxon_id}|{source_name}".encode("utf-8")).hexdigest()
        conn = self._get_main_conn()
        row = conn.execute(
            "SELECT observation_id FROM observations WHERE fingerprint = ?",
            (fingerprint,)).fetchone()
        if row:
            return int(row[0])
        row = conn.execute(
            "SELECT MIN(observation_id) FROM observations WHERE observation_id < 0"
        ).fetchone()
        new_id = (int(row[0]) - 1) if row and row[0] is not None else -1
        # 先确保类群已登记
        self.ensure_taxon(
            taxon_id=taxon_id,
            scientific_name=scientific_name,
            common_name=common_name,
            rank=taxon_rank,
            label_cn="",
        )
        now = datetime.now().isoformat(timespec="seconds")
        try:
            conn.execute(
                """
                INSERT INTO observations (
                    observation_id, uri, quality_grade, scientific_name, common_name,
                    taxon_id, taxon_rank, species_guess, description, photo_count,
                    local_photo_files, fingerprint, created_at, updated_at
                ) VALUES (?, '', 'local', ?, ?, ?, ?, ?, ?, 0, '', ?, ?, ?)
                """,
                (new_id, scientific_name, common_name, taxon_id, taxon_rank,
                 source_name, note, fingerprint, now, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # 极端并发下指纹冲突：直接复用已有记录
            row = conn.execute(
                "SELECT observation_id FROM observations WHERE fingerprint = ?",
                (fingerprint,)).fetchone()
            if row:
                return int(row[0])
            raise
        # 原始层同步留存（原始 JSON 记录导入来源）
        local_obs = {
            "observation_id": new_id,
            "scientific_name": scientific_name,
            "common_name": common_name,
            "taxon_id": taxon_id,
            "taxon_rank": taxon_rank,
            "species_guess": source_name,
            "description": note,
            "photo_count": 0,
        }
        self.insert_raw_observation(local_obs, {"source": "local_import"}, now)
        return new_id

    def has_photo_fingerprint(self, content_sha: str) -> bool:
        """检查照片内容指纹是否已存在于库中（跨批次内容级去重）。"""
        conn = self._get_main_conn()
        row = conn.execute(
            "SELECT 1 FROM photos WHERE fingerprint = ? LIMIT 1",
            (content_sha,)).fetchone()
        return row is not None

    def next_local_photo_id(self) -> int:
        """获取下一个本地照片 ID（负数，依次递减编码）。"""
        conn = self._get_main_conn()
        row = conn.execute(
            "SELECT MIN(photo_id) FROM photos WHERE photo_id < 0").fetchone()
        return (int(row[0]) - 1) if row and row[0] is not None else -1

    def upsert_local_photo(
        self,
        photo_id: int,
        observation_id: int,
        content_sha: str,
        original_name: str,
        dest_relative: str,
        file_size: int = 0,
        width: int | None = None,
        height: int | None = None,
        position: int = 0,
    ) -> str:
        """登记本地导入照片，以文件内容 SHA-256 为指纹。

        返回 "inserted"（新入库）或 "duplicate"（内容指纹已存在）。
        """
        conn = self._get_main_conn()
        now = datetime.now().isoformat(timespec="seconds")
        try:
            conn.execute(
                """
                INSERT INTO photos (
                    photo_id, observation_id, position, license_code, attribution,
                    width, height, requested_size, downloaded_size, original_url,
                    local_path, download_ok, fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, 'local', ?, ?, ?, 'original', ?, ?, ?, 1, ?, ?, ?)
                """,
                (photo_id, observation_id, position, f"本地导入: {original_name}",
                 width, height, file_size, f"local://{original_name}",
                 dest_relative, content_sha, now, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return "duplicate"
        # 原始层同步留存
        local_photo = {
            "photo_id": photo_id,
            "observation_id": observation_id,
            "position": position,
            "license_code": "local",
            "attribution": f"本地导入: {original_name}",
            "width": width,
            "height": height,
            "requested_size": "original",
            "downloaded_size": file_size,
            "original_url": f"local://{original_name}",
            "local_path": dest_relative,
            "download_ok": 1,
        }
        self.insert_raw_photo(local_photo, {"source": "local_import"}, now)
        return "inserted"

    def refresh_local_observation_stats(self, observation_id: int) -> None:
        """按 photos 表刷新本地观察的 photo_count 与 local_photo_files。"""
        conn = self._get_main_conn()
        rows = conn.execute(
            "SELECT local_path FROM photos "
            "WHERE observation_id = ? AND download_ok = 1 ORDER BY photo_id",
            (observation_id,)).fetchall()
        files = [r[0] for r in rows]
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE observations SET photo_count = ?, local_photo_files = ?, "
            "updated_at = ? WHERE observation_id = ?",
            (len(files), " | ".join(files), now, observation_id),
        )
        conn.commit()


if __name__ == "__main__":
    # 简单自测：初始化并打印统计
    base = Path(__file__).resolve().parent / "data" / "db"
    manager = INaturalistDatabaseManager(base / "raw_data.db", base / "bird_observations.db")
    print(f"观察记录总数: {manager.get_observation_count()}")
    print(f"照片总数: {manager.get_photo_count()}")
    manager.close()
