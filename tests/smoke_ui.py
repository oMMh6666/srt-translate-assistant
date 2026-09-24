"""界面冒烟：真起服务 + 无头 Chrome 真点一遍界面。

服务是真的（FastAPI + 真路由 + 真 SSE），浏览器是真的（本机 Chrome，无头），
只有**引擎是假的** —— 由 tests/fake_server.py 提供，不打真实 API，
Key 库和 log 目录全在临时目录里，一个字节都不碰你的真实数据。

这个是防「跑完了界面却不动」这类只有真跑一遍才看得出来的问题。

    .venv\\Scripts\\python.exe tests/smoke_ui.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests._harness import (  # noqa: E402
    Report,
    free_port,
    make_srt,
    spawn_server,
    stop_server,
)

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def main() -> int:
    r = Report("界面冒烟")
    tmp = Path(tempfile.mkdtemp(prefix="srt-ui-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = None
    errors: list[str] = []
    shot = Path(tempfile.mkdtemp(prefix="srt-shot-"))

    try:
        srt_file = make_srt(tmp / "ui_smoke.srt", n=5)
        proc = spawn_server(port, tmp)

        # ------------------------------------------------ 静态资源不缓存
        print("\n[1] 静态资源")
        for path in ("/", "/style.css", "/js/main.js"):
            resp = requests.get(base + path, timeout=10)
            cc = resp.headers.get("cache-control", "")
            r.check(f"{path} 带 no-cache", "no-cache" in cc, cc or "(无)")
        r.check("API 响应不受影响（没有 no-cache）",
                "no-cache" not in requests.get(base + "/api/config", timeout=10)
                .headers.get("cache-control", ""),
                requests.get(base + "/api/config", timeout=10).headers.get("cache-control", ""))

        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=CHROME if Path(CHROME).exists() else None,
                headless=True,
            )
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())

            page.goto(base, wait_until="load")
            # 模型下拉在隐藏的弹窗里，只能等「已填充」而不是「可见」
            page.wait_for_function(
                "() => document.querySelectorAll('#new-model option').length > 0",
                timeout=15000,
            )
            page.wait_for_timeout(800)

            # ------------------------------------------------ 首屏
            print("\n[2] 首屏")
            r.check("任务视图可见", page.is_visible("#view-tasks"))
            r.check("顶栏三个主视图都在",
                    page.query_selector_all("#toptabs button").__len__() == 3,
                    str(len(page.query_selector_all("#toptabs button"))))
            r.check("Key 概览显示 2 条可用 Key", "2" in page.inner_text("#keystat"),
                    page.inner_text("#keystat"))
            r.check("没有任务时显示空态", page.is_visible("#job-empty"))

            # ------------------------------------------------ 主视图切换
            print("\n[3] 主视图切换")
            page.click("#toptabs button[data-view='prompts']")
            page.wait_for_timeout(400)
            r.check("切到提示词管理", page.is_visible("#view-prompts"))
            r.check("全局模板列表非空",
                    len(page.query_selector_all("#tpllist li")) >= 1,
                    str(len(page.query_selector_all("#tpllist li"))))
            # custom_prompt.md 是个空文件，正文断言要挑 reflect.md
            names = page.eval_on_selector_all("#tpllist li .jname", "els => els.map(e => e.textContent)")
            idx = next((i for i, n in enumerate(names) if "reflect" in (n or "")), 0)
            page.click(f"#tpllist li:nth-child({idx + 1})")
            page.wait_for_timeout(600)
            r.check("选中模板后能载入正文",
                    len(page.input_value("#tpl-content")) > 0,
                    f"{names[idx] if names else '?'} -> {len(page.input_value('#tpl-content'))} 字")

            page.click("#toptabs button[data-view='keys']")
            page.wait_for_timeout(500)
            r.check("切到 API Key 管理", page.is_visible("#view-keys"))
            rows = page.query_selector_all("#keys-body tr")
            r.check("Key 表有 2 行", len(rows) == 2, str(len(rows)))

            # ------------------------------------------------ 禁用 Key 的删除线
            print("\n[4] 禁用 Key 的删除线")
            strike = lambda i: page.eval_on_selector(  # noqa: E731
                f"#keys-body tr:nth-child({i}) td.mono",
                "el => getComputedStyle(el).textDecorationLine",
            )

            def row_class(i: int) -> str:
                return page.eval_on_selector(
                    f"#keys-body tr:nth-child({i})", "el => el.className"
                )

            r.check("启用中的 Key 没有删除线", strike(1) == "none", strike(1))
            page.click("#keys-body tr:nth-child(1) input[data-ck]")
            page.wait_for_timeout(200)
            page.click("button[data-bulk='disable']")
            page.wait_for_timeout(800)
            r.check("禁用后该行加上 key-off", "key-off" in row_class(1), row_class(1))
            r.check("禁用后 API Key 有删除线", "line-through" in strike(1), strike(1))
            r.check("禁用后项目名也有删除线",
                    "line-through" in page.eval_on_selector(
                        "#keys-body tr:nth-child(1) td.pname",
                        "el => getComputedStyle(el).textDecorationLine"),
                    page.eval_on_selector("#keys-body tr:nth-child(1) td.pname",
                                          "el => getComputedStyle(el).textDecorationLine"))
            r.check("另一行不受影响", strike(2) == "none", strike(2))

            # 操作完列表会重画、勾选状态清空，重新勾一次再启用
            page.click("#keys-body tr:nth-child(1) input[data-ck]")
            page.wait_for_timeout(300)
            page.click("button[data-bulk='enable']")
            page.wait_for_timeout(900)
            r.check("重新启用后删除线消失", strike(1) == "none", strike(1))

            # ------------------------------------------------ 建任务
            print("\n[5] 新建任务")
            page.click("#toptabs button[data-view='tasks']")
            page.wait_for_timeout(300)
            page.click("#btn-new")
            page.wait_for_timeout(300)
            r.check("新建弹窗打开", page.is_visible("#modal-new"))
            page.set_input_files("#new-srt-file", str(srt_file))
            page.wait_for_timeout(300)
            r.check("选完文件有提示", "已选择" in page.inner_text("#new-srt-info"),
                    page.inner_text("#new-srt-info"))

            page.click("#btn-create")
            # 创建是一条异步链：upload → create → refreshJobs → selectJob，等它走完
            page.wait_for_function(
                "() => document.querySelectorAll('#joblist li').length === 1 "
                "&& !document.getElementById('job-detail').hidden",
                timeout=20000,
            )
            page.wait_for_timeout(600)
            active = page.inner_text("#joblist li.active") if page.query_selector(
                "#joblist li.active") else ""
            r.check("任务出现在列表且被选中", "ui_smoke" in active, active[:80])
            r.check("详情区已展开", page.is_visible("#job-detail"))
            r.check("字幕对照有 5 行",
                    len(page.query_selector_all("#pairs-body tr")) == 5,
                    str(len(page.query_selector_all("#pairs-body tr"))))
            r.check("进度文案已渲染", "批" in page.inner_text("#progtxt"),
                    page.inner_text("#progtxt"))
            r.check("开始按钮显示「开始翻译」类文案",
                    ("开始" in page.inner_text("#btn-start")),
                    page.inner_text("#btn-start"))
            r.check("没跑完时下载按钮置灰", page.is_disabled("#btn-dl-srt"))

            # ------------------------------------------------ 子页签
            print("\n[6] 子页签")
            for tab, marker in [("params", "#p-batch"), ("prompts", "#t-reflect"),
                                ("apilogs", "#tab-apilogs"), ("pairs", "#tab-pairs")]:
                page.click(f".subtabs button[data-tab='{tab}']")
                page.wait_for_timeout(300)
                sel = f".subtabs button[data-tab='{tab}']"
                r.check(f"切到 {tab} 页签", page.is_visible(marker)
                        and "active" in (page.get_attribute(sel, "class") or ""),
                        page.get_attribute(sel, "class") or "")

            # ------------------------------------------------ 跑一轮
            print("\n[7] 跑一轮，看进度条动不动")
            page.click("#btn-start")
            deadline = time.time() + 60
            percent = "0%"
            while time.time() < deadline:
                percent = page.eval_on_selector("#bar", "el => el.style.width") or "0%"
                if percent == "100%":
                    break
                page.wait_for_timeout(300)
            r.check("进度条走到 100%", percent == "100%", percent)
            # 终态后还要异步重拉详情才拿得到 outputs_ready，等它解锁而不是固定 sleep
            unlocked = False
            deadline = time.time() + 20
            while time.time() < deadline:
                if not page.is_disabled("#btn-dl-srt"):
                    unlocked = True
                    break
                page.wait_for_timeout(300)
            r.check("完成后下载按钮解锁", unlocked,
                    page.get_attribute("#btn-dl-srt", "title") or "")
            r.check("字幕对照里出现了译文", "译" in page.inner_text("#pairs-body"),
                    page.inner_text("#pairs-body")[:60])
            r.check("运行日志有内容", len(page.inner_text("#logs")) > 0)

            page.click(".subtabs button[data-tab='apilogs']")
            page.wait_for_timeout(1200)
            r.check("翻译记录页签列出了调用",
                    len(page.query_selector_all("#alogs-body tr")) >= 1,
                    str(len(page.query_selector_all("#alogs-body tr"))))

            page.screenshot(path=str(shot / "ui.png"), full_page=False)

            # ------------------------------------------------ 删除任务
            print("\n[8] 删除任务")
            page.click(".subtabs button[data-tab='pairs']")
            page.wait_for_timeout(300)
            page.click("#joblist li .jdel")
            page.wait_for_timeout(1500)
            r.check("任务已从列表移除",
                    len(page.query_selector_all("#joblist li")) == 0,
                    str(len(page.query_selector_all("#joblist li"))))

            page.wait_for_timeout(500)
            browser.close()

        r.check("全程无 console 报错", not errors, str(errors[:3]))
    finally:
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(shot, ignore_errors=True)

    return r.finish()


if __name__ == "__main__":
    raise SystemExit(main())
