#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================
"""SQLite 连接工厂：统一的 PRAGMA、只读 URI 与连接生命周期。

解决的问题（对应评审 P0-3）：
    1. `with sqlite3.connect(...) as conn` 只提交/回滚事务，**不会关闭连接**；
       长期运行的 GUI 每查一次库就漏一个句柄。本模块用 contextmanager 保证 close。
    2. 只读连接原先用 `file:{path}?mode=ro` 手工拼接，路径含空格（例如安装到
       C:/Program Files/鸟类）时 URI 不合法；改用 Path.as_uri() 做百分号编码。
    3. 全库未开 WAL / busy_timeout，GUI 只读查询与爬虫子进程写入并发时会抛
       `database is locked`；写入连接统一开启 WAL + busy_timeout。

用法：
    from db import connect

    with connect(DB_PATH, readonly=True) as conn:
        rows = conn.execute("SELECT ...").fetchall()

    with connect(DB_PATH) as conn:          # 自动 commit / 异常回滚
        conn.execute("INSERT ...")
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 拿不到锁时的等待上限（毫秒）：GUI 读与子进程写并存时避免立刻报 locked
BUSY_TIMEOUT_MS = 5000


def readonly_uri(path: Path | str) -> str:
    """构造 SQLite 只读 URI。

    用 Path.as_uri() 而非字符串拼接：空格、中文、#?% 等字符会被正确
    百分号编码，避免「安装到带空格目录后打不开库」。
    """
    return Path(path).resolve().as_uri() + "?mode=ro"


def apply_read_pragmas(conn: sqlite3.Connection) -> None:
    """只读连接 PRAGMA：禁止写入 + 等待锁超时。"""
    conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


def apply_write_pragmas(conn: sqlite3.Connection) -> None:
    """写入连接 PRAGMA：WAL + NORMAL 同步 + 外键 + 等待锁超时。

    journal_mode 单独兜底：若已有其他连接持有读事务，切换 WAL 可能临时
    拿不到锁；此时保持原模式继续，不阻断正常写入。
    """
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


@contextmanager
def connect(path: Path | str, *, readonly: bool = False,
            timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
    """打开数据库连接并在退出时关闭（只读连接不会写入）。

    参数：
        path      数据库文件路径
        readonly  True=只读（mode=ro + query_only），False=读写（WAL）
        timeout   sqlite3.connect 的连接超时（秒），实际等锁上限见 busy_timeout
    """
    target = Path(path)
    if readonly:
        conn = sqlite3.connect(readonly_uri(target), uri=True, timeout=timeout)
        apply_read_pragmas(conn)
    else:
        conn = sqlite3.connect(str(target), timeout=timeout)
        apply_write_pragmas(conn)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()
