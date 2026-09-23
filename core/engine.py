"""Gemini 调用层：google-genai 官方 SDK 原生调用。

坚持官方路线（不用 OpenAI 兼容接口）：
- `client.models.generate_content(model, contents, config=GenerateContentConfig)`
- 强结构输出用 Gemini 专属的 `response_mime_type="application/json"`
  + `response_schema=genai.types.Schema`

对外只有这一个入口 `generate_json()`，返回结构化结果（文本 + 原始响应 dump，供日志落库）。
没有类、没有可变状态：每次调用各自建 client，Key 轮换时才不会串味。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.models import needs_thinking_config

# JSON Schema 的类型名 -> Gemini 自己的类型名（STRING/INTEGER/...）
_GEMINI_TYPES = {
    "object": "OBJECT",
    "array": "ARRAY",
    "string": "STRING",
    "str": "STRING",
    "integer": "INTEGER",
    "int": "INTEGER",
    "number": "NUMBER",
    "float": "NUMBER",
    "double": "NUMBER",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "null": "NULL",
}


@dataclass(frozen=True)
class Generation:
    """一次调用的结果。

    text 给业务用；raw 是 SDK 响应的整体 dump（含 candidates / usage_metadata），
    供 api_logs 全量落库 —— 日志量级对齐旧脚本的 response.model_dump()。
    """

    text: str
    raw: dict | None


def make_serializable(obj):
    """递归清洗：bytes -> hex，保证整体能 JSON 序列化。"""
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_serializable(i) for i in obj]
    if isinstance(obj, bytes):
        return obj.hex()
    return obj


def dump_response(obj) -> dict | None:
    """把 SDK 的响应对象整体 dump 成 dict。dump 不了返回 None（调用方回落到只记文本）。"""
    try:
        if hasattr(obj, "model_dump"):
            return make_serializable(obj.model_dump())
        if isinstance(obj, dict):
            return make_serializable(obj)
    except Exception:
        return None
    return None


def to_gemini_schema(spec: dict, types) -> Any:
    """把通用 JSON Schema dict 转成 Gemini 专属的 `genai.types.Schema`。

    两件事必须做足：
    1. **description 一定要带上** —— 这是给模型的字段说明，丢掉等于白少一层约束；
    2. 类型要逐个映射，不能一律退化成 STRING（integer / number / boolean 要用对的类型）；
       另外补上 property_ordering，让模型按我们声明的字段顺序输出。
    """
    kind = str(spec.get("type", "object")).lower()
    gemini_type = getattr(types.Type, _GEMINI_TYPES.get(kind, "STRING"))

    obj = types.Schema(type=gemini_type)
    if spec.get("title"):
        obj.title = spec["title"]
    if spec.get("description"):
        obj.description = spec["description"]
    if spec.get("enum"):
        obj.enum = list(spec["enum"])

    if gemini_type == types.Type.OBJECT:
        props = spec.get("properties") or {}
        obj.properties = {k: to_gemini_schema(v, types) for k, v in props.items()}
        if spec.get("required"):
            obj.required = list(spec["required"])
        if props:
            obj.property_ordering = list(props.keys())
        return obj

    if gemini_type == types.Type.ARRAY:
        items = spec.get("items")
        if items:
            obj.items = to_gemini_schema(items, types)
        return obj

    return obj


def generate_json(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    turns: list[dict],
    schema: dict | None = None,
    temperature: float = 0.7,
    top_p: float = 0.95,
    max_output_tokens: int = 65536,
    thinking_level: str | None = None,
) -> Generation:
    """调一次 Gemini，返回 JSON 文本（强结构输出）。

    turns = [{'role': 'user'|'model', 'text': str}]，按顺序作为多轮 contents。
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    contents = [
        types.Content(
            role=t.get("role", "user"),
            parts=[types.Part.from_text(text=t.get("text", ""))],
        )
        for t in turns
    ]

    kwargs: dict[str, Any] = dict(
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
        system_instruction=[types.Part.from_text(text=system_prompt)],
    )
    if schema is not None:
        kwargs["response_schema"] = to_gemini_schema(schema, types)
    if needs_thinking_config(thinking_level):
        kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=str(thinking_level).upper()
        )

    resp = client.models.generate_content(
        model=model, contents=contents, config=types.GenerateContentConfig(**kwargs)
    )
    return Generation(text=resp.text or "", raw=dump_response(resp))
