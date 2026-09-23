"""任务接口：列表 / 详情 / 启停 / 事件流 / 翻译记录 / 下载。

这一层只做三件事：解析并校验请求参数、调 core、把结果组装成响应。
业务规则（批次怎么跑、Key 怎么调度）一律在 core。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app import deps
from core import config as CFG
from core import jobs as J
from core import prompts as P
from core.events import End, Event, HEARTBEAT_SECONDS, sse_frame
from core.runner import JobBusyError

router = APIRouter(prefix="/api/jobs")

NO_KEY_MESSAGE = (
    "没有可用 Key（全部已禁用，或被 429 quotaValue 判定配额耗尽而自动停用）。"
    "请到「API Key 管理」启用或新增 Key 后再继续。"
)


# ------------------------------------------------------------------ 工具


def resolve_store(job_id: str) -> J.JobStore:
    """job_id -> 任务库。

    job_id 就是 log/ 下的库文件名（如 `E05_20260922_1015.db`）：
    不接受路径分隔符，也不接受 log/ 之外的位置 —— 免得把整个磁盘暴露出去。
    """
    if not job_id or job_id in (".", "..") or "/" in job_id or "\\" in job_id:
        raise HTTPException(404, f"找不到任务：{job_id}")

    candidate = (deps.LOG_DIR / job_id).resolve()
    if candidate.parent != deps.LOG_DIR.resolve() or candidate.suffix != ".db":
        raise HTTPException(404, f"找不到任务：{job_id}")
    if not candidate.is_file():
        raise HTTPException(404, f"找不到任务：{job_id}")
    return J.JobStore(candidate)


def job_id_of(store: J.JobStore) -> str:
    """任务库 -> job_id（相对 log/ 的正斜杠路径）。"""
    return store.db_path.relative_to(deps.LOG_DIR).as_posix()


def build_cfg(params: dict) -> dict:
    """任务参数 -> 运行配置：只认已知键，空值回落到默认值。"""
    cfg = CFG.default_config()
    for k, v in params.items():
        if k in cfg and v not in (None, ""):
            cfg[k] = v
    return cfg


def subtitle_pairs(store: J.JobStore) -> list[dict]:
    """原文 / 译文 对照列表（主界面主体）。

    逐条字幕都物化在库里（id/时间/原文齐全，只空译文一列），不必再去匹配外部 srt。
    """
    store.apply_translations(store.translations_map())

    rows = store.subtitle_rows()
    if not rows:
        return []
    batch_size = max(int(store.get_param("BATCH_SIZE", 20) or 20), 1)
    statuses = store.batch_statuses()

    out = []
    for i, r in enumerate(rows):
        out.append(
            {
                "id": str(r["id"]),
                "index": i + 1,
                "time": r["time"],
                "original": r["text"],
                "translation": r["translation"] or "",
                "batch": i // batch_size + 1,
                "status": statuses.get(i // batch_size + 1, J.STATUS_PENDING),
            }
        )
    return out


def _out_ready(store: J.JobStore, param_key: str) -> bool:
    """产物能不能下载 = 参数里有路径且文件确实还在磁盘上。"""
    p = store.get_param(param_key)
    return bool(p and Path(p).exists())


# ------------------------------------------------------------------ 列表 / 创建


@router.get("")
def list_jobs():
    """任务列表 + 当前运行中的任务 + Key 概览（首屏一次拿齐）。"""
    try:
        keys = deps.get_pool().stats()
    except Exception as e:  # noqa: BLE001 - Key 库异常不该拖垮任务列表
        deps.record_error("load key stats failed", e)
        keys = {"total": 0, "active": 0, "available": 0, "cooling": 0}

    jobs = []
    for item in J.scan_log_dir(deps.LOG_DIR):
        item["id"] = Path(item["db_path"]).relative_to(deps.LOG_DIR).as_posix()
        jobs.append(item)
    return {
        "jobs": jobs,
        "running_job": deps.get_registry().current_id(),
        "keys": keys,
    }


class CreateJobRequest(BaseModel):
    input_file: str
    params: dict = {}
    system_template: str = "reflect.md"
    customer_template: str = "custom_prompt.md"


@router.post("")
def create_job(req: CreateJobRequest):
    src = Path(req.input_file)
    if not src.exists():
        raise HTTPException(400, f"字幕文件不存在：{req.input_file}")

    params = CFG.default_config()
    params.update({k: v for k, v in req.params.items() if v not in (None, "")})

    # 两条提示词随任务入库（system_prompt / customer_prompt），不再散落成文件
    prompts = {
        J.PROMPT_SYSTEM: P.read_prompt(req.system_template),
        J.PROMPT_CUSTOMER: P.read_prompt(req.customer_template),
    }
    store = J.create_job(src, params, prompts, log_dir=deps.LOG_DIR)
    return {"job_id": job_id_of(store), "db_path": str(store.db_path)}


# ------------------------------------------------------------------ 详情


@router.get("/{job_id}")
def job_detail(job_id: str, pairs: bool = False):
    """任务详情。

    pairs 默认不返回：一部剧上千条字幕全量回传会拖死浏览器，
    前端只在选中任务的那一刻要一次，之后靠 SSE 增量更新。
    """
    store = resolve_store(job_id)
    params = store.get_params()
    total = int(params.get(J.PARAM_TOTAL_BATCHES, 0) or 0)
    meta = store.source_meta()

    return {
        "job_id": job_id,
        "db_path": str(store.db_path),
        "params": params,
        "source": meta,
        "source_ok": meta["lines"] > 0,
        "status": params.get(J.PARAM_STATUS, J.STATUS_PENDING),
        "progress": store.progress(total),
        "prompts": {
            name: (store.get_prompt(name) or "")
            for name in (J.PROMPT_SYSTEM, J.PROMPT_CUSTOMER)
        },
        "outputs": {
            "srt": params.get("OUTPUT_SRT", ""),
            "ass": params.get("OUTPUT_ASS", ""),
        },
        "outputs_ready": {
            "srt": _out_ready(store, "OUTPUT_SRT"),
            "ass": _out_ready(store, "OUTPUT_ASS"),
        },
        "pairs": subtitle_pairs(store) if pairs else [],
        "running": deps.get_registry().current_id() == job_id,
    }


@router.get("/{job_id}/progress")
def job_progress(job_id: str):
    """轻量进度（列表轮询用，不拉字幕）。"""
    store = resolve_store(job_id)
    total = int(store.get_param(J.PARAM_TOTAL_BATCHES, 0) or 0)
    return {
        "job_id": job_id,
        "status": store.get_param(J.PARAM_STATUS, J.STATUS_PENDING),
        "progress": store.progress(total),
        "running": deps.get_registry().current_id() == job_id,
    }


@router.put("/{job_id}/params")
def update_params(job_id: str, body: dict):
    """保存运行参数（任务运行中禁止改）。"""
    store = resolve_store(job_id)
    if deps.get_registry().current_id() == job_id:
        raise HTTPException(400, "任务正在运行，请先暂停")
    params = CFG.default_config()
    params.update({k: v for k, v in body.items() if v not in (None, "")})
    store.save_params(params)
    return {"ok": True, "params": store.get_params()}


class PromptUpdate(BaseModel):
    name: str
    content: str


@router.put("/{job_id}/prompts")
def update_prompt(job_id: str, req: PromptUpdate):
    store = resolve_store(job_id)
    store.save_prompt(req.name, req.content)
    return {"ok": True}


# ------------------------------------------------------------------ 启停


@router.post("/{job_id}/start")
def start_job(job_id: str, resume: bool = True):
    store = resolve_store(job_id)
    if not store.has_subtitles():
        raise HTTPException(400, "这个任务里没有字幕，无法运行。")

    registry = deps.get_registry()
    running = registry.current_id()
    if running and running != job_id:
        raise HTTPException(
            400,
            f"已有任务在运行：{running}。同一时间只能执行一个任务，请先暂停或等待它完成。",
        )
    if registry.get(job_id):
        return {"ok": True, "message": "任务已在运行"}

    # 没有可用 Key 就不启动：跑了也是立刻停，不如把原因先讲清楚
    pool = deps.get_pool()
    if pool.available_count() == 0:
        raise HTTPException(400, NO_KEY_MESSAGE)

    try:
        registry.start(job_id, store, pool, build_cfg(store.get_params()))
    except JobBusyError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "job_id": job_id}


@router.post("/{job_id}/pause")
def pause_job(job_id: str):
    runner = deps.get_registry().get(job_id)
    if not runner:
        raise HTTPException(400, "任务未在运行")
    runner.stop()
    return {"ok": True}


@router.delete("/{job_id}")
def delete_job(job_id: str):
    """删除单个任务：任务库整个挪进 log/.trash/，不进系统回收站。"""
    store = resolve_store(job_id)
    if deps.get_registry().current_id() == job_id:
        raise HTTPException(400, "任务正在运行，请先暂停再删除")

    try:
        dest = J.trash_job(store.db_path, deps.LOG_DIR)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except OSError as e:
        raise HTTPException(500, f"删除失败（文件可能仍被占用）：{e}")

    return {"ok": True, "job_id": job_id, "trashed_to": str(dest)}


# ------------------------------------------------------------------ 事件流


@router.get("/{job_id}/events")
def job_events(request: Request, job_id: str, last: int = 0):
    """SSE 事件流。

    断线后浏览器会自动重连并带上 `Last-Event-ID`（也可以显式用 ?last=），
    服务端从环形缓冲里补发那段事件 —— 前端不必整页重拉。
    """
    runner = deps.get_registry().get(job_id)

    if runner is None:
        def closed() -> Iterator[str]:
            yield f"event: end\ndata: {json.dumps({'reason': 'closed'})}\n\n"

        return StreamingResponse(closed(), media_type="text/event-stream")

    last_seq = last or _int_or_zero(request.headers.get("last-event-id"))
    sub = runner.subscribe()

    def gen() -> Iterator[str]:
        try:
            yield "retry: 3000\n\n"
            if last_seq and last_seq < runner.oldest_seq:
                yield sse_frame(
                    _notice("断线过久，中间的事件已超出缓冲，界面可能落后于实际进度")
                )
            for event in runner.events_since(last_seq):
                yield sse_frame(event)

            while True:
                item = sub.get(HEARTBEAT_SECONDS)
                if item is None:
                    yield ": heartbeat\n\n"
                    continue
                if isinstance(item, End):
                    yield f"event: end\ndata: {json.dumps({'reason': item.reason})}\n\n"
                    break
                yield sse_frame(item)
        finally:
            runner.unsubscribe(sub)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _int_or_zero(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _notice(message: str) -> Event:
    return Event(
        seq=0, type="log", ts=datetime.now().isoformat(timespec="seconds"),
        data={"level": "warn", "message": message},
    )


# ------------------------------------------------------------------ 翻译记录


@router.get("/{job_id}/apilogs")
def api_logs(job_id: str):
    """api_logs 清单（不含正文，只给长度）。"""
    return {"job_id": job_id, "logs": resolve_store(job_id).api_log_entries()}


@router.get("/{job_id}/apilogs/{log_id}")
def api_log_detail(job_id: str, log_id: int):
    """某一条 api_log 的完整正文（请求 / 响应全量，界面只读展示）。"""
    row = resolve_store(job_id).api_log_detail(log_id)
    if row is None:
        raise HTTPException(404, f"没有这条记录：#{log_id}")
    return row


# ------------------------------------------------------------------ 下载


@router.get("/{job_id}/download")
def download(job_id: str, kind: str = "srt"):
    store = resolve_store(job_id)
    key = "OUTPUT_SRT" if kind == "srt" else "OUTPUT_ASS"
    path = store.get_param(key)
    if not path or not Path(path).exists():
        raise HTTPException(404, "产物尚未生成")
    return FileResponse(path, filename=Path(path).name)
