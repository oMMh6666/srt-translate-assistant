"""冒烟测试公共底座 —— 所有测试脚本共用。

三条铁律（都是踩过坑才定下来的）：

1. **一律用临时目录**：Key 库 / log 目录 / input 目录全部指向 tempdir。
   绝不碰 `api_keys/` 里的真实 Key，也绝不碰 `log/` 里的真实任务。
   （早期版本直接往项目 log/ 里建任务，残留会污染后续断言，查了很久。）
2. **一律用假引擎**：替换 `core.engine.generate_json`，一个字节都不发给真实 API。
3. **每个脚本自给自足**：自带起停服务，跑完自己收干净。
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台可能是 cp936，中文输出会炸
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ------------------------------------------------------------------ 结果汇总
class Report:
    def __init__(self, title: str):
        self.title = title
        self.passed = 0
        self.failed = 0
        self.fails: list[str] = []

    def check(self, name: str, ok, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  ok   {name}")
        else:
            self.failed += 1
            text = str(detail)[:200]
            print(f"  FAIL {name}" + (f"  <- {text}" if text else ""))
            self.fails.append(f"{name} <- {text}")
        return bool(ok)

    def finish(self) -> int:
        total = self.passed + self.failed
        print(f"\n{self.title}: {self.passed}/{total} 通过")
        if self.failed:
            print("失败项：")
            for f in self.fails:
                print("  - " + f)
            return 1
        print("全绿。")
        return 0


# ------------------------------------------------------------------ 环境隔离
def isolate(tmp: Path) -> None:
    """把 app 层的共享路径全部指到临时目录，并清掉单例缓存。"""
    from app import deps

    deps.KEYS_DB = tmp / "api_keys.db"
    deps.LOG_DIR = tmp / "log"
    deps.INPUT_DIR = tmp / "input"
    deps.SERVICE_LOG = tmp / "service.log"
    for p in (deps.LOG_DIR, deps.INPUT_DIR):
        p.mkdir(parents=True, exist_ok=True)
    deps.get_pool.cache_clear()
    deps.get_registry.cache_clear()


def make_srt(path: Path, n: int = 5, prefix: str = "Line") -> Path:
    """造一份 n 条字幕的 srt。"""
    blocks = [
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\n{prefix} {i}."
        for i in range(1, n + 1)
    ]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ 假引擎
_TASK_RE = re.compile(r"<current_task>.*?JSON 结果：\n(.*?)\s*</current_task>", re.S)


class FakeEngine:
    """替换 `core.engine.generate_json`。

    从最后一个 `<current_task>` 段里读出本批 id，原样造一份译文返回，
    不联网、不花钱、结果确定 —— 断言才写得稳。

    可调的行为（都是为了让特定场景可复现）：
    - `delay`：每批先睡一会，用来观察进度条是不是一批一跳
    - `gate`：`threading.Event`，没 set 就一直卡在请求里（掐断 / 重连用）
    - `missing_first`：第一次调用故意漏一条，触发漏句纠错分支
    - `raise_exc`：每次调用都抛这个异常（走错误分类 / 换 Key 分支）
    """

    def __init__(
        self,
        delay: float = 0.0,
        missing_first: bool = False,
        gate=None,
        raise_exc: BaseException | None = None,
        prefix: str = "译",
    ):
        self.delay = delay
        self.missing_first = missing_first
        self.gate = gate
        self.raise_exc = raise_exc
        self.prefix = prefix
        self.calls: list[int] = []  # 每次调用的本批条数
        self._orig = None
        self._n = 0

    def install(self) -> "FakeEngine":
        from core import engine as E

        self._orig = E.generate_json
        E.generate_json = self
        return self

    def uninstall(self) -> None:
        from core import engine as E

        if self._orig is not None:
            E.generate_json = self._orig
            self._orig = None

    def __call__(self, *, api_key, model, system_prompt, turns, schema=None, **kw):
        from core import engine as E

        batch = self._current_task(turns)
        self.calls.append(len(batch))
        self._n += 1

        if self.gate is not None:
            self.gate.wait(120)
        if self.delay:
            time.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc

        items = []
        for i, sid in enumerate(batch):
            if self.missing_first and self._n == 1 and i == 0:
                continue  # 第一次故意漏一条
            items.append(
                {
                    "id": sid,
                    "direct_translation": f"直译 {sid}",
                    "reflection": "ok",
                    "native_translation": f"{self.prefix}{sid}",
                }
            )
        return E.Generation(
            text=json.dumps(items, ensure_ascii=False),
            raw={"fake": True, "count": len(items)},
        )

    @staticmethod
    def _current_task(turns: list[dict]) -> dict:
        """从 turns 里找回本批 `{id: text}`。

        漏句纠错会往 turns 后面追加轮次，所以要**从后往前**找最后一个 current_task。
        """
        for t in reversed(turns):
            m = _TASK_RE.search(t.get("text", "") or "")
            if m:
                try:
                    return json.loads(m.group(1))
                except Exception:
                    return {}
        return {}


# ------------------------------------------------------------------ 拉起服务
def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def spawn_server(port: int, tmp: Path, delay: float = 0.0, wait: float = 30.0):
    """起一个「假引擎 + 真服务」的子进程（tests/fake_server.py）。

    用子进程而不是线程：uvicorn 的 `capture_signals` 在非主线程里装不了信号处理器。
    """
    import os
    import subprocess

    script = Path(__file__).with_name("fake_server.py")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(script),
            "--port",
            str(port),
            "--tmp",
            str(tmp),
            "--delay",
            str(delay),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"服务进程提前退出：\n{out[-2000:]}")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/config", timeout=2
            ) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.3)

    out = proc.stdout.read() if proc.stdout else ""
    proc.kill()
    raise RuntimeError(f"服务没起来（port {port}）：\n{out[-2000:]}")


def stop_server(proc) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()


# ------------------------------------------------------------ 读 SSE 流
class StreamReader(threading.Thread):
    """在后台线程里读一个同步生成器，测试侧想看几帧就看几帧。

    为什么绕这么一下：Starlette 会把同步生成器包成 async generator
    （`iterate_in_threadpool`），而 `asyncio.wait_for` 超时会 cancel 掉
    `agen.__anext__()` —— 异步生成器被顺手关掉，后面一帧都取不到。
    直接读原始生成器 + 后台线程，时序最好控制，也不会被超时误伤。
    """

    def __init__(self, gen):
        super().__init__(daemon=True)
        self.gen = gen
        self.frames: list[str] = []
        self._stop = False

    def run(self) -> None:
        try:
            for frame in self.gen:
                self.frames.append(frame)
                if self._stop:
                    break
        except Exception:  # noqa: BLE001 - 流断了就算了
            pass
        try:
            self.gen.close()
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        """模拟浏览器断开。

        线程可能正卡在 `queue.get`，最多等一个心跳周期（15s）就会自己退出并
        `unsubscribe`，不用测试侧干等。
        """
        self._stop = True


_STREAM_CAPTURE: list = []
_orig_streaming_init = None


def install_stream_capture() -> None:
    """抓住路由返回的**原始**同步生成器（StreamingResponse 只留包装后的）。"""
    global _orig_streaming_init
    if _orig_streaming_init is not None:
        return
    import inspect

    from starlette.responses import StreamingResponse

    _orig_streaming_init = StreamingResponse.__init__

    def spy(self, content, *a, **kw):
        if inspect.isgenerator(content):
            _STREAM_CAPTURE.append(content)
        _orig_streaming_init(self, content, *a, **kw)

    StreamingResponse.__init__ = spy


def last_stream():
    return _STREAM_CAPTURE[-1] if _STREAM_CAPTURE else None
