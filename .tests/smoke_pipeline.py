"""流水线冒烟：建任务 → 跑批 → 暂停 → 续跑 → 导出 → SSE 断线重连。

全程在进程内跑（不起 HTTP 服务），但用的是**真的** JobStore / Translator / RunnerRegistry /
EventBroker —— 只有 `core.engine.generate_json` 被换成假引擎，一个字节都不发给真实 API。

SSE 那一节直接驱动 `job_events` 端点函数（用一个带 Last-Event-ID 的假 Request），
配合 GatedEngine 逐批放行，时序完全可控、结果确定 ——
靠真浏览器去撞时序只会得到一堆随机失败。

    .venv\\Scripts\\python.exe .tests/smoke_pipeline.py
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # .tests 不是合法包名，直接按目录导入

from _harness import (  # noqa: E402
    FakeEngine,
    Report,
    StreamReader,
    install_stream_capture,
    isolate,
    last_stream,
    make_srt,
)


def seqs_of(frames: list[str]) -> list[int]:
    """数据帧的 id:（= 事件 seq）。end 帧没有 id，所以不会被算进来。"""
    out = []
    for f in frames:
        if f.startswith("id: "):
            try:
                out.append(int(f.split("id: ", 1)[1].split("\n", 1)[0]))
            except ValueError:
                pass
    return out


def frame_type(frame: str) -> str:
    """取出帧里的 `event:` 字段。

    数据帧长这样 `id: 5\\nevent: batch\\ndata: {...}` —— 直接 startswith('event:')
    是匹配不上的，必须真的去解析。
    """
    m = re.search(r"^event:\s*(\S+)", frame, re.M)
    return m.group(1) if m else ""


class GatedEngine(FakeEngine):
    """每批都要等一个「放行令牌」才返回 —— 让测试完全掌控节奏。

    断线重连那一条必须这样：先掐断、再放行、最后带着 Last-Event-ID 重连，
    才能确定「补发的确实是断线期间发生的事件」。
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.release: "queue.Queue[int]" = __import__("queue").Queue()

    def __call__(self, *a, **kw):
        self.release.get(timeout=90)
        return super().__call__(*a, **kw)


class FakeRequest:
    """够 `job_events` 用的最小 Request：只读 headers。"""

    def __init__(self, last_event_id: str | None = None):
        self.headers = {"last-event-id": last_event_id or ""}


def new_env(n_subtitles: int = 6, batch_size: int = 2, keys: int = 2):
    """开一套全新环境（临时目录 + 临时 Key 库 + 干净的任务注册表）。"""
    from core import config as CFG
    from core import jobs as J
    from core.keys import KeyPool

    tmp = Path(tempfile.mkdtemp(prefix="srt-pipe-"))
    isolate(tmp)
    pool = KeyPool(tmp / "api_keys.db")
    if keys:
        pool.add_keys([(f"KEY-{i}", f"proj-{i}") for i in range(1, keys + 1)])

    cfg = CFG.default_config()
    cfg["BATCH_SIZE"] = batch_size
    cfg["RETRY_WAIT"] = 0.2  # 失败退避压到最短，冒烟不等

    src = make_srt(tmp / "in.srt", n=n_subtitles)
    store = J.create_job(
        src,
        cfg,
        {J.PROMPT_SYSTEM: "你是字幕翻译器", J.PROMPT_CUSTOMER: "口语化"},
        log_dir=tmp / "log",
    )
    return tmp, pool, cfg, store


def run_job(store, pool, cfg, resume: bool = True):
    """同步跑一个任务（不走线程），返回 (result, events)。"""
    from core.translator import Translator

    events: list[dict] = []
    tr = Translator(store, pool, cfg, emit=lambda t, **d: events.append({"type": t, **d}))
    return tr.run(resume=resume), events


def wait_until(fn, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    r = Report("流水线冒烟")

    # ---------------------------------------------------------------- 建任务
    print("\n[1] 建任务")
    tmp, pool, cfg, store = new_env(n_subtitles=6, batch_size=2)
    try:
        r.check("任务库已建", store.db_path.exists())
        r.check("字幕已内嵌（6 条）", store.count_subtitles() == 6, str(store.count_subtitles()))
        r.check("建库时还不算总批次（run 时才算）",
                int(store.get_param("TOTAL_BATCHES", 0) or 0) == 0,
                str(store.get_param("TOTAL_BATCHES")))
        r.check("字幕名只记文件名", store.get_param("INPUT_NAME") == "in.srt",
                str(store.get_param("INPUT_NAME")))
        r.check("两条提示词随任务入库",
                store.get_prompt("system_prompt") == "你是字幕翻译器"
                and store.get_prompt("customer_prompt") == "口语化")

        # ------------------------------------------------------------ 空任务
        print("\n[2] 没有字幕的任务")
        e = new_env(n_subtitles=0)
        try:
            res, _ = run_job(e[3], e[1], e[2])
            r.check("空任务直接 ERROR，不空转", res.outcome == "ERROR", res.outcome)
        finally:
            shutil.rmtree(e[0], ignore_errors=True)

        # ------------------------------------------------------------ 完整跑一轮
        print("\n[3] 完整跑一轮")
        eng = FakeEngine().install()
        res, events = run_job(store, pool, cfg)
        eng.uninstall()

        r.check("结果是 DONE", res.outcome == "DONE", res.outcome)
        r.check("总批次 = 3", res.progress["total"] == 3, str(res.progress))
        r.check("3 批全部成功", res.progress["ok"] == 3 and res.progress["error"] == 0, str(res.progress))
        r.check("进度 100%", res.progress["percent"] == 100.0, str(res.progress["percent"]))

        trans = store.translations_map()
        r.check("6 条译文全部落库", len(trans) == 6, str(len(trans)))
        r.check("译文内容正确", all(v == f"译{i}" for i, v in trans.items()), str(sorted(trans.items())[:2]))

        types = [x["type"] for x in events]
        r.check("事件含 status", "status" in types)
        r.check("事件含 log", "log" in types)
        r.check("事件含 batch", "batch" in types)
        r.check("batch 事件数 = 3", types.count("batch") == 3, str(types.count("batch")))
        batch_events = [x for x in events if x["type"] == "batch"]
        r.check("每个 batch 事件都带 progress", all("progress" in x for x in batch_events))
        r.check("batch 的 progress 递增",
                [x["progress"]["ok"] for x in batch_events] == [1, 2, 3],
                str([x["progress"]["ok"] for x in batch_events]))
        r.check("终态事件是 status 且 status=DONE",
                events[-1]["type"] == "status" and events[-1].get("status") == "DONE",
                str(events[-1])[:120])
        r.check("跑完会导出（export 事件）", "export" in types)

        # ------------------------------------------------------------ 产物与日志
        print("\n[4] 产物与日志")
        from core.exporter import PARAM_OUTPUT_ASS, PARAM_OUTPUT_SRT

        srt_path = Path(store.get_param(PARAM_OUTPUT_SRT, ""))
        ass_path = Path(store.get_param(PARAM_OUTPUT_ASS, ""))
        r.check("cn.srt 已生成", srt_path.exists(), str(srt_path))
        r.check("cn.ass 已生成", ass_path.exists(), str(ass_path))
        r.check("srt 里是译文不是原文", "译1" in srt_path.read_text(encoding="utf-8"))

        logs = store.api_log_entries()
        r.check("api_logs 记了 3 次调用", len(logs) == 3, str(len(logs)))
        detail = store.api_log_detail(logs[0]["id"]) if logs else None
        r.check("日志含完整请求体", bool(detail and detail.get("request_payload")),
                str(list((detail or {}).keys())))
        r.check("日志含响应体", bool(detail and detail.get("response_payload")))
        r.check("日志带 Key 归属", bool(detail and detail.get("key_id")),
                str((detail or {}).get("key_id")))

        # ------------------------------------------------------------ 暂停 / 续跑
        print("\n[5] 暂停与续跑")
        e2 = new_env(n_subtitles=6, batch_size=2)
        try:
            from core.translator import Translator

            tr = Translator(e2[3], e2[1], e2[2], emit=lambda *a, **k: None)

            class StopEngine(FakeEngine):
                """跑完第 2 批就请求暂停。"""

                def __call__(self, *a, **kw):
                    out = super().__call__(*a, **kw)
                    if len(self.calls) >= 2:
                        tr.stop()
                    return out

            eng2 = StopEngine().install()
            res2 = tr.run(resume=True)
            eng2.uninstall()

            r.check("中途暂停 → PAUSED", res2.outcome == "PAUSED", res2.outcome)
            r.check("暂停时已完成 2 批", res2.progress["ok"] == 2, str(res2.progress))
            r.check("状态已落库", e2[3].status == "PAUSED", e2[3].status)

            # 续跑走的是新的 Translator（真服务里也是新起一个 runner）
            eng3 = FakeEngine().install()
            tr2 = Translator(e2[3], e2[1], e2[2], emit=lambda *a, **k: None)
            res3 = tr2.run(resume=True)
            eng3.uninstall()
            r.check("续跑 → DONE", res3.outcome == "DONE", res3.outcome)
            r.check("续跑后 3 批全齐", res3.progress["ok"] == 3, str(res3.progress))
            r.check("续跑不会重翻已完成的批次（引擎只被调 1 次）",
                    len(eng3.calls) == 1, str(eng3.calls))
        finally:
            shutil.rmtree(e2[0], ignore_errors=True)

        # ------------------------------------------------------------ 没有 Key
        print("\n[6] 没有可用 Key")
        e3 = new_env(n_subtitles=4, batch_size=2, keys=0)
        try:
            res4, _ = run_job(e3[3], e3[1], e3[2])
            r.check("无 Key 时立刻 NO_KEY 停止", res4.outcome == "NO_KEY", res4.outcome)
            r.check("无 Key 时不调引擎", len(FakeEngine().calls) == 0)
        finally:
            shutil.rmtree(e3[0], ignore_errors=True)

        # ------------------------------------------------------------ 漏句纠错
        print("\n[7] 漏句纠错")
        e4 = new_env(n_subtitles=4, batch_size=4)
        try:
            eng5 = FakeEngine(missing_first=True).install()
            res5, _ = run_job(e4[3], e4[1], e4[2])
            eng5.uninstall()
            r.check("漏句后重发并最终成功", res5.outcome == "DONE", res5.outcome)
            r.check("4 条译文齐全", len(e4[3].translations_map()) == 4,
                    str(len(e4[3].translations_map())))
            labels = [x.get("status") for x in e4[3].api_log_entries()]
            r.check("记了 ERROR_MISSING_KEYS", "ERROR_MISSING_KEYS" in labels, str(labels))
        finally:
            shutil.rmtree(e4[0], ignore_errors=True)

        # ------------------------------------------------------------ SSE
        print("\n[8] SSE 与断线重连")
        e5 = new_env(n_subtitles=6, batch_size=2)
        try:
            from app import deps
            from app.routers.jobs import job_events

            install_stream_capture()
            gat = GatedEngine().install()
            job_id = e5[3].db_path.name
            runner = deps.get_registry().start(job_id, e5[3], e5[1], e5[2])

            # 第 1 批还卡在引擎里 —— 先接上流 A 读几帧
            job_events(request=FakeRequest(None), job_id=job_id)
            reader_a = StreamReader(last_stream())
            reader_a.start()
            wait_until(lambda: len(reader_a.frames) >= 4, 10)
            frames_a = list(reader_a.frames)
            r.check("流 A 首帧是 retry 指令",
                    bool(frames_a) and frames_a[0].startswith("retry:"), str(frames_a[:1]))
            seqs_a = seqs_of(frames_a)
            r.check("流 A 收到了带 seq 的事件", bool(seqs_a), str(seqs_a))
            last_a = max(seqs_a) if seqs_a else 0

            reader_a.stop()  # 浏览器断开

            # 放行第 1 批：等到 batch 事件确实进了环形缓冲再重连，
            # 否则「发布 / 订阅」谁先谁后是随机的，断言会飘
            gat.release.put(1)
            r.check("第 1 批完成",
                    wait_until(lambda: len(e5[3].completed_batches()) == 1),
                    str(list(e5[3].completed_batches())))
            r.check("batch 事件已进入事件缓冲",
                    wait_until(lambda: any(x.type == "batch" for x in runner.events_since(0))),
                    str([x.type for x in runner.events_since(0)]))

            # 带着 Last-Event-ID 重连
            job_events(request=FakeRequest(str(last_a)), job_id=job_id)
            reader_b = StreamReader(last_stream())
            reader_b.start()
            wait_until(lambda: any(frame_type(f) == "batch" for f in reader_b.frames), 15)
            frames_b = list(reader_b.frames)
            seqs_b = seqs_of(frames_b)
            r.check("重连后补发了断线期间的事件", bool(seqs_b), str(seqs_b))
            r.check("补发的 seq 全部 > 断线时的 seq",
                    bool(seqs_b) and all(s > last_a for s in seqs_b), f"{seqs_b} vs {last_a}")
            r.check("补发里确实有 batch 事件",
                    any(frame_type(f) == "batch" for f in frames_b), str(frames_b[:2]))

            # 放完剩下两批，看终态（这是重连后的**实时**投递，不是补发）
            for _ in range(3):
                gat.release.put(1)
            wait_until(lambda: any(frame_type(f) == "end" for f in reader_b.frames), 30)
            frames_b_all = list(reader_b.frames)
            end_frame = next((f for f in frames_b_all if frame_type(f) == "end"), "")
            r.check("任务跑完会收到 end 帧", bool(end_frame), str(frames_b_all[-1:])[:80])
            r.check("end 的 reason 是 finished", "finished" in end_frame, end_frame[:80])
            pos_end = [i for i, f in enumerate(frames_b_all) if frame_type(f) == "end"]
            pos_batch = [i for i, f in enumerate(frames_b_all) if frame_type(f) == "batch"]
            r.check("end 排在 batch 之后（先补发再实时推）",
                    bool(pos_end) and bool(pos_batch) and pos_end[0] > pos_batch[0],
                    f"{pos_batch[:1]} vs {pos_end[:1]}")
            reader_b.stop()
            gat.uninstall()

            wait_until(lambda: deps.get_registry().get(job_id) is None, 10)

            # ---------------------------------------------------- 单任务槽
            print("\n[9] 全局单任务槽")
            from core.runner import JobBusyError

            e6 = new_env(n_subtitles=4, batch_size=2)
            try:
                g2 = GatedEngine().install()
                deps.get_registry().start("job_a", e6[3], e6[1], e6[2])
                busy = False
                try:
                    deps.get_registry().start("job_b", e6[3], e6[1], e6[2])
                except JobBusyError:
                    busy = True
                r.check("已有任务在跑时第二个被拒", busy)

                ra = deps.get_registry().get("job_a")
                if ra:
                    ra.stop()
                g2.release.put(1)
                wait_until(lambda: deps.get_registry().get("job_a") is None, 10)
                g2.uninstall()
                deps.get_registry().release("job_a")
            finally:
                shutil.rmtree(e6[0], ignore_errors=True)

            # ---------------------------------------------------- 删除任务
            print("\n[10] 删除任务")
            from core import jobs as J

            dest = J.trash_job(e5[3].db_path, e5[0] / "log")
            r.check("任务库挪进 .trash", dest.parent.name == ".trash" and dest.exists(), str(dest))
            r.check("原位置已不存在", not e5[3].db_path.exists())
        finally:
            shutil.rmtree(e5[0], ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return r.finish()


if __name__ == "__main__":
    raise SystemExit(main())
