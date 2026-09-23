"""全局提示词模板管理（prompts/ 目录）。

任务自带的提示词存在任务库的 prompt_files 表里（随任务走），
这里的模板只用于「新建任务时选一份」与「把改好的提示词另存为模板」。
"""

from __future__ import annotations

import time
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
BUILTIN_TEMPLATES = ("reflect.md", "custom_prompt.md")


def list_prompt_files() -> list[str]:
    if not PROMPTS_DIR.exists():
        return []
    return sorted(p.name for p in PROMPTS_DIR.glob("*.md") if p.is_file())


def read_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def write_prompt(name: str, content: str) -> Path:
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    p = PROMPTS_DIR / name
    p.write_text(content, encoding="utf-8", newline="\n")
    return p


def delete_prompt(name: str) -> bool:
    """删除模板：移进 prompts/.trash（而不是硬删除，误删可找回）。

    内置的两个模板由调用方拦截，这里只管搬移。
    """
    p = PROMPTS_DIR / name
    if not p.exists():
        return False
    trash = PROMPTS_DIR / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / name
    if target.exists():
        target = trash / f"{p.stem}_{int(time.time())}{p.suffix}"
    p.replace(target)
    return True


def render_system_prompt(
    reflect_text: str, custom_text: str, target_language: str
) -> str:
    """渲染 reflect.md 中的 ${target_language} 与 ${custom_prompt} 占位符。"""
    out = (reflect_text or "").replace("${target_language}", target_language or "")
    return out.replace("${custom_prompt}", custom_text or "")
