"""结构冒烟：分层是否守住、废弃设计是否清干净、核心契约是否还成立。

不发请求、不起服务、不碰真实数据 —— 只读源码 + 在临时目录里验证核心类的行为。
这是重构后的第一道闸门：结构塌了，后面跑得再顺也是白搭。

    .venv\\Scripts\\python.exe .tests/smoke_refactor.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# 直接跑脚本时 sys.path[0] 是 .tests/，先把项目根塞进去才 import 得到 _harness
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # .tests 不是合法包名，直接按目录导入

from _harness import ROOT, Report, isolate, make_srt  # noqa: E402

CODE_SUFFIX = (".py", ".js", ".html", ".css")
CODE_DIRS = ("core", "app", "web")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def grep_project(pattern: str) -> list[str]:
    """在项目代码里找pattern（不含 .tests/、.tmp/、.docs/、History.md）。"""
    hits = []
    rx = re.compile(pattern)
    for d in CODE_DIRS:
        for p in (ROOT / d).rglob("*"):
            if not p.is_file() or p.suffix not in CODE_SUFFIX:
                continue
            if ".trash" in p.parts:
                continue
            for i, line in enumerate(
                p.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if rx.search(line):
                    hits.append(f"{p.relative_to(ROOT).as_posix()}:{i}")
    return hits


def main() -> int:
    r = Report("结构冒烟")
    print("\n[1] 废弃设计是否已删除")

    r.check("core/engines.py 已删除", not (ROOT / "core" / "engines.py").exists())
    r.check("web/app.js 已删除", not (ROOT / "web" / "app.js").exists())

    engine_src = read("core/engine.py")
    r.check(
        "core/engine.py 里没有 BaseEngine / OpenAICompatible",
        "BaseEngine" not in engine_src and "OpenAICompatible" not in engine_src,
    )

    cfg_src = read("core/config.py")
    r.check(
        "core/config.py 里没有 ENGINE / SRT_DEFAULT_DIR",
        "ENGINE" not in cfg_src and "SRT_DEFAULT_DIR" not in cfg_src,
    )

    r.check(
        "core/errors.py 里没有 punishes_key",
        "punishes_key" not in read("core/errors.py"),
    )

    jobs_src = read("core/jobs.py")
    r.check(
        "JobStore 里没有 list_prompts / save_source_name / ensure_subtitles",
        not any(
            n in jobs_src
            for n in ("def list_prompts", "def save_source_name", "def ensure_subtitles")
        ),
    )

    keys_src = read("core/keys.py")
    r.check(
        "KeyPool 里没有 usage_count() / fail_count() / mark_disabled()",
        not any(
            n in keys_src
            for n in ("def usage_count", "def fail_count", "def mark_disabled")
        ),
    )

    print("\n[2] 死代码零引用")
    for pat, why in [
        (r"\bengines\b", "旧引擎抽象层"),
        (r"OpenAICompatible", "OpenAI 兼容引擎"),
        (r"LEGACY_", "旧参数/提示词兼容字段"),
        (r"ensure_subtitles|subtitle_lines", "旧字幕迁移接口"),
        (r"build_ass\(", "已并入 blocks_from_subtitles"),
        (r"fs/list", "已删的文件浏览接口"),
        (r"reset_cooldown", "已改名为 reset_usage"),
        (r"SOURCE_SRT_PATH", "已改为 INPUT_NAME"),
    ]:
        hits = grep_project(pat)
        r.check(f"{why}（{pat}）零引用", not hits, str(hits[:3]))

    print("\n[3] 分层")
    main_py = read("app/main.py")
    r.check(
        "app/main.py 只是装配层（<=120 行）",
        len(main_py.splitlines()) <= 120,
        f"{len(main_py.splitlines())} 行",
    )
    for name in ("jobs", "keys", "prompts"):
        r.check(f"app/routers/{name}.py 存在", (ROOT / "app" / "routers" / f"{name}.py").exists())
    r.check(
        "main.py 不自己写业务（无 sqlite / 无 Thread）",
        "sqlite3" not in main_py and "threading" not in main_py,
    )

    db_src = read("core/db.py")
    r.check(
        "core/db.py 用全局锁串行所有 SQLite 访问",
        "_SQLITE_LOCK" in db_src and "RLock" in db_src,
    )
    r.check(
        "core/jobs.py 与 core/keys.py 都走 db.connect",
        "db.connect(" in jobs_src and "db.connect(" in keys_src,
    )

    print("\n[4] SQLite 并发（曾在这里栽过：Windows 上反复开关连接会把库降级成只读）")
    tmp = Path(tempfile.mkdtemp(prefix="srt-struct-"))
    try:
        from core import db as DB

        db_path = tmp / "stress.db"
        DB.init_schema(
            db_path, "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT);"
        )
        errors: list[str] = []

        def worker(n: int) -> None:
            try:
                for i in range(30):
                    with DB.connect(db_path) as conn:
                        conn.execute(
                            "INSERT INTO t (v) VALUES (?)", (f"{n}-{i}",)
                        )
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        r.check("8 线程 × 30 次写入无异常", not errors, str(errors[:2]))

        with DB.connect(db_path) as conn:
            n_rows = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        r.check("240 行全部落库", n_rows == 240, str(n_rows))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n[5] 事件总线（SSE 的数据源）")
    from core.events import (
        END_FINISHED,
        EventBroker,
        End,
        HISTORY_SIZE,
        HEARTBEAT_SECONDS,
        sse_frame,
    )

    broker = EventBroker()
    s1, s2 = broker.subscribe(), broker.subscribe()
    broker.publish("log", message="hello")
    broker.close(END_FINISHED)

    def drain(sub):
        got = []
        while True:
            item = sub.get(timeout=1.0)
            if item is None:
                break
            got.append(item)
            if isinstance(item, End):
                break
        return got

    g1, g2 = drain(s1), drain(s2)
    r.check(
        "广播：两个订阅者都收到同一条事件",
        any(getattr(x, "type", None) == "log" for x in g1)
        and any(getattr(x, "type", None) == "log" for x in g2),
    )
    r.check(
        "广播：两个订阅者都收到 End（不会互相抢）",
        any(isinstance(x, End) for x in g1) and any(isinstance(x, End) for x in g2),
    )

    broker2 = EventBroker()
    for i in range(3):
        broker2.publish("log", message=str(i))
    r.check("seq 单调递增", [e.seq for e in broker2.since(0)] == [1, 2, 3])
    r.check("since(seq) 只补发之后的", len(broker2.since(1)) == 2, str(len(broker2.since(1))))
    broker2.close()
    r.check("close 后 publish 不再生效", broker2.publish("log", message="x") is None)

    frame = sse_frame(broker2.since(0)[0])
    r.check("SSE 帧含 id/event/data", "id: " in frame and "event: " in frame and "data: " in frame)
    r.check("环形缓冲与心跳有默认值", HISTORY_SIZE == 200 and HEARTBEAT_SECONDS == 15.0)

    print("\n[6] 任务运行层")
    from core.runner import JobBusyError, JobRunner, RunnerRegistry

    r.check("runner 暴露 JobRunner / RunnerRegistry / JobBusyError", all(
        x is not None for x in (JobRunner, RunnerRegistry, JobBusyError)
    ))

    from core.watchdog import RequestTimeout, run_with_timeout

    timed_out = False
    try:
        run_with_timeout(lambda: __import__("time").sleep(5), timeout=0.3)
    except RequestTimeout:
        timed_out = True
    r.check("run_with_timeout 超时抛 RequestTimeout", timed_out)

    print("\n[7] core 各层契约")
    from core.context import MAX_TURNS, build_turns, trim_turns

    turns = build_turns("全文", "无", {"1": "a"})
    long_turns = turns + [{"role": "user", "text": f"第{i}轮"} for i in range(10)]
    trimmed = trim_turns(long_turns)
    r.check("trim_turns 超限会截断", len(trimmed) < len(long_turns), f"{len(long_turns)}->{len(trimmed)}")
    r.check("trim_turns 保留开头上下文", trimmed[0]["text"].startswith("<full_context>"))

    from core.payload import LOG_MAX_CHARS, TRUNCATED_FLAG, cap_payload

    big = {"contents": [{"role": "user", "parts": [{"text": "a" * (LOG_MAX_CHARS + 1000)}]}]}
    capped = cap_payload(big)
    r.check("cap_payload 超限打截断标记", capped.get(TRUNCATED_FLAG) is True)
    r.check(
        "cap_payload 真的缩短了",
        len(capped["contents"][0]["parts"][0]["text"]) < LOG_MAX_CHARS,
    )

    from core.models import THINKING_OFF, needs_thinking_config

    r.check("THINKING_OFF 不发送 thinking_config", needs_thinking_config(THINKING_OFF) is False)
    r.check("非空 thinking 等级要发送", needs_thinking_config("LOW") is True)

    from core.translator import BatchOutcome, RunResult, Translator, chunk_list

    r.check("chunk_list 分批正确", [len(c) for c in chunk_list(list(range(7)), 3)] == [3, 3, 1])
    r.check("BatchOutcome 四种结局齐全", {o.value for o in BatchOutcome} == {"OK", "PAUSED", "NO_KEY", "FATAL"})

    print("\n[8] JobStore / KeyPool / 导出")
    tmp = Path(tempfile.mkdtemp(prefix="srt-store-"))
    try:
        isolate(tmp)
        from core import config as CFG
        from core import jobs as J
        from core.exporter import PARAM_OUTPUT_ASS, PARAM_OUTPUT_SRT, export_outputs
        from core.keys import KeyPool

        src = make_srt(tmp / "s.srt", n=4)
        store = J.create_job(
            src,
            CFG.default_config(),
            {J.PROMPT_SYSTEM: "sys", J.PROMPT_CUSTOMER: "cust"},
            log_dir=tmp / "log",
        )
        r.check("建库后字幕已内嵌", store.count_subtitles() == 4, str(store.count_subtitles()))
        prog = store.progress(2)
        r.check("progress 字段不含已废弃的 fallback", "fallback" not in prog, str(prog.keys()))
        r.check("progress 百分比可算", prog["percent"] == 0.0 and prog["total"] == 2)

        store.save_batch_result(1, J.STATUS_OK, {"1": "一", "2": "二"}, key_id=1)
        store.apply_translations({"1": "一", "2": "二"})
        res = export_outputs(store, store.subtitles())
        r.check("导出 srt 存在", Path(res.srt_path).exists(), res.srt_path)
        r.check("导出 ass 存在", Path(res.ass_path).exists(), res.ass_path)
        r.check("导出路径写回参数", bool(store.get_param(PARAM_OUTPUT_SRT)) and bool(store.get_param(PARAM_OUTPUT_ASS)))
        r.check("缺译文的字幕会被点名", set(res.missing) == {"3", "4"}, str(res.missing))

        pool = KeyPool(tmp / "api_keys.db")
        pool.add_keys([("k1", "p1"), ("k2", "p2")])
        r.check("可用 Key 计数正确", pool.available_count() == 2, str(pool.available_count()))
        pool.disable(1)
        r.check("禁用后可用数下降", pool.available_count() == 1)
        r.check("acquire 只给启用的 Key", (pool.acquire() or type("X", (), {"id": 0})()).id == 2)

        # 路径穿越：job_id 来自 URL，不能让它跳出 log/
        from fastapi import HTTPException

        from app.routers.jobs import resolve_store

        blocked = False
        for bad in ("../evil.db", "..", "a/b.db", "C:\\windows\\x.db"):
            try:
                resolve_store(bad)
            except HTTPException:
                blocked = True
            except Exception:
                pass
        r.check("resolve_store 拒绝路径穿越", blocked)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n[9] Translator 事件签名")
    from core.translator import Translator as T  # noqa: F811

    seen: list[tuple] = []

    def spy(*a, **kw):
        seen.append((a, kw))

    tr = T.__new__(T)
    tr.emit = spy
    tr.store = type("S", (), {"progress": lambda self: {"ok": 1}})()
    tr._status("RUNNING", "go")
    r.check(
        "emit 是 emit(type, **data) 而不是旧的位置参数",
        seen and seen[0][0] == ("status",) and "status" in seen[0][1],
        str(seen[:1]),
    )

    print("\n[10] 前端")
    js_dir = ROOT / "web" / "js"
    mods = sorted(js_dir.glob("*.js"))
    r.check("web/js/ 至少 10 个模块", len(mods) >= 10, str(len(mods)))

    html = read("web/index.html")
    r.check("index.html 用 ES module 入口", 'type="module"' in html and "js/main.js" in html)
    r.check("index.html 里没有引擎下拉（已只留 Gemini）", "#p-engine" not in html and "#new-engine" not in html)

    node = shutil.which("node")
    if node:
        bad = []
        tmpdir = Path(tempfile.mkdtemp(prefix="srt-nodecheck-"))
        try:
            for m in mods:
                copy = tmpdir / (m.stem + ".mjs")
                copy.write_text(m.read_text(encoding="utf-8"), encoding="utf-8")
                p = subprocess.run([node, "--check", str(copy)], capture_output=True, text=True)
                if p.returncode != 0:
                    bad.append(f"{m.name}: {p.stderr.strip()[:120]}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        r.check("所有前端模块语法正确（node --check）", not bad, str(bad[:2]))
    else:
        print("  --   node 不可用，跳过语法检查")

    # 没有孤立模块：除 main.js 外每个模块都得被别人 import
    all_src = html + "\n" + "\n".join(m.read_text(encoding="utf-8") for m in mods)
    orphan = [
        m.name
        for m in mods
        if m.stem != "main"
        and not re.search(
            rf"""(from|import)\s+["'][^"']*{re.escape(m.stem)}(\.js)?["']""", all_src
        )
    ]
    r.check("没有孤立的前端模块", not orphan, str(orphan))

    return r.finish()


if __name__ == "__main__":
    raise SystemExit(main())
