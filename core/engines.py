"""翻译引擎抽象层。

把「调谁家的模型」和「翻译流程」解耦：
Translator 只依赖 BaseEngine.generate_json(...)，新增引擎只要实现这一个方法。
"""

from __future__ import annotations

from typing import Any


def make_serializable(obj):
    """递归清洗：bytes -> hex，保证整体能 JSON 序列化（跟 legacy/utils 一致）。"""
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_serializable(i) for i in obj]
    if isinstance(obj, bytes):
        return obj.hex()
    return obj


def dump_response(obj) -> dict | None:
    """把 SDK 的响应对象整体 dump 成 dict（含 candidates / usage_metadata）。

    日志要跟旧脚本一样是「完整响应」，所以不能只留 resp.text。
    dump 不了就返回 None，调用方回落到只记文本。
    """
    try:
        if hasattr(obj, "model_dump"):
            return make_serializable(obj.model_dump())
        if isinstance(obj, dict):
            return make_serializable(obj)
    except Exception:
        return None
    return None


class BaseEngine:
    name = "base"
    label = "Base"
    # 该引擎是否需要 API Key（本地模型可为 False）
    needs_key = True
    # 最近一次调用的原始响应（整体 dump）。Translator 用它写完整日志。
    last_raw_response: dict | None = None

    def generate_json(
        self,
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
    ) -> str:
        """返回模型输出的 JSON 文本。turns = [{'role': 'user'|'model', 'text': str}]"""
        raise NotImplementedError


# --------------------------------------------------------------------- Gemini


# Gemini 的类型名是自己的一套（STRING/INTEGER/...），这里把 JSON Schema 的类型名映射过去
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


def to_gemini_schema(spec: dict, types) -> Any:
    """把通用 JSON Schema dict 转成 Gemini 专属的 `genai.types.Schema`。

    Gemini 的强结构输出（response_schema + response_mime_type=application/json）
    只认自己的 Schema 对象，不能把 OpenAI 那套 JSON Schema dict 直接丢过去。

    两件事必须做足：
    1. **description 一定要带上** —— 这是给模型的字段说明，丢掉等于白少一层约束，
       也就是老脚本里每个字段都写 description 的原因；
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


# 旧名保留兼容
_to_gemini_schema = to_gemini_schema


class GeminiEngine(BaseEngine):
    name = "gemini"
    label = "Google Gemini"

    def generate_json(self, *, api_key, model, system_prompt, turns, schema=None,
                      temperature=0.7, top_p=0.95, max_output_tokens=65536,
                      thinking_level=None) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        contents = [
            types.Content(role=t.get("role", "user"),
                          parts=[types.Part.from_text(text=t.get("text", ""))])
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
            # Gemini 强结构：Google 官方 SDK 的 types.Schema + JSON mime
            kwargs["response_schema"] = to_gemini_schema(schema, types)
        if thinking_level and str(thinking_level).upper() != "OFF":
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=str(thinking_level).upper()
            )

        resp = client.models.generate_content(
            model=model, contents=contents, config=types.GenerateContentConfig(**kwargs)
        )
        self.last_raw_response = dump_response(resp)  # 完整响应：candidates / usage_metadata
        return resp.text or ""


# ------------------------------------------- OpenAI 兼容接口（含各家国产模型）


class OpenAICompatibleEngine(BaseEngine):
    """OpenAI / DeepSeek / 通义 / Moonshot 等 chat.completions 兼容接口。"""

    name = "openai"
    label = "OpenAI 兼容接口"

    def __init__(self, base_url: str = "https://api.openai.com/v1"):
        self.base_url = base_url

    def generate_json(self, *, api_key, model, system_prompt, turns, schema=None,
                      temperature=0.7, top_p=0.95, max_output_tokens=65536,
                      thinking_level=None) -> str:
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "使用 OpenAI 兼容引擎需要安装 openai：pip install openai"
            ) from e

        client = OpenAI(api_key=api_key, base_url=self.base_url)
        messages = [{"role": "system", "content": system_prompt}]
        for t in turns:
            role = "assistant" if t.get("role") == "model" else "user"
            messages.append({"role": role, "content": t.get("text", "")})

        kwargs: dict[str, Any] = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
        )
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "subtitles", "schema": schema, "strict": False},
            }
        else:
            kwargs["response_format"] = {"type": "json_object"}

        resp = client.chat.completions.create(**kwargs)
        self.last_raw_response = dump_response(resp)
        return resp.choices[0].message.content or ""


ENGINES: dict[str, type[BaseEngine]] = {
    GeminiEngine.name: GeminiEngine,
    OpenAICompatibleEngine.name: OpenAICompatibleEngine,
}


def list_engines() -> list[dict]:
    return [
        {"name": cls.name, "label": cls.label, "needs_key": cls.needs_key}
        for cls in ENGINES.values()
    ]


def create_engine(name: str, **opts) -> BaseEngine:
    key = (name or "gemini").lower()
    if key not in ENGINES:
        raise ValueError(f"未知引擎：{name}，可选：{list(ENGINES)}")
    return ENGINES[key](**opts)
