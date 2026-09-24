"""「假引擎 + 真服务」的独立进程。

给 `smoke_ui` / `smoke_progress` 用：引擎是假的（不打真实 API），
但 FastAPI 路由、SSE 广播、前端页面、浏览器全是真的 ——
只有这样才能测出「跑完了界面却不动」这类光读代码看不出来的问题。

    python tests/fake_server.py --port 8899 --delay 0.8
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._harness import FakeEngine, isolate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--tmp", type=str, default="")
    ap.add_argument("--delay", type=float, default=0.0)
    args = ap.parse_args()

    tmp = Path(args.tmp) if args.tmp else Path(tempfile.mkdtemp(prefix="srt-smoke-"))
    tmp.mkdir(parents=True, exist_ok=True)
    isolate(tmp)

    # 两条假 Key —— 全落临时库，绝不碰 api_keys/
    from app import deps
    from core.keys import KeyPool

    pool = KeyPool(deps.KEYS_DB)
    pool.add_keys([("FAKE-KEY-0001", "smoke-1"), ("FAKE-KEY-0002", "smoke-2")])
    deps.get_pool.cache_clear()

    # 关掉退避等待，让冒烟跑得快一点（RETRY_WAIT 只影响失败重试，成功路径不受影响）
    FakeEngine(delay=args.delay).install()

    import uvicorn
    from app.main import create_app

    uvicorn.run(
        create_app(), host="127.0.0.1", port=args.port, log_level="warning"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
