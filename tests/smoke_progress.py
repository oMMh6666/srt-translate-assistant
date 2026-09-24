"""进度条专项：跑完一轮，看顶部进度条是不是跟着批次数一跳一跳走。

为什么要单开一个脚本：进度条有两条更新路径 —— SSE 事件流、以及 SSE 不在时的轮询兜底。
只读代码两条都「看起来对」，真跑一遍才发现兜底那条一次都没执行过（条件互斥）。
这个脚本专门盯这一点：

- A：SSE 正常 —— 一批一跳
- B：掐断 SSE、停在「字幕对照」—— 靠兜底也要走到 100%
- C：掐断 SSE、停在「翻译记录」—— 同上

引擎假的（每批故意慢 2 秒），服务 / SSE / 前端 / 浏览器全是真的。

    .venv\\Scripts\\python.exe tests/smoke_progress.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

from tests._harness import (  # noqa: E402
    Report,
    free_port,
    make_srt,
    spawn_server,
    stop_server,
)

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
N_LINES = 5  # 每批 1 条 -> 5 批 -> 20% 一跳


def pct_of(width: str) -> float:
    try:
        return float(str(width).replace("%", "") or 0)
    except ValueError:
        return 0.0


def create_job(page, srt_file: Path, expected_count: int) -> None:
    """新建任务：5 条字幕、每批 1 条，并确认选中的确实是它。

    每个场景用不同的字幕文件名 —— 否则「列表里有一个同名且已跑完的旧任务」时，
    断言会打在旧任务上，进度条一上来就是 100%，看着像通过其实什么都没测到。
    """
    page.click("#btn-new")
    page.wait_for_timeout(300)
    page.set_input_files("#new-srt-file", str(srt_file))
    page.fill("#new-batch", "1")
    page.wait_for_timeout(200)
    page.click("#btn-create")
    # 创建是一条异步链（upload → create → refreshJobs → selectJob），
    # 等到「列表里有它 **且** 它被选中」再动手，否则断言会打在旧任务上
    stem = json.dumps(srt_file.stem)
    page.wait_for_function(
        f"() => document.querySelectorAll('#joblist li').length === {expected_count} "
        f"&& !document.getElementById('job-detail').hidden "
        f"&& (document.querySelector('#joblist li.active') || {{}}).textContent "
        f"&& document.querySelector('#joblist li.active').textContent.includes({stem})",
        timeout=25000,
    )
    # 上面那条只保证「列表里选中了它」—— 详情是异步拉的，
    # 这会儿 state.detail 可能还是上一个任务的，进度条显示的还是旧值。
    # 等详情落定（新任务进度必然是 0%）再动手，否则采样第一下就是 100%。
    try:
        page.wait_for_function(
            "() => document.getElementById('bar').style.width === '0%'", timeout=10000
        )
    except Exception:  # noqa: BLE001 - 等不到就让下面的断言把它报出来
        pass
    page.wait_for_timeout(400)


def run_and_sample(page, budget: float = 60.0) -> list[float]:
    """点开始，然后一路采样进度条宽度，直到 100% 或超时。"""
    page.click("#btn-start")
    seen: list[float] = []
    deadline = time.time() + budget
    while time.time() < deadline:
        w = page.eval_on_selector("#bar", "el => el.style.width") or "0%"
        p = pct_of(w)
        if not seen or seen[-1] != p:
            seen.append(p)
        if p >= 100:
            break
        page.wait_for_timeout(200)
    return seen


def main() -> int:
    r = Report("进度条专项")
    tmp = Path(tempfile.mkdtemp(prefix="srt-prog-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = None

    try:
        srt_a = make_srt(tmp / "prog_a.srt", n=N_LINES)
        srt_b = make_srt(tmp / "prog_b.srt", n=N_LINES)
        srt_c = make_srt(tmp / "prog_c.srt", n=N_LINES)
        # 每批故意慢 2s，让「一跳一跳」看得见，兜底轮询也来得及采到中间态
        proc = spawn_server(port, tmp, delay=2.0)

        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=CHROME if Path(CHROME).exists() else None,
                headless=True,
            )

            def make_page(abort_sse: bool):
                """每个场景开一个新页面，避免上一个场景的状态串进来。

                （不复用的话，进度条会留着上一轮的 100%，采样第一下就是满的，
                  看着像通过，其实什么都没测到。）
                """
                errs: list[str] = []
                pg = browser.new_page(viewport={"width": 1500, "height": 950})
                pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.on("dialog", lambda d: d.accept())
                if abort_sse:
                    # 只掐 SSE 端点。别用 "**/events*" —— 那会把前端模块
                    # js/events.js 一起拦掉，整个页面直接起不来
                    pg.route(
                        re.compile(r"/api/jobs/[^/]+/events(\?|$)"),
                        lambda route: route.abort(),
                    )
                pg.goto(base, wait_until="load")
                pg.wait_for_function(
                    "() => document.querySelectorAll('#new-model option').length > 0",
                    timeout=15000,
                )
                pg.wait_for_timeout(600)
                return pg, errs

            # ------------------------------------------------ A：SSE 正常
            print("\n[A] SSE 正常 —— 应该一批一跳")
            page, errors = make_page(abort_sse=False)
            create_job(page, srt_a, 1)
            r.check("新任务选中时进度条归零",
                    pct_of(page.eval_on_selector("#bar", "el => el.style.width")) == 0,
                    page.eval_on_selector("#bar", "el => el.style.width"))
            seq = run_and_sample(page)
            r.check("进度条走到 100%", bool(seq) and seq[-1] >= 100, str(seq))
            r.check("中途每一跳都能看见（>=4 个中间值）",
                    len([v for v in seq if 0 < v < 100]) >= 4, str(seq))
            r.check("进度单调不回退", all(b >= a for a, b in zip(seq, seq[1:])), str(seq))
            r.check("20% 一跳（5 批）", all(v % 20 == 0 for v in seq), str(seq))
            r.check("SSE 正常时无 console 报错", not errors, str(errors[:2]))

            # ------------------------------------------------ B：掐断 SSE，停在字幕对照
            print("\n[B] 掐断 SSE + 停在字幕对照 —— 靠兜底也要走完")
            page.close()
            page, errors = make_page(abort_sse=True)
            create_job(page, srt_b, 2)
            page.click(".subtabs button[data-tab='pairs']")
            page.wait_for_timeout(300)
            r.check("当前停在字幕对照页签",
                    page.is_visible("#tab-pairs") and not page.get_attribute("#tab-pairs", "hidden"))
            r.check("新任务选中时进度条归零",
                    pct_of(page.eval_on_selector("#bar", "el => el.style.width")) == 0,
                    page.eval_on_selector("#bar", "el => el.style.width"))
            seq_b = run_and_sample(page, budget=80)
            r.check("SSE 断了进度条照样走到 100%", bool(seq_b) and seq_b[-1] >= 100, str(seq_b))
            r.check("不是从 0 直接跳 100（有中间态）",
                    any(0 < v < 100 for v in seq_b), str(seq_b))

            # ------------------------------------------------ C：掐断 SSE，停在翻译记录
            print("\n[C] 掐断 SSE + 停在翻译记录 —— 同上")
            page.close()
            page, errors = make_page(abort_sse=True)
            create_job(page, srt_c, 3)
            r.check("新任务选中时进度条归零",
                    pct_of(page.eval_on_selector("#bar", "el => el.style.width")) == 0,
                    page.eval_on_selector("#bar", "el => el.style.width"))
            page.click(".subtabs button[data-tab='apilogs']")
            page.wait_for_timeout(300)
            seq_c = run_and_sample(page, budget=80)
            r.check("停在翻译记录时进度条也走完", bool(seq_c) and seq_c[-1] >= 100, str(seq_c))
            r.check("不是从 0 直接跳 100（有中间态）",
                    any(0 < v < 100 for v in seq_c), str(seq_c))
            n_logs = 0
            deadline = time.time() + 20
            while time.time() < deadline:
                n_logs = len(page.query_selector_all("#alogs-body tr"))
                if n_logs:
                    break
                page.wait_for_timeout(500)
            r.check("翻译记录页签仍会自动加载", n_logs >= 1, str(n_logs))

            # 掐断 SSE 会让浏览器打出资源加载失败，属于预期，不算失败
            real_errors = [
                e for e in errors
                if "Failed to load resource" not in e and "net::ERR" not in e
            ]
            r.check("除了 SSE 掐断本身，没有别的 console 报错", not real_errors, str(real_errors[:2]))

            browser.close()
    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)

    return r.finish()


if __name__ == "__main__":
    raise SystemExit(main())
