#!/usr/bin/env python3
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-07

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from inaturalist_db_initializer import _v31e07de3


def _v8df7575a(text: Any) -> str:
    if text is None:
        return ""
    text = str(text)
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"[^\w\s]", "", text)
    text = text.lower()
    text = re.sub(r"\s+", "", text)
    return text


def _v6fef4824(obs: dict[str, Any]) -> str:
    parts = [
        str(obs.get("observation_id", "")),
        _v8df7575a(obs.get("scientific_name", "")),
        _v8df7575a(obs.get("observed_on", "")),
        str(obs.get("latitude", "")),
        str(obs.get("longitude", "")),
        _v8df7575a(obs.get("description", "")),
    ]
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _v8b848431(photo: dict[str, Any]) -> str:
    parts = [
        str(photo.get("photo_id", "")),
        str(photo.get("observation_id", "")),
        str(photo.get("original_url", "")),
        str(photo.get("local_path", "")),
    ]
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _v564558a3(obj: Any) -> str:
    canonical = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _vd3b0a367(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _vd702f24b(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _v127cf39d(value: Any) -> int | None:
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


class _v8b9d65af:

    def __init__(
        self,
        raw_db_path: Path | str,
        main_db_path: Path | str,
        batch_uuid: str | None = None,
    ) -> None:
        self.raw_db_path = Path(raw_db_path)
        self.main_db_path = Path(main_db_path)
        self.batch_uuid = batch_uuid or str(uuid.uuid4())
        self._ensure_databases()
        self._raw_conn: sqlite3.Connection | None = None
        self._main_conn: sqlite3.Connection | None = None

    def _ensure_databases(self) -> None:
        _v31e07de3(self.raw_db_path, self.main_db_path)

    def _get_raw_conn(self) -> sqlite3.Connection:
        if self._raw_conn is None:
            self._raw_conn = sqlite3.connect(self.raw_db_path)
            self._raw_conn.execute("PRAGMA foreign_keys = ON")
        return self._raw_conn

    def _get_main_conn(self) -> sqlite3.Connection:
        if self._main_conn is None:
            self._main_conn = sqlite3.connect(self.main_db_path)
            self._main_conn.execute("PRAGMA foreign_keys = ON")
        return self._main_conn

    def close(self) -> None:
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

    # ------------------------------------------------------------------
    # 观察记录与照片写入
    # ------------------------------------------------------------------
    def insert_raw_observation(
        self,
        obs: dict[str, Any],
        raw_json: dict[str, Any],
        crawl_time: str,
    ) -> None:
        # 原始层指纹：结合业务主键与原始 JSON，避免空 JSON 导致重复
        fingerprint = _v564558a3(
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
                _vd3b0a367(obs.get("observation_id")),
                obs.get("uuid"),
                obs.get("uri"),
                obs.get("quality_grade"),
                obs.get("scientific_name"),
                obs.get("common_name"),
                _vd3b0a367(obs.get("taxon_id")),
                obs.get("taxon_rank"),
                obs.get("species_guess"),
                obs.get("observed_on"),
                obs.get("time_observed_at"),
                obs.get("time_zone"),
                _vd702f24b(obs.get("latitude")),
                _vd702f24b(obs.get("longitude")),
                obs.get("place_guess"),
                obs.get("place_names"),
                _vd3b0a367(obs.get("positional_accuracy_m")),
                _vd3b0a367(obs.get("public_positional_accuracy_m")),
                obs.get("geoprivacy"),
                obs.get("taxon_geoprivacy"),
                _v127cf39d(obs.get("obscured")),
                _v127cf39d(obs.get("mappable")),
                _v127cf39d(obs.get("captive")),
                obs.get("description"),
                obs.get("observation_license"),
                _vd3b0a367(obs.get("observer_id")),
                obs.get("observer_login"),
                obs.get("observer_name"),
                _vd3b0a367(obs.get("identifications_count")),
                _vd3b0a367(obs.get("photo_count")),
                _vd3b0a367(obs.get("sound_count")),
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
        # 原始层指纹：结合业务主键与原始 JSON，避免空 JSON 导致重复
        fingerprint = _v564558a3(
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
                _vd3b0a367(photo.get("observation_id")),
                _vd3b0a367(photo.get("photo_id")),
                _vd3b0a367(photo.get("position")),
                photo.get("license_code"),
                photo.get("attribution"),
                _vd3b0a367(photo.get("width")),
                _vd3b0a367(photo.get("height")),
                photo.get("requested_size"),
                photo.get("downloaded_size"),
                photo.get("original_url"),
                photo.get("local_path"),
                _v127cf39d(photo.get("download_ok")),
                json.dumps(raw_json, ensure_ascii=False, default=str),
                crawl_time,
                self.batch_uuid,
                fingerprint,
            ),
        )
        conn.commit()

    def upsert_observation(self, obs: dict[str, Any]) -> None:
        # 如果观察记录带有类群信息，先确保类群已登记
        taxon_id = _vd3b0a367(obs.get("taxon_id"))
        if taxon_id is not None:
            self.ensure_taxon(
                taxon_id=taxon_id,
                scientific_name=obs.get("scientific_name", ""),
                common_name=obs.get("common_name", ""),
                rank=obs.get("taxon_rank", ""),
                label_cn="",
            )
        fingerprint = _v6fef4824(obs)
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
                _vd3b0a367(obs.get("observation_id")),
                obs.get("uuid"),
                obs.get("uri"),
                obs.get("quality_grade"),
                obs.get("scientific_name"),
                obs.get("common_name"),
                _vd3b0a367(obs.get("taxon_id")),
                obs.get("taxon_rank"),
                obs.get("species_guess"),
                obs.get("observed_on"),
                obs.get("time_observed_at"),
                obs.get("time_zone"),
                _vd702f24b(obs.get("latitude")),
                _vd702f24b(obs.get("longitude")),
                obs.get("place_guess"),
                obs.get("place_names"),
                _vd3b0a367(obs.get("positional_accuracy_m")),
                _vd3b0a367(obs.get("public_positional_accuracy_m")),
                obs.get("geoprivacy"),
                obs.get("taxon_geoprivacy"),
                _v127cf39d(obs.get("obscured")),
                _v127cf39d(obs.get("mappable")),
                _v127cf39d(obs.get("captive")),
                obs.get("description"),
                obs.get("observation_license"),
                _vd3b0a367(obs.get("observer_id")),
                obs.get("observer_login"),
                obs.get("observer_name"),
                _vd3b0a367(obs.get("identifications_count")),
                _vd3b0a367(obs.get("photo_count")),
                _vd3b0a367(obs.get("sound_count")),
                obs.get("local_photo_files"),
                fingerprint,
                now,
                now,
            ),
        )
        conn.commit()

    def upsert_photo(self, photo: dict[str, Any]) -> None:
        fingerprint = _v8b848431(photo)
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
                _vd3b0a367(photo.get("photo_id")),
                _vd3b0a367(photo.get("observation_id")),
                _vd3b0a367(photo.get("position")),
                photo.get("license_code"),
                photo.get("attribution"),
                _vd3b0a367(photo.get("width")),
                _vd3b0a367(photo.get("height")),
                photo.get("requested_size"),
                photo.get("downloaded_size"),
                photo.get("original_url"),
                photo.get("local_path"),
                _v127cf39d(photo.get("download_ok")),
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
        conn = self._get_main_conn()
        row = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
        return row[0] if row else 0

    def get_photo_count(self) -> int:
        conn = self._get_main_conn()
        row = conn.execute("SELECT COUNT(*) FROM photos").fetchone()
        return row[0] if row else 0

    def get_taxon_stats(self) -> list[dict[str, Any]]:
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
        conn = self._get_main_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM photos WHERE download_ok = 0 OR download_ok IS NULL LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_batch_summary(self) -> list[dict[str, Any]]:
        conn = self._get_main_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM batch_runs ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def update_observation_local_photo_files(
        self,
        files_by_obs: dict[Any, list[str]],
    ) -> None:
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
        conn = self._get_main_conn()
        row = conn.execute(
            "SELECT 1 FROM photos WHERE fingerprint = ? LIMIT 1",
            (content_sha,)).fetchone()
        return row is not None

    def next_local_photo_id(self) -> int:
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
    manager = _v8b9d65af(base / "raw_data.db", base / "bird_observations.db")
    print(f"观察记录总数: {manager.get_observation_count()}")
    print(f"照片总数: {manager.get_photo_count()}")
    manager.close()
