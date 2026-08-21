"""认证与管理后台 API 路由。

前台认证（/api/auth）：
- POST /api/auth/login     邮箱+密码登录（成功设置 HttpOnly Cookie，并返回 token）
- POST /api/auth/logout    登出（清除 Cookie）
- GET  /api/auth/me        当前登录用户信息（开放模式未登录返回 user=null）

管理后台（/api/admin，需 super_admin / admin 角色）：
- GET    /api/admin/stats                       仪表盘统计（用户/任务/Token）
- GET    /api/admin/users                       用户列表（搜索/角色/状态筛选）
- POST   /api/admin/users                       创建用户
- GET    /api/admin/users/{user_id}             用户详情（含统计与最近任务）
- PATCH  /api/admin/users/{user_id}             更新用户（用户名/邮箱/角色/状态）
- POST   /api/admin/users/{user_id}/reset-password  重置密码
- DELETE /api/admin/users/{user_id}             删除用户
- GET    /api/admin/tasks                       全部用户任务列表（管理视角）

权限规则（PRD 第 11 节）：
1. 普通管理员（admin）不能查看/修改超级管理员（super_admin）账号。
2. super_admin 角色只能由 super_admin 分配。
3. 不能禁用/删除自己；不能禁用/删除最后一个 super_admin。
4. 用户列表响应绝不包含 password_hash。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from kbrefiner.auth import TOKEN_COOKIE, create_token
from kbrefiner.config import get_settings
from kbrefiner.db import TaskStore, UserStore

from .deps import get_current_user, get_task_store, get_user_store, require_admin

router = APIRouter(prefix="/api", tags=["认证与管理后台"])

# 合法角色与状态
_ROLES = ("super_admin", "admin", "user")
_STATUSES = ("active", "disabled")


# =====================================================================
# 请求体模型
# =====================================================================

class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=6, max_length=128)
    role: str = "user"
    status: str = "active"


class UserUpdateRequest(BaseModel):
    username: Optional[str] = Field(default=None, min_length=1, max_length=50)
    email: Optional[str] = Field(default=None, min_length=3, max_length=254)
    role: Optional[str] = None
    status: Optional[str] = None


class PasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=6, max_length=128)


# =====================================================================
# 内部工具
# =====================================================================

def _validate_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=400, detail=f"邮箱格式不正确: {email}")
    return email


def _can_operate(operator: dict, target: dict) -> None:
    """数据权限：普通管理员不能查看/修改超级管理员账号。"""
    if operator.get("role") != "super_admin" and target.get("role") == "super_admin":
        raise HTTPException(status_code=403, detail="普通管理员不能操作超级管理员账号")


def _super_admin_count(user_store: UserStore) -> int:
    return sum(1 for u in user_store.list_all() if u.get("role") == "super_admin")


def _guard_last_super_admin(
    operator: dict, target: dict, user_store: UserStore, *, removing: bool
) -> None:
    """禁止禁用/删除最后一个 super_admin（防止后台失去控制权）。"""
    if target.get("role") != "super_admin":
        return
    if operator.get("id") == target.get("id"):
        # 对自己：删除/禁用都禁止（既是"最后一个"也是"自己"保护）
        raise HTTPException(status_code=400, detail="不能对自己执行该操作")
    if _super_admin_count(user_store) <= 1:
        raise HTTPException(
            status_code=400,
            detail="系统至少需要保留一个超级管理员",
        )
    _ = removing  # 预留：删除/禁用同规则


# =====================================================================
# 前台认证
# =====================================================================

@router.post("/auth/login")
async def login(
    body: LoginRequest,
    response: Response,
    user_store: UserStore = Depends(get_user_store),
):
    """邮箱+密码登录。

    成功：设置 HttpOnly Cookie（浏览器页面用）并返回 token（API 客户端用）。
    失败：401（邮箱/密码错误或账号被禁用，不区分提示以防枚举）。
    """
    user = user_store.verify_login(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="邮箱或密码错误，或账号已被禁用")

    token = create_token(user["id"], user["role"])
    settings = get_settings()
    max_age = int(settings.auth_token_expire_hours * 3600)
    response.set_cookie(
        TOKEN_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
    )
    return {"token": token, "user": user}


@router.post("/auth/logout")
async def logout(response: Response):
    """登出：清除认证 Cookie。"""
    response.delete_cookie(TOKEN_COOKIE)
    return {"ok": True}


@router.get("/auth/me")
async def me(user: Optional[dict] = Depends(get_current_user)):
    """当前登录用户信息。

    开放模式（require_login=False）下未登录返回 user=null（前端据此
    显示"游客"）；强制登录模式下未登录由依赖抛 401。
    """
    return {"user": user}


# =====================================================================
# 管理后台：仪表盘统计
# =====================================================================

@router.get("/admin/stats")
async def admin_stats(
    trend_days: int = 7,
    _admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
    task_store: TaskStore = Depends(get_task_store),
):
    """仪表盘统计：用户概况、任务概况、Token 消耗、近 N 天趋势。

    trend_days 控制趋势窗口（默认 7 天，上限 90）；统计卡片始终为全量。
    """
    users = user_store.list_all()
    tasks = task_store.list_all()

    # 本地今日零点时间戳
    now = time.localtime()
    today_start = time.mktime(
        time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    )

    def _count(*statuses: str) -> int:
        return sum(1 for t in tasks if t.get("status") in statuses)

    trend_days = max(1, min(int(trend_days or 7), 90))
    return {
        "users": {
            "total": len(users),
            "active": sum(1 for u in users if u.get("status") == "active"),
        },
        "tasks": {
            "total": len(tasks),
            "today": sum(
                1 for t in tasks if (t.get("created_at") or 0) >= today_start
            ),
            "processing": _count("processing", "pending"),
            "completed": _count("completed"),
            "failed": _count("failed", "cancelled"),
        },
        "tokens": {
            "total": sum(int(t.get("token_consumed") or 0) for t in tasks),
        },
        # 上传文档总数（有文件体积的任务视为已上传文档）
        "upload_total": sum(1 for t in tasks if (t.get("file_size") or 0) > 0),
        "trends": task_store.daily_stats(trend_days),
    }


# =====================================================================
# 管理后台：用户管理
# =====================================================================

def _user_with_stats(
    user: dict[str, Any], stats: dict[str, dict[str, int]]
) -> dict[str, Any]:
    """附加任务统计字段（PRD 9.3.1 用户列表口径）。"""
    s = stats.get(user["id"], {})
    return {
        **user,
        "task_count": s.get("task_count", 0),
        "upload_count": s.get("upload_count", 0),
        "token_consumed": s.get("token_total", 0),
    }


@router.get("/admin/users")
async def list_users(
    search: str = "",
    role: str = "",
    status: str = "",
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
    task_store: TaskStore = Depends(get_task_store),
):
    """用户列表：支持用户名/邮箱模糊搜索、角色/状态筛选。

    普通管理员看不到超级管理员账号（PRD 11.2 数据权限）。
    """
    users = user_store.list_all(search=search, role=role, status=status)
    if admin.get("role") != "super_admin":
        users = [u for u in users if u.get("role") != "super_admin"]
    stats = task_store.stats_by_user()
    users = [_user_with_stats(u, stats) for u in users]
    return {"users": users, "total": len(users)}


@router.post("/admin/users", status_code=201)
async def create_user(
    body: UserCreateRequest,
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
):
    """创建用户。super_admin 角色仅 super_admin 可分配。"""
    if body.role not in _ROLES:
        raise HTTPException(status_code=400, detail=f"非法角色: {body.role}（可选: {', '.join(_ROLES)}）")
    if body.status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"非法状态: {body.status}（可选: {', '.join(_STATUSES)}）")
    if body.role == "super_admin" and admin.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="仅超级管理员可创建超级管理员")

    email = _validate_email(body.email)
    try:
        user = user_store.create(
            email=email, username=body.username.strip(),
            password=body.password, role=body.role, status=body.status,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return user


@router.get("/admin/users/{user_id}")
async def get_user(
    user_id: str,
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
    task_store: TaskStore = Depends(get_task_store),
):
    """用户详情：基础信息 + 任务统计 + 最近 6 条任务（PRD 9.3.3）。"""
    user = user_store.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"用户 {user_id} 不存在")
    _can_operate(admin, user)

    stats = task_store.stats_by_user()
    result = _user_with_stats(user, stats)

    recent = [
        {
            "task_id": t["task_id"],
            "filename": t.get("filename") or "未知文件",
            "status": t.get("status"),
            "created_at": t.get("created_at"),
            "token_consumed": int(t.get("token_consumed") or 0),
        }
        for t in task_store.list_all(user_id=user_id)[:6]
    ]
    result["recent_tasks"] = recent
    return result


@router.patch("/admin/users/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdateRequest,
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
):
    """更新用户信息（用户名/邮箱/角色/状态）。

    保护规则见模块 docstring。
    """
    user = user_store.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"用户 {user_id} 不存在")
    _can_operate(admin, user)

    if body.role is not None and body.role not in _ROLES:
        raise HTTPException(status_code=400, detail=f"非法角色: {body.role}")
    if body.status is not None and body.status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"非法状态: {body.status}")

    # 角色变更到 super_admin：仅 super_admin 可操作
    if (
        body.role == "super_admin"
        and user.get("role") != "super_admin"
        and admin.get("role") != "super_admin"
    ):
        raise HTTPException(status_code=403, detail="仅超级管理员可分配超级管理员角色")

    # 降级/禁用/删除最后一个 super_admin 的保护
    target_still_super = body.role in (None, "super_admin")
    if user.get("role") == "super_admin" and not target_still_super:
        if admin.get("id") == user_id:
            raise HTTPException(status_code=400, detail="不能降级自己的超级管理员角色")
        if _super_admin_count(user_store) <= 1:
            raise HTTPException(status_code=400, detail="系统至少需要保留一个超级管理员")
    if body.status == "disabled" and user.get("status") == "active":
        _guard_last_super_admin(admin, user, user_store, removing=False)

    if body.email is not None:
        email = _validate_email(body.email)
        existing = user_store.get_by_email(email)
        if existing and existing["id"] != user_id:
            raise HTTPException(status_code=409, detail=f"邮箱已被占用: {email}")
        user_store.update_profile(user_id, email=email)
    if body.username is not None:
        user_store.update_profile(user_id, username=body.username.strip())
    if body.role is not None:
        user_store.update_role(user_id, body.role)
    if body.status is not None:
        user_store.update_status(user_id, body.status)

    return user_store.get(user_id)


@router.post("/admin/users/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    body: PasswordResetRequest,
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
):
    """重置用户密码。"""
    user = user_store.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"用户 {user_id} 不存在")
    _can_operate(admin, user)

    user_store.update_password(user_id, body.new_password)
    return {"ok": True, "message": "密码已重置"}


@router.delete("/admin/users/{user_id}")
async def delete_user(
    user_id: str,
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
):
    """删除用户（仅 super_admin；不能删除自己）。"""
    if admin.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="仅超级管理员可删除用户")
    user = user_store.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"用户 {user_id} 不存在")
    _can_operate(admin, user)
    if admin.get("id") == user_id:
        raise HTTPException(status_code=400, detail="不能删除自己的账号")
    if user.get("role") == "super_admin" and _super_admin_count(user_store) <= 1:
        raise HTTPException(status_code=400, detail="系统至少需要保留一个超级管理员")

    user_store.delete(user_id)
    return {"ok": True, "message": "用户已删除"}


# =====================================================================
# 管理后台：任务列表（全用户视角）
# =====================================================================

@router.get("/admin/tasks")
async def admin_list_tasks(
    _admin: dict = Depends(require_admin),
    task_store: TaskStore = Depends(get_task_store),
):
    """全部用户的任务列表（管理视角，含 user_id 与 Token 消耗）。"""
    tasks = [
        {
            "task_id": t["task_id"],
            "filename": t.get("filename") or "未知文件",
            "status": t.get("status"),
            "progress": 1.0 if t.get("status") == "completed" else 0.0,
            "created_at": t.get("created_at"),
            "file_size": int(t.get("file_size") or 0),
            "token_consumed": int(t.get("token_consumed") or 0),
            "user_id": t.get("user_id"),
            "error": t.get("error"),
        }
        for t in task_store.list_all()
    ]
    return {"tasks": tasks, "total": len(tasks)}
