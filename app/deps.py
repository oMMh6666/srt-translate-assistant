"""应用级共享资源：路径常量、单例（KeyPool / RunnerRegistry）、崩溃落盘日志。

路由从这里取依赖，不自己 new 对象、不自己算路径。
"""

from __future__ import annotations

import traceback
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from core.keys import KeyPool
from core.runner import RunnerRegistry

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "log"
WEB_DIR = ROOT / "web"
INPUT_DIR = ROOT / "input"
KEYS_DB = ROOT / "api_keys" / "api_keys.db"
SERVICE_LOG = ROOT / "service.log"


def record_error(prefix: str, exc: BaseException | None = None) -> str:
    """把异常写进 service.log。

    进程万一被系统收掉，界面日志会跟着一起消失；落盘才查得到到底发生了什么。
    """
    stamp = datetime.now().isoformat(timespec="seconds")
    detail = (
        traceback.format_exc()
        if exc is None
        else "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    )
    text = f"[{stamp}] {prefix}\n{detail}\n"
    try:
        with SERVICE_LOG.open("a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    return text


@lru_cache(maxsize=1)
def get_pool() -> KeyPool:
    return KeyPool(KEYS_DB)


@lru_cache(maxsize=1)
def get_registry() -> RunnerRegistry:
    return RunnerRegistry(
        on_crash=lambda job_id, exc: record_error(f"job worker crashed: {job_id}", exc)
    )
