"""认证与管理后台 API 路由。

前台认证（/api/auth）：
- POST /api/auth/login     邮箱+密码登录（成功设置 HttpOnly Cookie，并返回 token）
- POST /api/auth/logout    登出（清除 Cookie）
- GET  /api/auth/me        当前登录用户信息（开放模式未登录返回 user=null）
- POST /api/auth/reset-password  匿名密码重置（通过重置链接 token）

管理后台（/api/admin，需 super_admin / admin 角色）：
- GET    /api/admin/stats                       仪表盘统计（用户/任务/Token + 活跃用户/异常/排名）
- GET    /api/admin/users                       用户列表（搜索/角色/状态筛选）
- POST   /api/admin/users                       创建用户
- GET    /api/admin/users/{user_id}             用户详情（含统计/最近任务/7日趋势）
- PATCH  /api/admin/users/{user_id}             更新用户（用户名/邮箱/角色/状态）
- POST   /api/admin/users/{user_id}/reset-password  重置密码
- POST   /api/admin/users/{user_id}/reset-link      生成密码重置链接
- DELETE /api/admin/users/{user_id}             删除用户
- GET    /api/admin/tasks                       全部用户任务列表（管理视角）
- GET    /api/admin/audit-logs                  审计日志列表（分页+筛选）

权限规则（PRD 第 11 节）：
1. 普通管理员（admin）不能查看/修改超级管理员（super_admin）账号。
2. super_admin 角色只能由 super_admin 分配。
3. 不能禁用/删除自己；不能禁用/删除最后一个 super_admin。
4. 用户列表响应绝不包含 password_hash。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from kbrefiner.auth import TOKEN_COOKIE, create_token
from kbrefiner.config import get_settings
from kbrefiner.db import (
    AccessStore,
    AuditStore,
    PasswordResetStore,
    TaskStore,
    UserStore,
)

from .deps import (
    get_access_store,
    get_audit_store,
    get_current_user,
    get_password_reset_store,
    get_settings_store,
    get_task_store,
    get_user_store,
    require_admin,
)

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


class RegisterRequest(BaseModel):
    """自助注册（角色固定 user，无需传角色字段）。"""

    username: str = Field(min_length=2, max_length=50)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=6, max_length=128)


class ResetPasswordAnonymousRequest(BaseModel):
    """匿名密码重置请求（含 token 和新密码）。"""
    token: str
    new_password: str = Field(min_length=6, max_length=128)


# =====================================================================
# 内部工具
# =====================================================================

def _validate_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=400, detail=f"邮箱格式不正确: {email}")
    return email


def _check_email_access(email: str, *, check_domain: bool) -> None:
    """邮箱相关的访问控制执法（黑名单始终生效；域名白名单按开关）。

    - 用户黑名单：命中直接 403（登录 / 注册 / 管理员创建用户）
    - 域名白名单：启用时非白名单域名禁止注册/创建
    """
    access_store = get_access_store()
    settings_store = get_settings_store()

    blacklist = access_store.values("user_blacklist")
    if AccessStore.is_blacklisted(email, blacklist):
        raise HTTPException(status_code=403, detail="该账号已被列入黑名单，禁止访问")

    if check_domain and settings_store.get("access_domain_whitelist_enabled"):
        domains = access_store.values("domain_whitelist")
        if not AccessStore.domain_allowed(email, domains):
            raise HTTPException(
                status_code=403,
                detail="邮箱域名不在白名单内，请联系管理员",
            )


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


def _user_daily_trend_simple(task_store: TaskStore, user_id: str, days: int = 7) -> list[dict[str, Any]]:
    """用户近 N 天每日任务 token 趋势（稠密数组，长度恒 = days）。

    task_store 如果没有 user_daily_trend 方法，走此本地实现：
    SQL GROUP BY date(created_at, ...) → Python 补空日期。
    """
    since = time.time() - days * 86400
    try:
        # 尝试直接访问连接（TaskStore 内部 _conn）
        conn: sqlite3.Connection = task_store._conn  # type: ignore[attr-defined]
        with conn:
            rows = conn.execute(
                "SELECT date(created_at, 'unixepoch', 'localtime') AS d, "
                "COALESCE(SUM(token_consumed), 0) AS token_consumed, "
                "COUNT(*) AS task_count "
                "FROM tasks WHERE user_id = ? AND created_at >= ? GROUP BY d ORDER BY d",
                (user_id, since),
            ).fetchall()
        sparse = {r["d"]: {"token_consumed": int(r["token_consumed"] or 0),
                            "task_count": int(r["task_count"] or 0)} for r in rows}
    except Exception:
        # fallback: 遍历 list_all
        sparse: dict[str, dict[str, int]] = {}
        for t in task_store.list_all(user_id=user_id):
            if (t.get("created_at") or 0) < since:
                continue
            d_struct = time.localtime(t["created_at"])
            d = time.strftime("%Y-%m-%d", d_struct)
            entry = sparse.setdefault(d, {"token_consumed": 0, "task_count": 0})
            entry["token_consumed"] += int(t.get("token_consumed") or 0)
            entry["task_count"] += 1

    # 生成 days 天稠密数组（日期升序，D-days+1 到今日）
    result: list[dict[str, Any]] = []
    now_local = time.localtime()
    today_str = time.strftime("%Y-%m-%d", now_local)
    # 从 days-1 天前到今天
    import datetime as _dt
    today_dt = _dt.date.fromtimestamp(time.mktime(now_local))
    for i in range(days - 1, -1, -1):
        d = today_dt - _dt.timedelta(days=i)
        d_str = d.isoformat()
        e = sparse.get(d_str, {"token_consumed": 0, "task_count": 0})
        result.append({
            "date": d_str,
            "task_count": e["task_count"],
            "token_consumed": e["token_consumed"],
        })
    return result


# =====================================================================
# 前台认证
# =====================================================================

@router.post("/auth/login")
async def login(
    body: LoginRequest,
    response: Response,
    request: Request,
    user_store: UserStore = Depends(get_user_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """邮箱+密码登录。

    成功：设置 HttpOnly Cookie（浏览器页面用）并返回 token（API 客户端用）。
    失败：401（邮箱/密码错误或账号被禁用，不区分提示以防枚举）。
    连续失败锁定：达到阈值后账号锁定 N 分钟（配置见系统设置）。
    用户黑名单：命中返回 403。
    """
    email = body.email.strip().lower()
    _check_email_access(email, check_domain=False)

    settings_store = get_settings_store()
    fail_limit = int(settings_store.get("login_fail_limit") or 5)
    lock_minutes = int(settings_store.get("login_lock_minutes") or 30)

    remaining = user_store.lock_remaining(email)
    if remaining > 0:
        minutes = max(1, int(remaining // 60 + 1))
        msg = f"登录失败次数过多，账号已锁定，请 {minutes} 分钟后再试"
        try:
            audit_store.record(user=None, action="auth.login.failed",
                               target_id=email, detail=f"失败原因: {msg}", request=request)
        except Exception:
            pass
        raise HTTPException(status_code=401, detail=msg)

    user = user_store.verify_login(email, body.password)
    if not user:
        user_store.record_login_failure(email, fail_limit, lock_minutes)
        msg = "邮箱或密码错误，或账号已被禁用"
        try:
            audit_store.record(user=None, action="auth.login.failed",
                               target_id=email, detail=f"失败原因: {msg}", request=request)
        except Exception:
            pass
        raise HTTPException(status_code=401, detail=msg)
    user_store.clear_login_failures(user["id"])

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
    try:
        audit_store.record(user=user, action="auth.login.success",
                           target_type="users", target_id=user["id"], request=request)
    except Exception:
        pass
    return {"token": token, "user": user}


@router.post("/auth/register", status_code=201)
async def register(
    body: RegisterRequest,
    request: Request,
    user_store: UserStore = Depends(get_user_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """用户自助注册（邮箱 + 密码，角色固定 user）。

    受系统设置控制：
    - access_allow_register=false 时关闭注册（403）
    - 域名白名单启用时仅白名单域名可注册
    - 用户黑名单始终拒绝
    """
    settings_store = get_settings_store()
    if not settings_store.get("access_allow_register", True):
        raise HTTPException(status_code=403, detail="当前未开放自助注册，请联系管理员")

    email = _validate_email(body.email)
    _check_email_access(email, check_domain=True)

    try:
        user = user_store.create(
            email=email, username=body.username.strip(),
            password=body.password, role="user", status="active",
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    try:
        audit_store.record(user=None, action="user.registered",
                           target_type="users", target_id=user["id"], request=request)
    except Exception:
        pass
    return user


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


@router.post("/auth/reset-password")
async def reset_password_anonymous(
    body: ResetPasswordAnonymousRequest,
    user_store: UserStore = Depends(get_user_store),
    password_reset_store: PasswordResetStore = Depends(get_password_reset_store),
):
    """匿名重置密码：通过重置链接 token + 新密码。

    流程：verify → consume → update_password。
    """
    info = password_reset_store.verify(body.token)
    if not info:
        raise HTTPException(status_code=400, detail="重置链接无效或已过期")
    if not password_reset_store.consume(body.token):
        raise HTTPException(status_code=400, detail="重置链接已被使用")
    user_store.update_password(info["user_id"], body.new_password)
    return {"ok": True, "message": "密码已重置"}


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

    扩展字段（FR-1）：
    - active_users: 至少有 1 条任务的独立用户数（非 NULL）
    - abnormal_tasks: failed + cancelled + warning 状态的任务数
    - user_rankings: Top 10 按 token_total 降序用户列表

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

    # ===== FR-1: active_users =====
    active_user_ids = {t.get("user_id") for t in tasks if t.get("user_id") is not None}
    active_users = len(active_user_ids)

    # ===== FR-1: abnormal_tasks =====
    abnormal_tasks = sum(
        1 for t in tasks
        if t.get("status") in ("failed", "cancelled", "warning")
    )

    # ===== FR-1: user_rankings =====
    stats_by_uid = task_store.stats_by_user()
    ranking_items: list[dict[str, Any]] = []
    for uid, s in stats_by_uid.items():
        u = user_store.get(uid)
        if not u:
            continue  # 过滤已删除用户
        ranking_items.append({
            "user_id": uid,
            "username": u.get("username", ""),
            "task_count": int(s.get("task_count", 0)),
            "token_consumed": int(s.get("token_total", 0)),
        })
    ranking_items.sort(key=lambda x: -x["token_consumed"])
    user_rankings = ranking_items[:10]

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
        # ===== FR-1 新增字段 =====
        "active_users": active_users,
        "abnormal_tasks": abnormal_tasks,
        "user_rankings": user_rankings,
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
    request: Request,
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """创建用户。super_admin 角色仅 super_admin 可分配。"""
    if body.role not in _ROLES:
        raise HTTPException(status_code=400, detail=f"非法角色: {body.role}（可选: {', '.join(_ROLES)}）")
    if body.status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"非法状态: {body.status}（可选: {', '.join(_STATUSES)}）")
    if body.role == "super_admin" and admin.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="仅超级管理员可创建超级管理员")

    email = _validate_email(body.email)
    # 黑名单始终拒绝；域名白名单启用时校验（PRD 9.6.4）
    _check_email_access(email, check_domain=True)
    try:
        user = user_store.create(
            email=email, username=body.username.strip(),
            password=body.password, role=body.role, status=body.status,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    try:
        audit_store.record(user=admin, action="user.created", target_type="users",
                           target_id=user["id"], detail=f"角色={user['role']}", request=request)
    except Exception:
        pass
    return user


@router.get("/admin/users/{user_id}")
async def get_user(
    user_id: str,
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
    task_store: TaskStore = Depends(get_task_store),
):
    """用户详情：基础信息 + 任务统计 + 最近 6 条任务 + 7 日 token 趋势（FR-2）。"""
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

    # ===== FR-2: trend_7d（稠密 7 天数组）=====
    # 优先使用 task_store 的方法（如果存在），否则本地实现
    trend_method = getattr(task_store, "user_daily_trend", None)
    if callable(trend_method):
        try:
            trend_7d_raw = trend_method(user_id, days=7)
            # 统一字段名：token_total → token_consumed（TaskStore 返回 token_total）
            trend_7d = [
                {
                    "date": entry["date"],
                    "task_count": int(entry.get("task_count") or 0),
                    "token_consumed": int(
                        entry.get("token_consumed") or entry.get("token_total") or 0
                    ),
                }
                for entry in trend_7d_raw
            ]
        except Exception:
            trend_7d = _user_daily_trend_simple(task_store, user_id, days=7)
    else:
        trend_7d = _user_daily_trend_simple(task_store, user_id, days=7)
    result["trend_7d"] = trend_7d

    return result


@router.patch("/admin/users/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdateRequest,
    request: Request,
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
    audit_store: AuditStore = Depends(get_audit_store),
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

    try:
        changed_keys = list(body.model_dump(exclude_none=True).keys())
        audit_store.record(user=admin, action="user.updated", target_type="users",
                           target_id=user_id, detail=f"更新={changed_keys}", request=request)
    except Exception:
        pass

    return user_store.get(user_id)


@router.post("/admin/users/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    body: PasswordResetRequest,
    request: Request,
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """重置用户密码。"""
    user = user_store.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"用户 {user_id} 不存在")
    _can_operate(admin, user)

    user_store.update_password(user_id, body.new_password)
    try:
        audit_store.record(user=admin, action="user.password_reset",
                           target_type="users", target_id=user_id, request=request)
    except Exception:
        pass
    return {"ok": True, "message": "密码已重置"}


@router.post("/admin/users/{user_id}/reset-link")
async def create_reset_link(
    user_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
    password_reset_store: PasswordResetStore = Depends(get_password_reset_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """生成密码重置链接（24h TTL，一次性，仅超级管理员）。"""
    if admin.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="仅超级管理员可生成密码重置链接")
    user = user_store.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"用户 {user_id} 不存在")
    _can_operate(admin, user)

    token = password_reset_store.create(user_id)
    info = password_reset_store.verify(token)
    expires_at = info["expires_at"] if info else (time.time() + 86400)
    try:
        audit_store.record(user=admin, action="user.reset_link_created",
                           target_type="users", target_id=user_id, request=request)
    except Exception:
        pass
    return {
        "reset_url": f"/reset?token={token}",
        "token": token,
        "expires_at": expires_at,
    }


@router.delete("/admin/users/{user_id}")
async def delete_user(
    user_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
    user_store: UserStore = Depends(get_user_store),
    audit_store: AuditStore = Depends(get_audit_store),
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
    try:
        audit_store.record(user=admin, action="user.deleted",
                           target_type="users", target_id=user_id, request=request)
    except Exception:
        pass
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


# =====================================================================
# 管理后台：审计日志（P6 FR-6.1）
# =====================================================================

@router.get("/admin/audit-logs")
async def list_audit_logs(
    limit: int = 50,
    page: int = 1,
    offset: int = 0,
    user_id: str = "",
    user_keyword: str = "",
    action: str = "",
    days: int = 0,
    _admin: dict = Depends(require_admin),
    audit_store: AuditStore = Depends(get_audit_store),
    user_store: UserStore = Depends(get_user_store),
):
    """审计日志列表（按创建时间倒序，支持分页/关键词/动作/时间窗筛选）。"""
    limit = max(1, min(int(limit), 500))
    # 优先使用显式 offset；否则按 page 计算
    if int(offset) > 0:
        offset_val = max(0, int(offset))
    else:
        page = max(1, int(page))
        offset_val = (page - 1) * limit

    since = None
    if days and days > 0:
        since = time.time() - int(days) * 86400

    # 直接传 user_id → 跳过 user_keyword
    single_user_id: Optional[str] = None
    if user_id:
        single_user_id = str(user_id).strip()
    elif user_keyword:
        matched_users = user_store.list_all(search=user_keyword)
        matched_user_ids = [u["id"] for u in matched_users]
        if len(matched_user_ids) == 1:
            single_user_id = matched_user_ids[0]
        # 多用户命中：用 user_keyword，但这里 Python 侧过滤效果有限；简单取第一个
        elif matched_user_ids:
            single_user_id = None  # 后续走 list 不加 user_id 过滤，查全部然后 Python 过滤

    logs = audit_store.list(
        limit=limit,
        offset=offset_val,
        user_id=single_user_id,
        action=action.strip() or None,
        since=since,
    )

    # 如果多用户匹配（user_keyword 命中 >1 人），在 Python 侧按匹配集过滤
    if user_id == "" and user_keyword and single_user_id is None:
        matched_users = user_store.list_all(search=user_keyword)
        id_set = {u["id"] for u in matched_users}
        logs = [l for l in logs if l.get("user_id") in id_set]

    total = audit_store.count(
        user_id=single_user_id,
        action=action.strip() or None,
        since=since,
    )

    return {
        "logs": logs,
        "total": total,
        "page": page,
        "limit": limit,
    }
