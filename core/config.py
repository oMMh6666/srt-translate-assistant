"""全局默认配置（唯一来源）。

以前这里还会试图从外部配置文件读一份覆盖值，实际上项目根本没用过那个文件，
配置全部以下面的 DEFAULT_CONFIG 为准 —— 要改默认值直接改本文件即可。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PORT = 8777
SRT_DEFAULT_DIR = r"C:\Gemini"

DEFAULT_CONFIG: dict = {
    "ENGINE": "gemini",
    "MODEL_NAME": "gemini-3.5-flash-lite",
    "TEMPERATURE": 0.7,
    "TOP_P": 0.95,
    "MAX_OUTPUT_TOKENS": 65536,
    "BATCH_SIZE": 20,
    "TARGET_LANGUAGE": "简体中文",
    "THINKING_LEVEL": "",  # 留空 = 由模型自动决定
    "RETRY_WAIT": 2,
    # 没有可用 Key 时任务直接停止（不再挂起等待），到 Key 管理补充后点「继续」即可。
    # 单次请求的等待上限（秒）。超时视为服务端抖动，换 Key 重试 ——
    # 避免 SDK 卡死时整个任务「假静止」（日志里什么都看不到）。
    "REQUEST_TIMEOUT": 600,
}

TARGET_LANGUAGES = ["简体中文", "繁體中文", "English", "日本語", "한국어"]


def default_config() -> dict:
    """取一份默认配置的副本（调用方随便改，不会污染全局）。"""
    return dict(DEFAULT_CONFIG)
