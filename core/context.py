"""上下文组装：固定策略 = 完整全部字幕（整体参考）+ 上一批次译文（最近参考）。

固定为：
  <full_context>         整部字幕原文，供模型理解整体剧情与语境
  <previous_translation> 上一批次的译文，保证术语 / 人称 / 语气跨批一致
  <current_task>         本批要翻的字幕

这一层只产出「要发给模型的 turns」，不关心怎么发、发失败怎么办。
"""

from __future__ import annotations

import json

PREV_NONE = "无（本批次为第一批）"

# 反复漏句时纠错轮次会不断累积，请求越来越大、越来越慢，看起来像卡死。
# 超过这个段数就砍掉中间的部分，只保留最初的上下文 + 最近的几轮纠错。
MAX_TURNS = 9
KEEP_HEAD = 3
KEEP_TAIL = 4


def format_lines(subtitles: list[dict]) -> str:
    """[id] text 形式的纯文本上下文。"""
    return "\n".join(f"[{s['id']}] {s['text']}" for s in subtitles)


def full_context(subtitles: list[dict]) -> str:
    """整体参考：完整全部字幕原文。"""
    return format_lines(subtitles)


def prev_context(prev_map: dict | None) -> str:
    """上一批次译文 -> 上下文文本；没有就给一句明确的「无」。"""
    if not prev_map:
        return PREV_NONE
    items = [{"id": str(k), "native_translation": v} for k, v in prev_map.items()]
    return json.dumps(items, ensure_ascii=False, indent=2)


def build_turns(
    full_ctx: str, prev_ctx: str, batch: dict[str, str]
) -> list[dict]:
    """本批次的初始三轮：全文上下文 / 上一批译文 / 本批任务。"""
    return [
        {
            "role": "user",
            "text": (
                "<full_context>\n以下是整部视频的完整字幕原文，"
                "仅供你理解整体视频背景、上下文语境：\n"
                f"{full_ctx}\n</full_context>"
            ),
        },
        {
            "role": "user",
            "text": (
                "<previous_translation_context>\n以下是上一批次的翻译结果，"
                "仅供参考（用于保持术语统一、人称一致及连贯语气）：\n"
                f"{prev_ctx}\n</previous_translation_context>"
            ),
        },
        {
            "role": "user",
            "text": (
                "<current_task>\n请严格对以下指定范围的字幕条目进行"
                "**直译+反思+润色终译**。"
                f"必须且仅返回本次任务 {len(batch)} 条 ID 的 JSON 结果：\n"
                f"{json.dumps(batch, ensure_ascii=False, indent=2)}\n</current_task>"
            ),
        },
    ]


def missing_feedback(missing: list[str], raw: str) -> list[dict]:
    """漏句纠错的一轮：把模型的原文回给它，再点名缺了哪些 ID。"""
    return [
        {"role": "model", "text": raw},
        {
            "role": "user",
            "text": (
                f"错误：你返回的 JSON 缺失了以下 ID 的字幕条目：{missing}。"
                "请绝对保证输入的每一个 ID 都有对应的 JSON 节点，严禁擅自合并或省略！"
            ),
        },
    ]


def trim_turns(turns: list[dict]) -> list[dict]:
    """纠错轮次过长时截断：保留开头几轮上下文 + 最近几轮纠错。"""
    if len(turns) <= MAX_TURNS:
        return turns
    return turns[:KEEP_HEAD] + turns[-KEEP_TAIL:]
