"""单次请求的超时看门狗。

Gemini SDK 偶尔会卡住不返回（也不报错）。没有这一层的话界面会「静默」到像是死掉了 ——
这里做两件事：
1. 等待期间定期回调汇报已等待时长，明确告诉用户还在等模型；
2. 超过 timeout 抛 RequestTimeout，交给上层按「服务端抖动」换 Key 重试。

注意：Python 杀不掉一个正在阻塞的线程，超时后那个线程仍在后台跑完（daemon，随进程退出）。
这里做的是「不再等它」，不是「杀掉它」。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, TypeVar

REPORT_EVERY = 30.0  # 等待期间每 30s 汇报一次

T = TypeVar("T")


class RequestTimeout(Exception):
    """单次请求超过 REQUEST_TIMEOUT 仍未返回 —— 视为抖动，换 Key 重试。"""


def run_with_timeout(
    call: Callable[[], T],
    timeout: float,
    on_wait: Callable[[float], None] | None = None,
    report_every: float = REPORT_EVERY,
) -> T:
    """在后台线程里跑 call()，主线程等结果并盯超时。"""
    box: dict = {}

    def runner() -> None:
        try:
            box["value"] = call()
        except BaseException as e:  # noqa: BLE001 - 引擎异常要原样送回主线程
            box["error"] = e

    th = threading.Thread(target=runner, daemon=True)
    th.start()

    started = time.time()
    # join 的粒度决定超时的检出灵敏度：太粗会让「已经超时」迟迟不被发现
    step = min(1.0, max(timeout / 10, 0.05))
    next_report = report_every

    while th.is_alive():
        th.join(step)
        if not th.is_alive():
            break
        waited = time.time() - started
        if on_wait and waited >= next_report:
            on_wait(waited)
            next_report += report_every
        if waited >= timeout:
            raise RequestTimeout(f"请求超过 {int(timeout)} 秒未返回")

    if "error" in box:
        raise box["error"]
    return box.get("value")  # type: ignore[return-value]
