"""查看任务库里每条调用日志 / 导出某批次的完整请求与响应。

日志是全量落的（完整 system_instruction + 每一轮 contents + SDK 原始响应），
单批请求动辄几十 KB，界面上看不全 —— 这个脚本直接从任务库里读出来。

    .venv\\Scripts\\python.exe .tests/logpeek.py <任务库路径 或 log 下的任务名>
    .venv\\Scripts\\python.exe .tests/logpeek.py LawE05 --batch 3
    .venv\\Scripts\\python.exe .tests/logpeek.py LawE05 --batch 3 --out batch3.json

只读，不改任务库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import jobs as J  # noqa: E402

LOG_DIR = Path(__file__).resolve().parents[1] / "log"


def resolve(name: str) -> Path:
    p = Path(name)
    if p.is_file():
        return p
    if p.suffix != ".db":
        p = p.with_suffix(".db")
    cand = LOG_DIR / p.name
    if cand.is_file():
        return cand
    hits = sorted(LOG_DIR.glob(f"*{name}*.db"))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(f"找不到任务：{name}（log/ 下没有匹配的 .db）")
    raise SystemExit(
        f"匹配到 {len(hits)} 个任务，请写全一点：\n  "
        + "\n  ".join(h.name for h in hits[:10])
    )


def kb(n) -> str:
    try:
        return f"{len(json.dumps(n, ensure_ascii=False)) / 1024:.1f} KB"
    except Exception:
        return "?"


def main() -> int:
    ap = argparse.ArgumentParser(description="查看任务库的调用日志")
    ap.add_argument("job", help="任务库路径，或 log/ 下的任务名（可只写一部分）")
    ap.add_argument("--batch", type=int, help="只看某一批")
    ap.add_argument("--out", help="把该批次的完整 JSON 导出到这个文件")
    args = ap.parse_args()

    store = J.JobStore(resolve(args.job))
    print(f"任务库：{store.db_path.name}")
    print(f"字幕：{store.count_subtitles()} 条   状态：{store.status}")
    print(f"进度：{store.progress(int(store.get_param(J.PARAM_TOTAL_BATCHES, 0) or 0))}")

    entries = store.api_log_entries()
    if args.batch:
        entries = [e for e in entries if e.get("batch_index") == args.batch]
        if not entries:
            print(f"第 {args.batch} 批没有日志记录。")
            return 1

    print(f"\n共 {len(entries)} 条日志：")
    print(f"{'ID':>5}  {'批次':>4}  {'状态':<20}  {'请求':>9}  {'响应':>9}")
    for e in entries:
        print(f"{e['id']:>5}  {e.get('batch_index', '-')!s:>4}  "
              f"{str(e.get('status', '')):<20}  "
              f"{kb(e.get('request_payload')):>9}  {kb(e.get('response_payload')):>9}")

    if not args.batch:
        print("\n想看某一批的完整内容：加 --batch N；想导出：再加 --out 文件名")
        return 0

    picked = max(entries, key=lambda e: e["id"])
    detail = store.api_log_detail(picked["id"]) or {}
    print(f"\n第 {args.batch} 批（日志 #{picked['id']}，状态 {picked.get('status')}）：")

    req = detail.get("request_payload") or {}
    print("  请求顶层：", list(req.keys()))
    cfg = req.get("generate_config") or {}
    print("  生成参数：", {k: v for k, v in cfg.items() if k != "response_schema"})
    for i, c in enumerate(req.get("contents") or []):
        text = ((c.get("parts") or [{}])[0].get("text") or "")
        head = text.strip().splitlines()[0][:60] if text.strip() else ""
        print(f"  第 {i + 1} 轮（{c.get('role')}）{len(text)} 字符  {head}")

    resp = detail.get("response_payload") or {}
    print("  响应顶层：", list(resp.keys()))

    if args.out:
        out = Path(args.out)
        out.write_text(
            json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已导出：{out}（{out.stat().st_size / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
