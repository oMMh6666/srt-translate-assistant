"""API Key 池：最少使用优先 + 冷却调度。

在既有 api_keys.db 上做兼容升级（ALTER TABLE 加列），不破坏旧数据。
冷却语义：
- 429 日配额耗尽 -> 冷却到次日零点
- 429 分钟级限流 -> 冷却 retryDelay 秒
- 503 瞬时抖动   -> 不冷却（History.md：503 是服务端问题，不是 Key 的问题）

「禁用」与「冷却」是两回事：
- 冷却到点自动恢复，调度时跳过
- 禁用（429 带 quotaValue，额度真的用完）必须用户在 Key 管理里手动启用
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core import db
from core.errors import next_local_midnight

DEFAULT_DB = Path(__file__).resolve().parents[1] / "api_keys" / "api_keys.db"


def _iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).isoformat(timespec="seconds")


def mask_key(key: str) -> str:
    return f"{key[:8]}...{key[-4:]}" if key and len(key) > 12 else (key or "N/A")


@dataclass(frozen=True)
class KeyRecord:
    """一次取用拿到的 Key（只带调度需要的最小信息）。"""

    id: int
    api_key: str
    project_name: str


class KeyPool:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or DEFAULT_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with db.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key TEXT UNIQUE NOT NULL,
                    project_name TEXT UNIQUE NOT NULL,
                    usage_count INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_active_usage
                ON api_keys(is_active, usage_count)
            """)
            conn.commit()

            cols = {r[1] for r in conn.execute("PRAGMA table_info(api_keys)")}
            if "cooldown_until" not in cols:
                conn.execute("ALTER TABLE api_keys ADD COLUMN cooldown_until TEXT")
            if "fail_count" not in cols:
                conn.execute(
                    "ALTER TABLE api_keys ADD COLUMN fail_count INTEGER DEFAULT 0"
                )
            conn.commit()

    # ------------------------------------------------------------- 增删改
    def add_keys(self, pairs: list[tuple[str, str]]) -> None:
        valid = [
            (k.strip(), p.strip())
            for k, p in pairs
            if k and k.strip() and p and p.strip()
        ]
        if not valid:
            return
        with db.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO api_keys (api_key, project_name) VALUES (?, ?)",
                valid,
            )
            conn.commit()

    def update_key(self, key_id: int, api_key: str, project_name: str) -> bool:
        with db.connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE api_keys SET api_key=?, project_name=? WHERE id=?",
                (api_key.strip(), project_name.strip(), key_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_key(self, key_id: int) -> bool:
        with db.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
            conn.commit()
            return cur.rowcount > 0

    def disable(self, key_id: int) -> None:
        """停用一个 Key（手动禁用，或 429 带 quotaValue 判定额度耗尽）。

        同时清掉冷却时间：额度耗尽不是「等一会儿就好」，留着冷却只会让人误以为会自动恢复。
        """
        with db.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE api_keys SET is_active = 0, cooldown_until = NULL WHERE id = ?",
                (key_id,),
            )
            conn.commit()

    def enable(self, key_id: int) -> None:
        with db.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE api_keys SET is_active=1, fail_count=0, cooldown_until=NULL "
                "WHERE id=?",
                (key_id,),
            )
            conn.commit()

    def reset_usage(self, ids: list[int] | None = None) -> None:
        with db.connect(self.db_path) as conn:
            if ids:
                conn.executemany(
                    "UPDATE api_keys SET usage_count = 0 WHERE id = ?",
                    [(int(i),) for i in ids],
                )
            else:
                conn.execute("UPDATE api_keys SET usage_count = 0")
            conn.commit()

    # ------------------------------------------------------------- 取用
    def acquire(self, now: datetime | None = None) -> KeyRecord | None:
        """取一个当前可用的 Key（未禁用且不在冷却中），并把使用计数 +1。"""
        now_iso = _iso(now)
        with db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, api_key, project_name FROM api_keys
                WHERE is_active = 1
                  AND (cooldown_until IS NULL OR cooldown_until < ?)
                ORDER BY usage_count ASC, RANDOM() LIMIT 1
                """,
                (now_iso,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE api_keys SET usage_count = usage_count + 1, last_used_at = ? "
                "WHERE id = ?",
                (_iso(), row[0]),
            )
            conn.commit()
        return KeyRecord(id=row[0], api_key=row[1], project_name=row[2])

    def available_count(self, now: datetime | None = None) -> int:
        with db.connect(self.db_path) as conn:
            return conn.execute(
                """
                SELECT COUNT(*) FROM api_keys
                WHERE is_active = 1
                  AND (cooldown_until IS NULL OR cooldown_until < ?)
                """,
                (_iso(now),),
            ).fetchone()[0]

    # --------------------------------------------------------- 结果反馈
    def mark_ok(self, key_id: int) -> None:
        with db.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE api_keys SET fail_count = 0, cooldown_until = NULL WHERE id = ?",
                (key_id,),
            )
            conn.commit()

    def mark_transient(self, key_id: int) -> None:
        """503 等服务侧抖动：不冷却、不记失败（不是这个 Key 的锅）。"""
        return None

    def mark_rate_limit(
        self, key_id: int, seconds: float, now: datetime | None = None
    ) -> None:
        until = (now or datetime.now()).timestamp() + max(seconds, 1.0)
        with db.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE api_keys SET cooldown_until = ?, fail_count = fail_count + 1 "
                "WHERE id = ?",
                (_iso(datetime.fromtimestamp(until)), key_id),
            )
            conn.commit()

    def mark_quota_exhausted(self, key_id: int, now: datetime | None = None) -> None:
        until = next_local_midnight(now)
        with db.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE api_keys SET cooldown_until = ?, fail_count = fail_count + 1 "
                "WHERE id = ?",
                (_iso(until), key_id),
            )
            conn.commit()

    # ------------------------------------------------------------- 查询
    def list_keys(self) -> list[dict]:
        with db.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, api_key, project_name, usage_count, is_active,
                       last_used_at, cooldown_until, fail_count
                FROM api_keys ORDER BY id
                """
            ).fetchall()

        out = []
        for r in rows:
            out.append(
                {
                    "id": r[0],
                    "api_key": r[1],
                    "api_key_masked": mask_key(r[1]),
                    "project_name": r[2],
                    "usage_count": r[3],
                    "is_active": bool(r[4]),
                    "last_used_at": r[5],
                    "cooldown_until": r[6],
                    "fail_count": r[7] or 0,
                }
            )
        return out

    def stats(self) -> dict:
        keys = self.list_keys()
        return {
            "total": len(keys),
            "active": sum(1 for k in keys if k["is_active"]),
            "cooling": sum(1 for k in keys if k["cooldown_until"]),
            "available": self.available_count(),
        }
