"""Gemini 字幕翻译工作台 —— FastAPI 装配层。

这个文件只负责：把路由挂上、提供应用级元数据（/api/config）、接收浏览器上传的字幕、
托管前端静态资源。业务规则全在 core，任务相关的路由全在 app/routers/。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import deps
from app.routers import jobs, keys, prompts
from core import config as CFG
from core.models import KNOWN_MODELS, default_thinking_level, thinking_levels_for


class NoCacheStaticFiles(StaticFiles):
    """前端静态资源一律不缓存。

    这是本地工具，改完 css/js 就想立刻看到效果。默认的 StaticFiles 不带
    Cache-Control，浏览器会把旧文件缓存住 —— 按 F5 只重新验证主文档，
    css/js 仍走缓存，看起来就是「改了没生效」，非得 Ctrl+F5 才行。

    只管静态文件，API 响应不受影响。
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class UploadSrt(BaseModel):
    filename: str
    content: str


def create_app() -> FastAPI:
    app = FastAPI(title="Gemini 字幕翻译工作台")
    app.include_router(jobs.router)
    app.include_router(keys.router)
    app.include_router(prompts.router)

    @app.get("/api/config")
    def api_config():
        return {
            "defaults": CFG.default_config(),
            "models": KNOWN_MODELS,
            "languages": CFG.TARGET_LANGUAGES,
            "model_thinking": {
                m: {
                    "levels": thinking_levels_for(m),
                    "default": default_thinking_level(m),
                }
                for m in KNOWN_MODELS
            },
            "port": CFG.PORT,
        }

    @app.post("/api/upload-srt")
    def upload_srt(req: UploadSrt):
        """接收浏览器选中的字幕内容，落到项目 input/ 目录并返回路径。

        浏览器原生 <input type="file"> 出于安全不给出真实路径，所以走「上传内容」
        这条路：内容落盘后照样会被逐条 embed 进任务库，之后不再依赖任何外部文件。
        """
        name = Path(req.filename).name or "subtitle.srt"
        dest_dir = deps.INPUT_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        if dest.exists() and dest.read_bytes().decode("utf-8", "replace") != req.content:
            stamp = datetime.now().strftime("%H%M%S")
            dest = dest_dir / f"{Path(name).stem}_{stamp}{Path(name).suffix}"
        dest.write_bytes(req.content.encode("utf-8"))
        return {"ok": True, "path": str(dest), "filename": dest.name}

    @app.get("/favicon.ico")
    def favicon():
        """避免浏览器请求图标时打出一条 404 噪音。"""
        return Response(status_code=204)

    app.mount("/", NoCacheStaticFiles(directory=str(deps.WEB_DIR), html=True), name="web")
    return app


app = create_app()
