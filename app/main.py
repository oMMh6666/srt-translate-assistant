"""Gemini 字幕翻译工作台 —— FastAPI 服务层。"""

from __future__ import annotations

import json
import queue
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import config as CFG
from core import jobs as J
from core import prompts as P
from core import srt as S
from core.engines import list_engines
from core.jobs import JobStore, scan_log_dir
from core.keys import KeyPool
from core.models import KNOWN_MODELS, default_thinking_level, thinking_levels_for
from core.translator import Translator

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "log"
WEB_DIR = ROOT / "web"

app = FastAPI(title="Gemini 字幕翻译工作台")

RUNNING: dict[str, dict] = {}
_LOCK = threading.Lock()
SERVICE_LOG = ROOT / "service.log"


# --------------------------------------------------------------------- 工具


def record_error(prefix: str, exc: BaseException | None = None) -> str:
    """把异常写进 service.log。

    进程万一被系统收掉，界面日志会跟着一起消失；落盘才查得到到底发生了什么。
    """
    stamp = datetime.now().isoformat(timespec="seconds")
    detail = traceback.format_exc() if exc is None else "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    text = f"[{stamp}] {prefix}\n{detail}\n"
    try:
        with SERVICE_LOG.open("a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    return text


def _pool() -> KeyPool:
    return KeyPool(ROOT / "api_keys" / "api_keys.db")


def resolve_job(job_id: str) -> JobStore:
    candidates = [LOG_DIR / job_id, LOG_DIR / job_id / "job.db", Path(job_id)]
    for c in candidates:
        if c.exists() and c.suffix == ".db":
            return JobStore(c)
    raise HTTPException(404, f"找不到任务：{job_id}")


def job_id_of(store: JobStore) -> str:
    try:
        return str(store.db_path.relative_to(LOG_DIR))
    except ValueError:
        return str(store.db_path)


def _current_running_job() -> str | None:
    """当前正在运行的任务。同一时间只允许一个任务执行。"""
    for jid, item in list(RUNNING.items()):
        if item["thread"].is_alive():
            return jid
        RUNNING.pop(jid, None)
    return None


def build_cfg(params: dict) -> dict:
    cfg = CFG.default_config()
    for k, v in params.items():
        if k in cfg:
            cfg[k] = v
    return cfg


# --------------------------------------------------------------------- 配置


@app.get("/api/config")
def api_config():
    cfg = CFG.default_config()
    return {
        "defaults": cfg,
        "engines": list_engines(),
        "models": KNOWN_MODELS,
        "languages": CFG.TARGET_LANGUAGES,
        "model_thinking": {
            m: {"levels": thinking_levels_for(m), "default": default_thinking_level(m)}
            for m in KNOWN_MODELS
        },
        "thinking_levels": {
            m: thinking_levels_for(m) for m in KNOWN_MODELS
        },
        "srt_default_dir": CFG.SRT_DEFAULT_DIR,
        "port": CFG.PORT,
    }


@app.get("/api/fs/list")
def api_fs_list(path: str = "", exts: str = ".srt,.db,.ass"):
    """本地文件浏览（用于直接选 SRT / log 库，无需复制到项目目录）。"""
    target = Path(path) if path else Path(CFG.SRT_DEFAULT_DIR)
    if not target.exists():
        target = Path(CFG.SRT_DEFAULT_DIR if CFG.SRT_DEFAULT_DIR else ROOT)
    if not target.exists():
        target = ROOT

    allow = [e.strip().lower() for e in exts.split(",") if e.strip()]
    entries = []
    try:
        for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name.startswith("."):
                continue
            if p.is_dir():
                entries.append({"name": p.name, "path": str(p), "type": "dir"})
            elif p.suffix.lower() in allow:
                entries.append(
                    {
                        "name": p.name,
                        "path": str(p),
                        "type": "file",
                        "size": p.stat().st_size,
                    }
                )
    except PermissionError:
        raise HTTPException(403, "没有权限访问该目录")

    return {"current": str(target), "parent": str(target.parent), "entries": entries}


# --------------------------------------------------------------------- 任务


@app.get("/api/jobs")
def api_jobs():
    try:
        keys = _pool().stats()
    except Exception as e:  # noqa: BLE001 - Key 库异常不该拖垮任务列表
        record_error("load key stats failed", e)
        keys = {"total": 0, "active": 0, "available": 0}
    return {
        "jobs": scan_log_dir(LOG_DIR),
        "running_job": _current_running_job(),
        "keys": keys,
    }


class CreateJobRequest(BaseModel):
    input_file: str
    params: dict = {}
    reflect_template: str = "reflect.md"
    custom_template: str = "custom_prompt.md"


@app.post("/api/jobs")
def api_create_job(req: CreateJobRequest):
    src = Path(req.input_file)
    if not src.exists():
        raise HTTPException(400, f"字幕文件不存在：{req.input_file}")

    params = CFG.default_config()
    params.update({k: v for k, v in req.params.items() if v not in (None, "")})

    # 两条提示词随任务入库（system_prompt / customer_prompt），不再散落成文件
    prompts = {
        J.PROMPT_SYSTEM: P.read_prompt(req.reflect_template),
        J.PROMPT_CUSTOMER: P.read_prompt(req.custom_template),
    }
    store = J.create_job(src, params, prompts, log_dir=LOG_DIR)
    return {"job_id": job_id_of(store), "db_path": str(store.db_path)}


def _pairs(store: JobStore) -> list[dict]:
    """原文 / 译文 对照列表（主界面主体）。

    逐条字幕都物化在库里（id/时间/原文齐全，只空译文一列），
    加载 log 后不必再去匹配外部 srt。
    """
    # 旧库首次打开时把已完成的译文补进字幕行
    store.apply_translations(store.translations_map())

    rows = store.subtitle_rows()
    if not rows:
        return []
    batch_size = max(int(store.get_param("BATCH_SIZE", 20) or 20), 1)
    statuses = store.batch_statuses()

    out = []
    for i, r in enumerate(rows):
        idx = i // batch_size + 1
        out.append(
            {
                "id": str(r["id"]),
                "index": i + 1,
                "time": r["time"],
                "original": r["text"],
                "translation": r["translation"] or "",
                "batch": idx,
                "status": statuses.get(idx, J.STATUS_PENDING),
            }
        )
    return out


def _out_ready(store: JobStore, param_key: str) -> bool:
    """产物能不能下载 = 参数里有路径且文件确实还在磁盘上。"""
    p = store.get_param(param_key)
    return bool(p and Path(p).exists())


@app.get("/api/jobs/{job_id}")
def api_job_detail(job_id: str, pairs: bool = True):
    store = resolve_job(job_id)
    params = store.get_params()
    total = int(params.get(J.PARAM_TOTAL_BATCHES, 0) or 0)
    meta = store.source_meta()
    # 旧库第一次打开时按遗留的 INPUT_FILE 路径补一次逐条字幕
    if not meta["lines"]:
        store.ensure_subtitles()
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
        # 界面据此决定「下载」按钮能不能点：没跑完 / 产物被删都不给点，
        # 省得用户点了才收到一句「产物尚未生成」
        "outputs_ready": {
            "srt": _out_ready(store, "OUTPUT_SRT"),
            "ass": _out_ready(store, "OUTPUT_ASS"),
        },
        "pairs": _pairs(store) if pairs else [],
        "running": _current_running_job() == job_id,
    }


class PromptUpdate(BaseModel):
    name: str
    content: str


@app.put("/api/jobs/{job_id}/prompts")
def api_update_prompt(job_id: str, req: PromptUpdate):
    store = resolve_job(job_id)
    store.save_prompt(req.name, req.content)
    return {"ok": True}


@app.put("/api/jobs/{job_id}/params")
def api_update_params(job_id: str, params: dict):
    store = resolve_job(job_id)
    if job_id in RUNNING:
        raise HTTPException(400, "任务正在运行，请先暂停")
    store.save_params(params)
    return {"ok": True, "params": store.get_params()}


class UploadSrt(BaseModel):
    filename: str
    content: str


@app.post("/api/upload-srt")
def api_upload_srt(req: UploadSrt):
    """接收浏览器选中的字幕内容，落到项目 input/ 目录并返回路径。

    浏览器原生 <input type="file"> 出于安全不给出真实路径，所以走「上传内容」
    这条路：内容落盘后照样会被 embed 进任务库，后续续跑不再依赖任何外部文件。
    """
    name = Path(req.filename).name or "subtitle.srt"
    dest_dir = ROOT / "input"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    if dest.exists() and dest.read_bytes().decode("utf-8", "replace") != req.content:
        stamp = datetime.now().strftime("%H%M%S")
        dest = dest_dir / f"{Path(name).stem}_{stamp}{Path(name).suffix}"
    dest.write_bytes(req.content.encode("utf-8"))
    return {"ok": True, "path": str(dest), "filename": dest.name}


@app.post("/api/jobs/{job_id}/start")
def api_start_job(job_id: str, resume: bool = True):
    store = resolve_job(job_id)

    if not store.has_subtitles() and not store.ensure_subtitles():
        raise HTTPException(
            400,
            "这个 log 里没有逐条字幕，记录的原始路径也找不到了，无法续跑。",
        )

    # 同一时间只允许一个任务在跑：批次必须串行消费上一批译文，多任务并行没有意义
    running = _current_running_job()
    if running and running != job_id:
        raise HTTPException(
            400,
            f"已有任务在运行：{running}。同一时间只能执行一个任务，请先暂停或等待它完成。",
        )
    if job_id in RUNNING:
        return {"ok": True, "message": "任务已在运行"}

    # 没有可用 Key 就不启动：跑了也是立刻停，不如把原因先讲清楚
    pool = _pool()
    if pool.available_count() == 0:
        raise HTTPException(
            400,
            "没有可用 Key（全部已禁用，或被 429 quotaValue 判定配额耗尽而自动停用）。"
            "请到「API Key 管理」启用或新增 Key 后再继续。",
        )

    cfg = build_cfg(store.get_params())
    q: queue.Queue = queue.Queue()
    subs: set = set()   # 每个 SSE 连接一条自己的队列，广播式投递

    def emit(event: dict) -> None:
        q.put(event)
        # 广播给所有正在监听的连接：只往单一 queue 里丢的话，页面重连后
        # 老的 SSE 生成器还挂在同一个 queue 上抢事件，新连接反而收不到
        for sq in list(subs):
            try:
                sq.put(event)
            except Exception:  # noqa: BLE001 - 某个连接坏了不该拖垮别的连接
                subs.discard(sq)

    translator = Translator(store, pool, cfg, on_event=emit)

    def worker() -> None:
        try:
            translator.run(resume=resume)
        except BaseException as e:  # noqa: BLE001 - 必须兜住，否则线程静默死亡
            record_error(f"job worker crashed: {job_id}", e)
            emit(
                {
                    "type": "log",
                    "level": "error",
                    "message": f"任务线程异常终止：{type(e).__name__}：{e}（详情见 service.log）",
                }
            )
        finally:
            q.put({"type": "__end__"})

    t = threading.Thread(target=worker, daemon=True)
    with _LOCK:
        RUNNING[job_id] = {"thread": t, "queue": q, "subs": subs, "translator": translator}
    t.start()
    return {"ok": True, "job_id": job_id}


@app.post("/api/jobs/{job_id}/pause")
def api_pause_job(job_id: str):
    item = RUNNING.get(job_id)
    if not item:
        raise HTTPException(400, "任务未在运行")
    item["translator"].stop()
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: str):
    """删除单个任务：任务库整个挪进 log/.trash/，不进系统回收站。"""
    store = resolve_job(job_id)
    if job_id in RUNNING or _current_running_job() == job_id:
        raise HTTPException(400, "任务正在运行，请先暂停再删除")

    try:
        dest = J.trash_job(store.db_path, LOG_DIR)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except OSError as e:
        raise HTTPException(500, f"删除失败（文件可能仍被占用）：{e}")

    return {"ok": True, "job_id": job_id, "trashed_to": str(dest)}


@app.get("/api/jobs/{job_id}/events")
def api_events(job_id: str):
    item = RUNNING.get(job_id)
    if not item:
        # 任务已结束：直接关闭流
        def closed() -> Iterator[str]:
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(closed(), media_type="text/event-stream")

    # 每个连接领一条自己的队列：多条 SSE（多个标签页 / 页面重连）互不抢事件
    mine: queue.Queue = queue.Queue()
    subs = item.get("subs")
    if subs is None:
        subs = item["subs"] = set()
    subs.add(mine)

    def gen() -> Iterator[str]:
        try:
            while True:
                try:
                    event = mine.get(timeout=15)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                if event.get("type") == "__end__":
                    RUNNING.pop(job_id, None)
                    yield "event: end\ndata: {}\n\n"
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            subs.discard(mine)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/jobs/{job_id}/apilogs")
def api_job_apilogs(job_id: str):
    """任务库 api_logs 的清单（不含正文，只给长度）。"""
    store = resolve_job(job_id)
    return {"job_id": job_id, "logs": store.api_log_entries()}


@app.get("/api/jobs/{job_id}/apilogs/{log_id}")
def api_job_apilog_detail(job_id: str, log_id: int):
    """某一条 api_log 的完整正文（请求 / 响应全量，界面只读展示）。"""
    store = resolve_job(job_id)
    row = store.api_log_detail(log_id)
    if row is None:
        raise HTTPException(404, f"没有这条记录：#{log_id}")
    return row


@app.get("/api/jobs/{job_id}/download")
def api_download(job_id: str, kind: str = "srt"):
    store = resolve_job(job_id)
    key = "OUTPUT_SRT" if kind == "srt" else "OUTPUT_ASS"
    path = store.get_param(key)
    if not path or not Path(path).exists():
        raise HTTPException(404, "产物尚未生成")
    return FileResponse(path, filename=Path(path).name)


# --------------------------------------------------------------------- Key


@app.get("/api/keys")
def api_keys():
    pool = _pool()
    return {"keys": pool.list_keys(), "stats": pool.stats()}


class KeyIn(BaseModel):
    api_key: str
    project_name: str = ""


def _pool_keys(pool: KeyPool) -> list[dict]:
    return pool.list_keys()


@app.post("/api/keys")
def api_add_keys(items: list[KeyIn]):
    """批量新增 Key；重复的直接跳过并回报，避免「点了没反应」。"""
    pool = _pool()
    existing = _pool_keys(pool)
    used_keys = {k["api_key"] for k in existing}
    used_names = {k["project_name"] for k in existing}

    fresh, skipped = [], []
    for i in items:
        k, p = (i.api_key or "").strip(), (i.project_name or "").strip()
        if not k:
            skipped.append("空的 Key，已跳过")
            continue
        if k in used_keys:
            skipped.append(f"Key 重复：{k[:8]}…")
            continue
        if p and p in used_names:
            skipped.append(f"项目名重复：{p}")
            continue
        if not p:
            p = f"key-{len(existing) + len(fresh) + 1}"
        fresh.append((k, p))
        used_keys.add(k)
        used_names.add(p)

    if fresh:
        pool.add_keys(fresh)
    return {
        "ok": True,
        "added": len(fresh),
        "skipped": skipped,
        "stats": pool.stats(),
    }


class KeyUpdate(BaseModel):
    api_key: str | None = None
    project_name: str | None = None
    is_active: bool | None = None


@app.put("/api/keys/{key_id}")
def api_update_key(key_id: int, req: KeyUpdate):
    """行内编辑：Key 与项目名可单独改，也可以只切启用状态。"""
    pool = _pool()
    existing = {k["id"]: k for k in _pool_keys(pool)}
    cur = existing.get(key_id)
    if not cur:
        raise HTTPException(404, f"找不到 Key #{key_id}")

    new_key = (req.api_key or cur["api_key"]).strip()
    new_name = (req.project_name or cur["project_name"]).strip()
    if req.api_key is not None and not new_key:
        raise HTTPException(400, "API Key 不能为空")
    if req.project_name is not None and not new_name:
        raise HTTPException(400, "项目名不能为空")

    if new_key != cur["api_key"] and new_key in {k["api_key"] for k in existing.values()}:
        raise HTTPException(400, f"已存在相同的 Key：{new_key[:8]}…")
    if new_name != cur["project_name"] and new_name in {
        k["project_name"] for k in existing.values()
    }:
        raise HTTPException(400, f"项目名重复：{new_name}")

    pool.update_key(key_id, new_key, new_name)
    if req.is_active is True:
        pool.enable(key_id)
    elif req.is_active is False:
        pool.disable(key_id)
    return {"ok": True, "stats": pool.stats()}


class BulkKeyAction(BaseModel):
    ids: list[int]
    action: str


@app.post("/api/keys/bulk")
def api_bulk_keys(req: BulkKeyAction):
    """勾选式批量操作：启用 / 禁用 / 删除 / 重置计数。"""
    pool = _pool()
    ids = [int(i) for i in req.ids if int(i) > 0]
    if not ids:
        raise HTTPException(400, "请先勾选要操作的 Key")

    action = (req.action or "").strip()
    handlers = {
        "enable": lambda i: pool.enable(i),
        "disable": lambda i: pool.disable(i),
        "delete": lambda i: pool.delete_key(i),
        "reset_usage": lambda i: pool.reset_usage_counts([i]),
    }
    if action not in handlers:
        raise HTTPException(400, f"未知操作：{action}")

    affected = 0
    for i in ids:
        try:
            handlers[action](i)
            affected += 1
        except Exception as e:  # noqa: BLE001
            record_error(f"bulk key action failed: {action} #{i}", e)
    return {"ok": True, "affected": affected, "stats": pool.stats()}


@app.delete("/api/keys/{key_id}")
def api_delete_key(key_id: int):
    _pool().delete_key(key_id)
    return {"ok": True}


@app.post("/api/keys/reset-usage")
def api_reset_usage():
    pool = _pool()
    pool.reset_usage_counts()
    return {"ok": True}


# ----------------------------------------------------------------- 提示词模板


@app.get("/api/prompts/templates")
def api_prompt_templates():
    return {"templates": P.list_prompt_files()}


class TemplateIn(BaseModel):
    name: str
    content: str


@app.post("/api/prompts/templates")
def api_save_template(req: TemplateIn):
    """把当前任务里改好的提示词另存为全局模板，供以后新建任务复用。"""
    name = req.name.strip()
    if not name.endswith(".md"):
        name += ".md"
    P.write_prompt(name, req.content)
    return {"ok": True, "name": name, "templates": P.list_prompt_files()}


@app.get("/api/prompts/templates/{name}")
def api_read_template(name: str):
    try:
        return {"name": name, "content": P.read_prompt(name)}
    except Exception:
        raise HTTPException(404, f"模板不存在：{name}")


@app.delete("/api/prompts/templates/{name}")
def api_delete_template(name: str):
    """删除全局提示词模板（内置 reflect.md / custom_prompt.md 不允许删）。"""
    if name in ("reflect.md", "custom_prompt.md"):
        raise HTTPException(400, f"内置模板不可删除：{name}")
    if not P.delete_prompt(name):
        raise HTTPException(404, f"模板不存在：{name}")
    return {"ok": True, "templates": P.list_prompt_files()}


# ------------------------------------------------------------------- 静态前端


@app.get("/favicon.ico")
def api_favicon():
    """避免浏览器请求图标时打出一条 404 噪音。"""
    return Response(status_code=204)


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
