"""提示词文件管理（prompts/ 目录）。"""

from __future__ import annotations

import time
from pathlib import Path

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def prompts_dir(root: str | Path | None = None) -> Path:
    return Path(root) if root else DEFAULT_PROMPTS_DIR


def list_prompt_files(root: str | Path | None = None) -> list[str]:
    d = prompts_dir(root)
    if not d.exists():
        return []
    return sorted(p.name for p in d.glob("*.md") if p.is_file())


def read_prompt(name: str, root: str | Path | None = None) -> str:
    return (prompts_dir(root) / name).read_text(encoding="utf-8")


def write_prompt(
    name: str, content: str, root: str | Path | None = None
) -> Path:
    d = prompts_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content, encoding="utf-8", newline="\n")
    return p


def delete_prompt(name: str, root: str | Path | None = None) -> bool:
    """删除模板：移进 prompts/.trash（而不是硬删除，误删可找回）。"""
    d = prompts_dir(root)
    p = d / name
    if not p.exists():
        return False
    trash = d / ".trash"
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
