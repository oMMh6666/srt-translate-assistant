"""上下文组装：固定策略 = 完整全部字幕（整体参考）+ 上一批次译文（最近参考）。

固定为：
  <full_context>         整部字幕原文，供模型理解整体剧情与语境
  <previous_translation> 上一批次的译文，保证术语 / 人称 / 语气跨批一致
"""

from __future__ import annotations


def format_lines(subtitles: list[dict]) -> str:
    """[id] text 形式的纯文本上下文。"""
    return "\n".join(f"[{s['id']}] {s['text']}" for s in subtitles)


def full_context(subtitles: list[dict]) -> str:
    """整体参考：完整全部字幕原文。"""
    return format_lines(subtitles)


def estimate_tokens(text: str) -> int:
    """粗估 token 数（中英混排取 ~1.5 字符/token）。"""
    if not text:
        return 0
    return int(len(text) / 1.5)
