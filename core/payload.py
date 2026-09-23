"""api_logs 的请求 / 响应封包。

日志要跟旧脚本一个量级：完整 system_instruction + 生成参数 + 每一轮 contents（全文上下文、
上一批译文、本批任务、纠错轮次），响应侧带上 SDK 原始响应。
只有超大（> 2MB）才截断并打标记 —— 正常一部剧的单批请求也就几十 KB。
"""

from __future__ import annotations

import json

# 单条日志的封顶字符数（2MB）。这里只是防止异常输入把 db 撑爆。
LOG_MAX_CHARS = 2_000_000
TRUNCATED_FLAG = "__truncated__"


def request_payload(
    *,
    model: str,
    system_prompt: str,
    turns: list[dict],
    schema: dict,
    temperature: float,
    top_p: float,
    max_output_tokens: int,
    thinking_level: str | None = None,
) -> dict:
    """构建**全量**请求包（与真正发出去的内容一致）。"""
    return {
        "model": model,
        "system_instruction": system_prompt,
        "generate_config": {
            "temperature": temperature,
            "top_p": top_p,
            "max_output_tokens": max_output_tokens,
            "response_mime_type": "application/json",
            "response_schema": schema,
            "thinking_config": (
                {"thinking_level": thinking_level} if thinking_level else None
            ),
        },
        "contents": [
            {
                "role": t.get("role", "user"),
                "parts": [{"text": t.get("text", "")}],
            }
            for t in turns
        ],
    }


def response_payload(
    text: str, raw: dict | None = None, extra: dict | None = None
) -> dict:
    """完整响应包：SDK 原始响应 + 文本 + 解析/错误信息。"""
    payload: dict = {"text": text}
    if raw is not None:
        payload["raw_response"] = raw
    if extra:
        payload.update(extra)
    return payload


def error_payload(exc: BaseException, classified) -> dict:
    """失败时的响应包（不记原始响应，记错误分类与详情）。"""
    return {
        "error_type": type(exc).__name__,
        "error": classified.message,
        "error_detail": str(exc)[:4000],
    }


def cap_payload(payload: dict) -> dict:
    """超过封顶才截断，并在包里留标记。"""
    try:
        size = len(json.dumps(payload, ensure_ascii=False))
    except Exception:
        return payload
    if size <= LOG_MAX_CHARS:
        return payload

    out = dict(payload)
    out["contents"] = [
        {
            "role": c.get("role", "user"),
            "parts": [
                {
                    "text": (
                        p.get("text", "")[: LOG_MAX_CHARS // 4]
                        + "\n…[日志封顶，已截断]"
                    )
                }
                for p in c.get("parts", [])
            ],
        }
        for c in payload.get("contents", [])
    ]
    out[TRUNCATED_FLAG] = True
    return out
