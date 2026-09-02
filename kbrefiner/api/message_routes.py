"""消息模块 API 路由。

管理后台（/api/admin/messages，需管理员）：
- GET    /api/admin/messages            列表（类型/状态筛选 + 标题搜索）
- POST   /api/admin/messages            创建（草稿 / 立即发送 / 定时）
- GET    /api/admin/messages/{id}       详情
- PATCH  /api/admin/messages/{id}       编辑（仅草稿）
- POST   /api/admin/messages/{id}/send  立即发送（草稿/定时 → 已发送）
- POST   /api/admin/messages/{id}/revoke    撤回（已发送 → 已撤回，用户端不再展示）
- POST   /api/admin/messages/{id}/cancel    取消定时（定时 → 草稿）
- DELETE /api/admin/messages/{id}       删除

用户端（/api/messages，开放模式匿名可看全体消息）：
- GET /api/messages?limit=10  当前用户可见的已发送消息（最新在前）
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from kbrefiner.db import AuditStore, MessageStore, UserStore

from .deps import (
    get_audit_store,
    get_current_user,
    get_message_store,
    get_user_store,
    require_admin,
)

router = APIRouter(prefix="/api", tags=["消息"])

_TYPE_LABELS = {"system": "系统通知", "task": "任务通知", "maintenance": "维护公告", "security": "安全告警"}


class MessageCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=5000)
    type: str = "system"
    # send_now=true 立即发送；否则 status 决定草稿/定时
    send_now: bool = False
    scheduled_at: Optional[float] = None
    target_users: Any = "all"  # "all" 或 user_id 列表


class MessageUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    content: Optional[str] = Field(default=None, max_length=5000)
    type: Optional[str] = None
    target_users: Any = None


def _resolve_target(raw: Any, user_store: UserStore) -> str:
    """校验目标用户："all" 或存在的 user_id 列表 → 存储字符串。"""
    import json

    if raw is None or raw == "all":
        return "all"
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="target_users 须为 \"all\" 或用户 ID 列表")
    for uid in raw:
        if not user_store.get(str(uid)):
            raise HTTPException(status_code=400, detail=f"目标用户不存在: {uid}")
    return json.dumps([str(u) for u in raw], ensure_ascii=False)


@router.get("/admin/messages")
async def list_messages(
    type: str = "",
    status: str = "",
    search: str = "",
    admin: dict = Depends(require_admin),
    message_store: MessageStore = Depends(get_message_store),
    user_store: UserStore = Depends(get_user_store),
):
    """管理视角消息列表（含草稿与已撤回），附目标用户展示信息。"""
    if type and type not in _TYPE_LABELS:
        raise HTTPException(status_code=400, detail=f"非法消息类型: {type}")
    messages = message_store.list_all(msg_type=type, status=status, search=search)

    user_cache: dict[str, dict] = {}
    for m in messages:
        targets = m["target_users"]
        if targets == "all":
            m["target_display"] = "全部用户"
        elif isinstance(targets, list):
            names = []
            for uid in targets:
                if uid not in user_cache:
                    user_cache[uid] = user_store.get(uid) or {}
                names.append(user_cache[uid].get("username") or uid)
            m["target_display"] = "、".join(names)
        else:
            m["target_display"] = str(targets)
    return {"messages": messages, "total": len(messages)}


@router.post("/admin/messages", status_code=201)
async def create_message(
    body: MessageCreateRequest,
    request: Request,
    admin: dict = Depends(require_admin),
    message_store: MessageStore = Depends(get_message_store),
    user_store: UserStore = Depends(get_user_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """创建消息：send_now 立即发送；scheduled_at 定时；否则存草稿。"""
    if body.type not in _TYPE_LABELS:
        raise HTTPException(status_code=400, detail=f"非法消息类型: {body.type}")

    target = _resolve_target(body.target_users, user_store)
    if body.send_now:
        status = "sent"
    elif body.scheduled_at is not None:
        if body.scheduled_at <= time.time():
            raise HTTPException(status_code=400, detail="定时时间必须晚于当前时间")
        status = "scheduled"
    else:
        status = "draft"

    try:
        msg = message_store.create(
            title=body.title.strip(), content=body.content, msg_type=body.type,
            status=status, target_users=target,
            scheduled_at=body.scheduled_at, created_by=admin.get("id", ""),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        audit_store.record(
            user=admin, action="message.created", target_type="message",
            target_id=msg["id"], detail=f"type:{body.type};status:{status}",
            request=request,
        )
    except Exception:
        pass
    return msg


@router.get("/admin/messages/{message_id}")
async def get_message(
    message_id: str,
    _admin: dict = Depends(require_admin),
    message_store: MessageStore = Depends(get_message_store),
):
    msg = message_store.get(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail=f"消息 {message_id} 不存在")
    return msg


@router.patch("/admin/messages/{message_id}")
async def update_message(
    message_id: str,
    body: MessageUpdateRequest,
    request: Request,
    admin: dict = Depends(require_admin),
    message_store: MessageStore = Depends(get_message_store),
    user_store: UserStore = Depends(get_user_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """编辑消息（仅草稿可改）。"""
    msg = message_store.get(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail=f"消息 {message_id} 不存在")
    if msg["status"] != "draft":
        raise HTTPException(status_code=400, detail="仅草稿状态可编辑")
    if body.type is not None and body.type not in _TYPE_LABELS:
        raise HTTPException(status_code=400, detail=f"非法消息类型: {body.type}")

    target = _resolve_target(body.target_users, user_store) if body.target_users is not None else None
    message_store.update(
        message_id, title=body.title, content=body.content,
        msg_type=body.type, target_users=target,
    )
    try:
        audit_store.record(
            user=admin, action="message.updated", target_type="message",
            target_id=message_id, request=request,
        )
    except Exception:
        pass
    return message_store.get(message_id)


@router.post("/admin/messages/{message_id}/send")
async def send_message(
    message_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
    message_store: MessageStore = Depends(get_message_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """立即发送（草稿/已定时 → 已发送）。"""
    msg = message_store.get(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail=f"消息 {message_id} 不存在")
    if msg["status"] == "sent":
        raise HTTPException(status_code=400, detail="消息已发送")
    if msg["status"] == "revoked":
        raise HTTPException(status_code=400, detail="已撤回的消息不能再次发送")
    message_store.set_status(message_id, "sent")
    try:
        audit_store.record(
            user=admin, action="message.sent", target_type="message",
            target_id=message_id, request=request,
        )
    except Exception:
        pass
    return {"ok": True, "message": "已发送"}


@router.post("/admin/messages/{message_id}/revoke")
async def revoke_message(
    message_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
    message_store: MessageStore = Depends(get_message_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """撤回已发送消息（用户端不再展示）。"""
    msg = message_store.get(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail=f"消息 {message_id} 不存在")
    if msg["status"] != "sent":
        raise HTTPException(status_code=400, detail="仅已发送的消息可撤回")
    message_store.set_status(message_id, "revoked")
    try:
        audit_store.record(
            user=admin, action="message.revoked", target_type="message",
            target_id=message_id, request=request,
        )
    except Exception:
        pass
    return {"ok": True, "message": "已撤回"}


@router.post("/admin/messages/{message_id}/cancel")
async def cancel_schedule(
    message_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
    message_store: MessageStore = Depends(get_message_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """取消定时 → 转为草稿。"""
    msg = message_store.get(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail=f"消息 {message_id} 不存在")
    if msg["status"] != "scheduled":
        raise HTTPException(status_code=400, detail="仅定时消息可取消定时")
    message_store.set_status(message_id, "draft")
    try:
        audit_store.record(
            user=admin, action="message.schedule_cancelled", target_type="message",
            target_id=message_id, request=request,
        )
    except Exception:
        pass
    return {"ok": True, "message": "已取消定时，转为草稿"}


@router.delete("/admin/messages/{message_id}")
async def delete_message(
    message_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
    message_store: MessageStore = Depends(get_message_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """删除消息（不限状态）。"""
    msg = message_store.get(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail=f"消息 {message_id} 不存在")
    message_store.delete(message_id)
    try:
        audit_store.record(
            user=admin, action="message.deleted", target_type="message",
            target_id=message_id, request=request,
        )
    except Exception:
        pass
    return {"ok": True, "message": "已删除"}


# =====================================================================
# 用户端消息
# =====================================================================

@router.get("/messages")
async def my_messages(
    limit: int = 20,
    user: Optional[dict] = Depends(get_current_user),
    message_store: MessageStore = Depends(get_message_store),
):
    """当前用户可见的已发送消息（面向全体或点名本人），最新在前。

    开放模式未登录（匿名）：仅返回面向全体的消息。
    """
    uid = user["id"] if user else "__anonymous__"
    messages = message_store.list_for_user(uid, limit=max(1, min(limit, 50)))
    return {
        "messages": [
            {
                "id": m["id"],
                "title": m["title"],
                "content": m["content"],
                "type": m["type"],
                "type_label": _TYPE_LABELS.get(m["type"], m["type"]),
                "sent_at": m["sent_at"],
            }
            for m in messages
        ],
        "total": len(messages),
    }
