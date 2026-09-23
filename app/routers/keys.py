"""API Key 接口：列表 / 批量新增 / 行内编辑 / 勾选批量操作。

重复校验这类「请求合法性」判断放在接口层（返回 400），
KeyPool 只管持久化和调度。
"""

from __future__ import annotations

from typing import Iterable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import deps
from core.keys import KeyPool

router = APIRouter(prefix="/api/keys")


@router.get("")
def list_keys():
    pool = deps.get_pool()
    return {"keys": pool.list_keys(), "stats": pool.stats()}


class KeyIn(BaseModel):
    api_key: str
    project_name: str = ""


@router.post("")
def add_keys(items: list[KeyIn]):
    """批量新增 Key；重复的直接跳过并回报，避免「点了没反应」。"""
    pool = deps.get_pool()
    existing = pool.list_keys()
    used_keys = {k["api_key"] for k in existing}
    used_names = {k["project_name"] for k in existing}

    fresh: list[tuple[str, str]] = []
    skipped: list[str] = []
    for item in items:
        key = (item.api_key or "").strip()
        name = (item.project_name or "").strip()
        if not key:
            skipped.append("空的 Key，已跳过")
            continue
        if key in used_keys:
            skipped.append(f"Key 重复：{key[:8]}…")
            continue
        if name and name in used_names:
            skipped.append(f"项目名重复：{name}")
            continue
        if not name:
            name = f"key-{len(existing) + len(fresh) + 1}"

        fresh.append((key, name))
        used_keys.add(key)
        used_names.add(name)

    if fresh:
        pool.add_keys(fresh)
    return {
        "ok": True,
        "added": len(fresh),
        "skipped": skipped,
        "stats": pool.stats(),
    }


class KeyUpdate(BaseModel):
    api_key: str | None = None
    project_name: str | None = None
    is_active: bool | None = None


@router.put("/{key_id}")
def update_key(key_id: int, req: KeyUpdate):
    """行内编辑：Key 与项目名可单独改，也可以只切启用状态。"""
    pool = deps.get_pool()
    existing = {k["id"]: k for k in pool.list_keys()}
    cur = existing.get(key_id)
    if not cur:
        raise HTTPException(404, f"找不到 Key #{key_id}")

    new_key = (req.api_key or cur["api_key"]).strip()
    new_name = (req.project_name or cur["project_name"]).strip()
    if req.api_key is not None and not new_key:
        raise HTTPException(400, "API Key 不能为空")
    if req.project_name is not None and not new_name:
        raise HTTPException(400, "项目名不能为空")

    if new_key != cur["api_key"] and new_key in {k["api_key"] for k in existing.values()}:
        raise HTTPException(400, f"已存在相同的 Key：{new_key[:8]}…")
    if new_name != cur["project_name"] and new_name in {
        k["project_name"] for k in existing.values()
    }:
        raise HTTPException(400, f"项目名重复：{new_name}")

    pool.update_key(key_id, new_key, new_name)
    if req.is_active is True:
        pool.enable(key_id)
    elif req.is_active is False:
        pool.disable(key_id)
    return {"ok": True, "stats": pool.stats()}


class BulkKeyAction(BaseModel):
    ids: list[int]
    action: str


BULK_ACTIONS = ("enable", "disable", "delete", "reset_usage")


@router.post("/bulk")
def bulk_keys(req: BulkKeyAction):
    """勾选式批量操作：启用 / 禁用 / 删除 / 重置用量。

    删除单行也走这里（前端的「删除」按钮 = 只勾选这一行），
    所以没有单独的 DELETE /api/keys/{id}。
    """
    pool = deps.get_pool()
    ids = [int(i) for i in req.ids if int(i) > 0]
    if not ids:
        raise HTTPException(400, "请先勾选要操作的 Key")

    action = (req.action or "").strip()
    if action not in BULK_ACTIONS:
        raise HTTPException(400, f"未知操作：{action}")

    affected = 0
    for i in ids:
        try:
            _apply(pool, action, i)
            affected += 1
        except Exception as e:  # noqa: BLE001 - 单行失败不该拖垮整批
            deps.record_error(f"bulk key action failed: {action} #{i}", e)
    return {"ok": True, "affected": affected, "stats": pool.stats()}


def _apply(pool: KeyPool, action: str, key_id: int) -> None:
    if action == "enable":
        pool.enable(key_id)
    elif action == "disable":
        pool.disable(key_id)
    elif action == "delete":
        pool.delete_key(key_id)
    elif action == "reset_usage":
        pool.reset_usage([key_id])
