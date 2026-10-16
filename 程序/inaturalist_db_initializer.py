#!/usr/bin/env python3
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-09-07

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


# 原始库建表语句
_v0ea5fdb5: list[tuple[str, str]] = [
    (
        "raw_observations",
        """
        CREATE TABLE IF NOT EXISTS raw_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            观察记录ID INTEGER UNIQUE NOT NULL,
            UUID TEXT,
            链接 TEXT,
            质量等级 TEXT,
            学名 TEXT,
            俗名 TEXT,
            类群ID INTEGER,
            类群等级 TEXT,
            猜测种名 TEXT,
            观察日期 TEXT,
            观察时间 TEXT,
            时区 TEXT,
            纬度 REAL,
            经度 REAL,
            地点猜测 TEXT,
            地点名称 TEXT,
            位置精度 INTEGER,
            公开位置精度 INTEGER,
            地理隐私 TEXT,
            类群地理隐私 TEXT,
            是否模糊 INTEGER,
            是否可映射 INTEGER,
            是否圈养 INTEGER,
            描述 TEXT,
            观察许可证 TEXT,
            观察者ID INTEGER,
            观察者登录名 TEXT,
            观察者姓名 TEXT,
            鉴定数 INTEGER,
            照片数 INTEGER,
            声音数 INTEGER,
            本地照片文件 TEXT,
            原始JSON TEXT,
            采集时间 TEXT,
            批次ID TEXT,
            fingerprint TEXT UNIQUE NOT NULL
        )
        """,
    ),
    (
        "raw_photos",
        """
        CREATE TABLE IF NOT EXISTS raw_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            观察记录ID INTEGER,
            照片ID INTEGER UNIQUE NOT NULL,
            位置 INTEGER,
            许可协议 TEXT,
            署名 TEXT,
            宽度 INTEGER,
            高度 INTEGER,
            请求尺寸 TEXT,
            实际下载尺寸 TEXT,
            原始URL TEXT,
            本地路径 TEXT,
            下载成功 INTEGER,
            原始JSON TEXT,
            采集时间 TEXT,
            批次ID TEXT,
            fingerprint TEXT UNIQUE NOT NULL,
            FOREIGN KEY (观察记录ID) REFERENCES raw_observations(观察记录ID)
        )
        """,
    ),
]

_ve63d5931: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_raw_obs_观察记录ID ON raw_observations(观察记录ID)",
    "CREATE INDEX IF NOT EXISTS idx_raw_obs_类群ID ON raw_observations(类群ID)",
    "CREATE INDEX IF NOT EXISTS idx_raw_obs_采集时间 ON raw_observations(采集时间)",
    "CREATE INDEX IF NOT EXISTS idx_raw_obs_fingerprint ON raw_observations(fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_raw_photo_照片ID ON raw_photos(照片ID)",
    "CREATE INDEX IF NOT EXISTS idx_raw_photo_观察记录ID ON raw_photos(观察记录ID)",
    "CREATE INDEX IF NOT EXISTS idx_raw_photo_fingerprint ON raw_photos(fingerprint)",
]

# 主库建表语句
_v132f772a: list[tuple[str, str]] = [
    (
        "taxa",
        """
        CREATE TABLE IF NOT EXISTS taxa (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            taxon_id INTEGER UNIQUE NOT NULL,
            scientific_name TEXT,
            common_name TEXT,
            rank TEXT,
            label_cn TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT
        )
        """,
    ),
    (
        "batch_runs",
        """
        CREATE TABLE IF NOT EXISTS batch_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_uuid TEXT UNIQUE NOT NULL,
            batch_time TEXT,
            taxa_requested INTEGER,
            taxa_succeeded INTEGER,
            quality_grade TEXT,
            photo_size TEXT,
            max_per_taxon INTEGER,
            output_dir TEXT,
            total_observations INTEGER,
            total_photos INTEGER,
            total_downloaded INTEGER,
            total_failed INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
    ),
    (
        "observations",
        """
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id INTEGER UNIQUE NOT NULL,
            uuid TEXT,
            uri TEXT,
            quality_grade TEXT,
            scientific_name TEXT,
            common_name TEXT,
            taxon_id INTEGER,
            taxon_rank TEXT,
            species_guess TEXT,
            observed_on TEXT,
            time_observed_at TEXT,
            time_zone TEXT,
            latitude REAL,
            longitude REAL,
            place_guess TEXT,
            place_names TEXT,
            positional_accuracy_m INTEGER,
            public_positional_accuracy_m INTEGER,
            geoprivacy TEXT,
            taxon_geoprivacy TEXT,
            obscured INTEGER,
            mappable INTEGER,
            captive INTEGER,
            description TEXT,
            observation_license TEXT,
            observer_id INTEGER,
            observer_login TEXT,
            observer_name TEXT,
            identifications_count INTEGER,
            photo_count INTEGER,
            sound_count INTEGER,
            local_photo_files TEXT,
            fingerprint TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (taxon_id) REFERENCES taxa(taxon_id)
        )
        """,
    ),
    (
        "photos",
        """
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id INTEGER UNIQUE NOT NULL,
            observation_id INTEGER,
            position INTEGER,
            license_code TEXT,
            attribution TEXT,
            width INTEGER,
            height INTEGER,
            requested_size TEXT,
            downloaded_size TEXT,
            original_url TEXT,
            local_path TEXT,
            download_ok INTEGER,
            fingerprint TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (observation_id) REFERENCES observations(observation_id)
        )
        """,
    ),
    (
        "batch_observations",
        """
        CREATE TABLE IF NOT EXISTS batch_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,
            observation_id INTEGER,
            UNIQUE(batch_id, observation_id),
            FOREIGN KEY (batch_id) REFERENCES batch_runs(id),
            FOREIGN KEY (observation_id) REFERENCES observations(observation_id)
        )
        """,
    ),
    (
        "batch_taxa",
        """
        CREATE TABLE IF NOT EXISTS batch_taxa (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,
            taxon_id INTEGER,
            observation_count INTEGER,
            photo_count INTEGER,
            photos_downloaded INTEGER,
            photos_failed INTEGER,
            UNIQUE(batch_id, taxon_id),
            FOREIGN KEY (batch_id) REFERENCES batch_runs(id),
            FOREIGN KEY (taxon_id) REFERENCES taxa(taxon_id)
        )
        """,
    ),
    (
        "taxon_tree",
        """
        CREATE TABLE IF NOT EXISTS taxon_tree (
            taxon_id INTEGER PRIMARY KEY,
            rank TEXT,
            name_sci TEXT,
            name_cn TEXT,
            parent_id INTEGER,
            ancestry_ids TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
    ),
]

_v2e8c4020: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_obs_observation_id ON observations(observation_id)",
    "CREATE INDEX IF NOT EXISTS idx_obs_taxon_id ON observations(taxon_id)",
    "CREATE INDEX IF NOT EXISTS idx_obs_scientific_name ON observations(scientific_name)",
    "CREATE INDEX IF NOT EXISTS idx_obs_common_name ON observations(common_name)",
    "CREATE INDEX IF NOT EXISTS idx_obs_observed_on ON observations(observed_on)",
    "CREATE INDEX IF NOT EXISTS idx_obs_observer_id ON observations(observer_id)",
    "CREATE INDEX IF NOT EXISTS idx_obs_fingerprint ON observations(fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_obs_created_at ON observations(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_photo_photo_id ON photos(photo_id)",
    "CREATE INDEX IF NOT EXISTS idx_photo_observation_id ON photos(observation_id)",
    "CREATE INDEX IF NOT EXISTS idx_photo_original_url ON photos(original_url)",
    "CREATE INDEX IF NOT EXISTS idx_photo_fingerprint ON photos(fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_batch_obs_batch_id ON batch_observations(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_batch_obs_observation_id ON batch_observations(observation_id)",
    "CREATE INDEX IF NOT EXISTS idx_batch_taxa_batch_id ON batch_taxa(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_batch_taxa_taxon_id ON batch_taxa(taxon_id)",
    "CREATE INDEX IF NOT EXISTS idx_tree_parent ON taxon_tree(parent_id)",
]


def _v510cb11f(db_path: Path) -> None:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return
    backup_path = db_path.with_suffix(f".db.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    try:
        shutil.copy2(db_path, backup_path)
        db_path.unlink(missing_ok=True)
    except OSError:
        pass


def _vf4cc69e1(db_path: Path, schema: list[tuple[str, str]], indexes: list[str]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 先尝试连接，若损坏则备份重建
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
    except sqlite3.DatabaseError:
        _v510cb11f(db_path)

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        # 开启外键约束
        cursor.execute("PRAGMA foreign_keys = ON")
        # 建表
        for _, sql in schema:
            cursor.execute(sql)
        # 建索引
        for sql in indexes:
            cursor.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _vf04ed2c7(raw_db_path: Path | str) -> Path:
    path = Path(raw_db_path)
    _vf4cc69e1(path, _v0ea5fdb5, _ve63d5931)
    return path


def _v4f692264(main_db_path: Path | str) -> Path:
    path = Path(main_db_path)
    _vf4cc69e1(path, _v132f772a, _v2e8c4020)
    return path


def _ve49f7201(
    raw_db_path: Path | str,
    main_db_path: Path | str,
) -> dict[str, Path]:
    return {
        "raw_db": _vf04ed2c7(raw_db_path),
        "main_db": _v4f692264(main_db_path),
    }


def _v6f5fe057() -> int:
    import sys

    base_dir = Path(__file__).resolve().parent
    default_db_dir = base_dir / "data" / "db"
    raw_db = sys.argv[1] if len(sys.argv) > 1 else default_db_dir / "raw_data.db"
    main_db = sys.argv[2] if len(sys.argv) > 2 else default_db_dir / "bird_observations.db"

    paths = _ve49f7201(raw_db, main_db)
    print(f"原始数据库初始化完成: {paths['raw_db']}")
    print(f"主数据库初始化完成: {paths['main_db']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_v6f5fe057())
