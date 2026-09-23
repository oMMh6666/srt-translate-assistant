"""任务事件：SSE 的数据源。

设计要点
- 每个事件有单调递增的 `seq`（作为 SSE 的 `id:`），浏览器断线重连时会把最后收到的
  id 放进 `Last-Event-ID`，服务端据此补发缺失的那段 —— 前端就不必整页重拉了。
- 订阅式广播：每个 SSE 连接领一条自己的队列，互不抢事件（以前「页面重连后新连接收不到
  事件、必须点一下任务才更新」就是这个坑）。
- `close()` 会把结束信号广播给**所有**订阅，而不是只塞给某一条队列。
"""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Union

EVENT_LOG = "log"
EVENT_BATCH = "batch"
EVENT_STATUS = "status"
EVENT_EXPORT = "export"

# 环形缓冲大小：够覆盖一次短断线（服务重启那类长时间中断本来就该整页重拉）
HISTORY_SIZE = 200

# SSE 层多久没事件就发一次心跳注释帧（保活，代理不至于掐掉连接）
HEARTBEAT_SECONDS = 15.0

# 结束原因
END_FINISHED = "finished"
END_STOPPED = "stopped"
END_CRASHED = "crashed"
END_CLOSED = "closed"


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    ts: str
    data: dict


@dataclass(frozen=True)
class End:
    """流结束信号（不是业务事件，不进历史缓冲）。"""

    reason: str


QueueItem = Union[Event, End, None]  # None = 该发心跳了


class Subscription:
    """一条 SSE 连接自己的队列。"""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()

    def put(self, item: Event | End) -> None:
        self._q.put(item)

    def get(self, timeout: float = HEARTBEAT_SECONDS) -> QueueItem:
        """取一条事件；超时返回 None（调用方发心跳），收到 End 就原样返回。"""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


class EventBroker:
    def __init__(self, history_size: int = HISTORY_SIZE) -> None:
        self._lock = threading.Lock()
        self._subs: set[Subscription] = set()
        self._history: deque[Event] = deque(maxlen=history_size)
        self._seq = 0
        self._closed = False

    # ------------------------------------------------------------------ 发布
    def publish(self, type: str, **data) -> Event | None:
        """广播一条事件给所有订阅，并记进环形缓冲（供断线补发）。"""
        with self._lock:
            if self._closed:
                return None
            self._seq += 1
            event = Event(
                seq=self._seq,
                type=type,
                ts=datetime.now().isoformat(timespec="seconds"),
                data=data,
            )
            self._history.append(event)
            subs = list(self._subs)

        for sub in subs:
            sub.put(event)
        return event

    def close(self, reason: str = END_FINISHED) -> None:
        """结束事件流：广播 End 给所有订阅，之后 publish 不再生效。"""
        with self._lock:
            self._closed = True
            subs = list(self._subs)
        for sub in subs:
            sub.put(End(reason))

    # ------------------------------------------------------------------ 订阅
    def subscribe(self) -> Subscription:
        with self._lock:
            sub = Subscription()
            self._subs.add(sub)
            return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)

    # ------------------------------------------------------------------ 补发
    @property
    def oldest_seq(self) -> int:
        with self._lock:
            return self._history[0].seq if self._history else 0

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def since(self, seq: int) -> list[Event]:
        """seq 之后的事件（断线补发用）。seq<=0 表示「从头给」。"""
        with self._lock:
            if seq <= 0:
                return list(self._history)
            return [e for e in self._history if e.seq > seq]


def sse_frame(event: Event) -> str:
    """一个业务事件的 SSE 帧（id + 命名事件 + data）。"""
    payload = {
        "seq": event.seq,
        "type": event.type,
        "ts": event.ts,
        "data": event.data,
    }
    return (
        f"id: {event.seq}\n"
        f"event: {event.type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )
