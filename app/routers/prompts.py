"""全局提示词模板接口（prompts/ 目录）。

任务自带的提示词存在任务库里（走 /api/jobs/{id}/prompts），
这里只管「新建任务时选一份」和「把改好的另存为模板」。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import prompts as P

router = APIRouter(prefix="/api/prompts/templates")


@router.get("")
def list_templates():
    return {"templates": P.list_prompt_files(), "builtin": list(P.BUILTIN_TEMPLATES)}


class TemplateIn(BaseModel):
    name: str
    content: str


@router.post("")
def save_template(req: TemplateIn):
    name = req.name.strip()
    if not name.endswith(".md"):
        name += ".md"
    P.write_prompt(name, req.content)
    return {"ok": True, "name": name, "templates": P.list_prompt_files()}


@router.get("/{name}")
def read_template(name: str):
    try:
        return {"name": name, "content": P.read_prompt(name)}
    except Exception:
        raise HTTPException(404, f"模板不存在：{name}")


@router.delete("/{name}")
def delete_template(name: str):
    if name in P.BUILTIN_TEMPLATES:
        raise HTTPException(400, f"内置模板不可删除：{name}")
    if not P.delete_prompt(name):
        raise HTTPException(404, f"模板不存在：{name}")
    return {"ok": True, "templates": P.list_prompt_files()}
