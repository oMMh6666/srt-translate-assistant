"""任务运行：线程 + 事件广播 + 全局单任务槽。

把「起线程 / 广播 / 暂停 / 槽位互斥」从 FastAPI 路由里彻底搬出来：
路由只管调 `registry.start(...)`，不再自己捏 thread、queue、emit 闭包。

同一时间只允许一个任务执行 —— 批次必须串行消费上一批译文，多任务并行没有意义。
"""

from __future__ import annotations

import threading
import traceback
from typing import Callable

from core.events import END_CRASHED, END_FINISHED, EventBroker, Event, Subscription
from core.translator import Translator


class JobBusyError(Exception):
    """已经有别的任务在跑。"""


class JobRunner:
    """一个运行中的任务实例。"""

    def __init__(
        self,
        job_id: str,
        store,
        pool,
        cfg: dict,
        on_crash: Callable[[str, BaseException], None] | None = None,
    ):
        self.job_id = job_id
        self.store = store
        self.pool = pool
        self.cfg = cfg
        self.broker = EventBroker()
        self._on_crash = on_crash
        self._translator: Translator | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self, resume: bool = True) -> None:
        self._translator = Translator(
            self.store,
            self.pool,
            self.cfg,
            emit=self.broker.publish,
        )
        self._thread = threading.Thread(
            target=self._worker, args=(resume,), daemon=True
        )
        self._thread.start()

    def _worker(self, resume: bool) -> None:
        try:
            self._translator.run(resume=resume)  # type: ignore[union-attr]
        except BaseException as e:  # noqa: BLE001 - 必须兜住，否则线程静默死亡
            self._crash(e)
        finally:
            self.broker.close(END_FINISHED)

    def _crash(self, exc: BaseException) -> None:
        if self._on_crash:
            self._on_crash(self.job_id, exc)
        self.broker.publish(
            "log",
            level="error",
            message=f"任务线程异常终止：{type(exc).__name__}：{exc}（详情见 service.log）",
        )
        self.broker.close(END_CRASHED)

    def stop(self) -> None:
        if self._translator:
            self._translator.stop()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ 事件
    def subscribe(self) -> Subscription:
        return self.broker.subscribe()

    def unsubscribe(self, sub: Subscription) -> None:
        self.broker.unsubscribe(sub)

    def events_since(self, seq: int) -> list[Event]:
        return self.broker.since(seq)

    @property
    def oldest_seq(self) -> int:
        return self.broker.oldest_seq


class RunnerRegistry:
    """全局单任务槽。"""

    def __init__(self, on_crash: Callable[[str, BaseException], None] | None = None):
        self._on_crash = on_crash
        self._lock = threading.Lock()
        self._current: JobRunner | None = None

    def start(self, job_id: str, store, pool, cfg: dict) -> JobRunner:
        """占用槽位并起任务。已经有任务在跑（或刚跑完还没清理）就抛 JobBusyError。"""
        with self._lock:
            if self._current is not None and self._current.alive:
                raise JobBusyError(f"已有任务在运行：{self._current.job_id}")
            runner = JobRunner(job_id, store, pool, cfg, on_crash=self._on_crash)
            self._current = runner
        runner.start()
        return runner

    def get(self, job_id: str) -> JobRunner | None:
        with self._lock:
            cur = self._current
        if cur is None or cur.job_id != job_id:
            return None
        if not cur.alive:
            self.release(job_id)
            return None
        return cur

    def current_id(self) -> str | None:
        """当前正在跑的任务 id（顺手清理已结束的槽位）。"""
        with self._lock:
            cur = self._current
            if cur is None:
                return None
            if not cur.alive:
                self._current = None
                return None
            return cur.job_id

    def release(self, job_id: str) -> None:
        with self._lock:
            if self._current is not None and self._current.job_id == job_id:
                self._current = None


def format_crash(prefix: str, exc: BaseException) -> str:
    """线程崩溃的落盘文本（交给 app 层写 service.log）。"""
    return f"{prefix}\n" + "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
