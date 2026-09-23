"""启动工作台：拉起 FastAPI 并自动打开浏览器。

双击 start.bat 或执行：.venv\\Scripts\\python.exe run.py
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from core.config import PORT


def _open_browser(url: str) -> None:
    time.sleep(1.5)
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}/"
    print(f"字幕翻译工作台启动中：{url}")
    threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=PORT, reload=False)
