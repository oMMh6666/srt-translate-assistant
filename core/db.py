"""sqlite 连接工具：全局串行 + 用完即关。

两个踩过的坑，都记在这里免得再犯：
1. `with sqlite3.connect(...)` 只提交/回滚事务，**连接本身并不关闭**，
   句柄会一直泄漏，Windows 上还会把 db 文件锁死（删不掉、移不动）。
2. 但「每个操作都新开连接」在多线程下也有问题：任务线程在写、HTTP 请求在读，
   同一 db 被反复开关，Windows 上 `-shm` 竞争会让 SQLite 把库降级成只读
   （`attempt to write a readonly database`），写入直接失败。

所以这里的做法是：**连接用完即关，但全局串行** —— 同一时刻只有一个线程碰 sqlite；
再配 busy_timeout 兜底。本项目是单人本地工具，串行化的开销可以忽略。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_MS = 5000

# 全局一把锁：跨 JobStore / KeyPool 实例，保证同一时刻只有一个 sqlite 连接在干活
_SQLITE_LOCK = threading.RLock()


@contextmanager
def connect(db_path: str | Path):
    """打开一次性的 sqlite 连接，用完即关。"""
    with _SQLITE_LOCK:
        conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_schema(db_path: str | Path, schema_sql: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(schema_sql)
