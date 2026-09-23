"""模型与思考等级（thinking level）的对应关系。

历史坑位（见 History.md）：
- gemini-3.5 / 3.6 系列最低支持 MINIMAL
- gemini-3.7 / 3.8 系列最低只支持 LOW，传 MINIMAL 会直接报错
  「Thinking level MINIMAL is not supported for this model」

所以思考等级必须由模型决定，不能靠复制脚本来区分。
"""

from __future__ import annotations

# OFF 表示完全不发送 thinking_config（某些模型不接受该参数时的安全兜底）
THINKING_OFF = "OFF"

_MODEL_THINKING: dict[str, dict] = {
    "gemini-3.5-flash-lite": {
        "levels": ["OFF", "MINIMAL", "LOW", "MEDIUM", "HIGH"],
        "default": "MINIMAL",
    },
    "gemini-3.5-flash": {
        "levels": ["OFF", "MINIMAL", "LOW", "MEDIUM", "HIGH"],
        "default": "MINIMAL",
    },
    "gemini-3.6-flash": {
        "levels": ["OFF", "MINIMAL", "LOW", "MEDIUM", "HIGH"],
        "default": "MINIMAL",
    },
    "gemini-3.7-flash": {
        "levels": ["OFF", "LOW", "MEDIUM", "HIGH"],
        "default": "LOW",
    },
    "gemini-3.8-flash": {
        "levels": ["OFF", "LOW", "MEDIUM", "HIGH"],
        "default": "LOW",
    },
}

# 未知模型（用户手填 / 新模型）：给全量等级，默认 OFF 最稳
_FALLBACK_THINKING = {
    "levels": ["OFF", "MINIMAL", "LOW", "MEDIUM", "HIGH"],
    "default": "OFF",
}

KNOWN_MODELS = list(_MODEL_THINKING.keys())


def _spec(model: str) -> dict:
    return _MODEL_THINKING.get(model, _FALLBACK_THINKING)


def thinking_levels_for(model: str) -> list[str]:
    """该模型合法的思考等级列表。"""
    return _spec(model)["levels"]


def default_thinking_level(model: str) -> str:
    """该模型的默认思考等级。"""
    return _spec(model)["default"]


def normalize_thinking_level(model: str, level: str | None) -> str:
    """把任意输入收敛到该模型合法的等级；不合法则退回默认值。"""
    if not level:
        return default_thinking_level(model)
    level = str(level).strip().upper()
    if level in thinking_levels_for(model):
        return level
    return default_thinking_level(model)


def needs_thinking_config(level: str | None) -> bool:
    """OFF = 不发送 thinking_config。"""
    return bool(level) and str(level).upper() != THINKING_OFF
