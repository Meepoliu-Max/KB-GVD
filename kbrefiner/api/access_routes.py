"""黑白名单 API 路由（/api/admin/access，需管理员）。

- GET    /api/admin/access                 全量规则 + 开关状态
- PUT    /api/admin/access/toggle          启用/停用某类校验（仅 super_admin）
- POST   /api/admin/access/rules           添加规则（IP/CIDR/域名/邮箱）
- DELETE /api/admin/access/rules/{rule_id} 删除规则

执法（不在本文件，见 deps/routes）：
- IP 白名单：管理后台 API + 上传/处理（启用时，回环始终放行）
- 域名白名单：创建用户 / 自助注册（启用时）
- 用户黑名单：登录 / 自助注册（始终生效）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from kbrefiner.db import AccessStore, SettingsStore, UserStore

from .deps import get_access_store, get_settings_store, get_user_store, require_admin

router = APIRouter(prefix="/api/admin", tags=["黑白名单"])

# 规则类型 → 设置开关键
_TOGGLE_KEYS = {
    "ip_whitelist": "access_ip_whitelist_enabled",
    "domain_whitelist": "access_domain_whitelist_enabled",
}


class RuleCreateRequest(BaseModel):
    type: str
    value: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=200)


class ToggleRequest(BaseModel):
    type: str
    enabled: bool


@router.get("/access")
async def get_access(
    _admin: dict = Depends(require_admin),
    access_store: AccessStore = Depends(get_access_store),
    settings_store: SettingsStore = Depends(get_settings_store),
    user_store: UserStore = Depends(get_user_store),
):
    """全量访问规则与开关状态（黑名单附用户展示名）。"""
    ip_rules = access_store.list_rules("ip_whitelist")
    domain_rules = access_store.list_rules("domain_whitelist")
    blacklist = access_store.list_rules("user_blacklist")

    # 黑名单行附用户名（邮箱可能对应已存在用户）
    email_to_user = {u["email"]: u for u in user_store.list_all()}
    for r in blacklist:
        u = email_to_user.get(r["value"].strip().lower())
        r["username"] = u["username"] if u else r["value"].split("@")[0]

    return {
        "ip_whitelist": {
            "enabled": bool(settings_store.get("access_ip_whitelist_enabled")),
            "rules": ip_rules,
        },
        "domain_whitelist": {
            "enabled": bool(settings_store.get("access_domain_whitelist_enabled")),
            "rules": domain_rules,
        },
        "user_blacklist": {"rules": blacklist},
    }


@router.put("/access/toggle")
async def toggle_access(
    body: ToggleRequest,
    admin: dict = Depends(require_admin),
    settings_store: SettingsStore = Depends(get_settings_store),
):
    """启用/停用某类校验。开关影响全局访问，仅超级管理员可操作。"""
    if admin.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="仅超级管理员可修改访问开关")
    if body.type not in _TOGGLE_KEYS:
        raise HTTPException(status_code=400, detail=f"非法类型: {body.type}")
    settings_store.set(_TOGGLE_KEYS[body.type], body.enabled, updated_by=admin["id"])
    return {"ok": True, "type": body.type, "enabled": body.enabled}


@router.post("/access/rules", status_code=201)
async def create_rule(
    body: RuleCreateRequest,
    admin: dict = Depends(require_admin),
    access_store: AccessStore = Depends(get_access_store),
):
    """添加访问规则。value 格式校验见 AccessStore.validate_value。"""
    try:
        rule = access_store.create(
            body.type, body.value, body.note, created_by=admin.get("id", "")
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return rule


@router.delete("/access/rules/{rule_id}")
async def delete_rule(
    rule_id: str,
    _admin: dict = Depends(require_admin),
    access_store: AccessStore = Depends(get_access_store),
):
    rule = access_store.get(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail=f"规则 {rule_id} 不存在")
    access_store.delete(rule_id)
    return {"ok": True, "message": "已删除"}
