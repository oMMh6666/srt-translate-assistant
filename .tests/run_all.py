"""一键跑全部冒烟。

    .venv\\Scripts\\python.exe .tests/run_all.py

四个脚本各自自带起停服务、全部落临时目录，不碰真实 Key 与真实任务。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent

SUITES = [
    ("smoke_refactor.py", "结构 / 分层 / 死代码"),
    ("smoke_pipeline.py", "流水线 / SSE 断线重连"),
    ("smoke_ui.py", "界面（真服务 + 无头 Chrome）"),
    ("smoke_progress.py", "进度条专项"),
]


def main() -> int:
    results: list[tuple[str, int, float]] = []
    for name, desc in SUITES:
        print(f"\n{'=' * 64}\n▶ {name}  —— {desc}\n{'=' * 64}")
        t0 = time.time()
        p = subprocess.run(
            [sys.executable, str(HERE / name)], cwd=str(ROOT), text=True
        )
        results.append((name, p.returncode, time.time() - t0))

    print(f"\n{'=' * 64}\n汇总\n{'=' * 64}")
    bad = 0
    for name, code, secs in results:
        mark = "通过" if code == 0 else "失败"
        print(f"  {mark}  {name:<20} {secs:5.1f}s")
        bad += 0 if code == 0 else 1

    print(f"\n{len(results) - bad}/{len(results)} 个冒烟套件通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
