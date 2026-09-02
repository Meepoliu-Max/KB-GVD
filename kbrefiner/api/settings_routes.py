"""系统设置 API 路由（/api/admin/settings）。

- GET /api/admin/settings   读取全量设置（分组返回，API Key 脱敏）
- PUT /api/admin/settings   部分更新（仅超级管理员，PRD 7：普通管理员只读）

设置即时生效项：配额 / 允许文件类型 / 通知开关 / LLM 覆盖 /
强制登录 / 注册开关 / 登录锁定。
重启生效项：存储路径（uploads / outputs / tmp_dir）。
仅存储项（v1 不做实际动作）：通知推送渠道、会话超时、审计落库。
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from kbrefiner.config import Settings, get_settings
from kbrefiner.db import AuditStore, SettingsStore, TaskStore, UserStore

from .deps import (
    get_audit_store,
    get_settings_store,
    get_task_store,
    get_user_store,
    require_admin,
)
from kbrefiner.db.settings_store import decrypt_value, encrypt_value

router = APIRouter(prefix="/api/admin", tags=["系统设置"])

# 解析器支持的文件类型白名单（doc 旧格式不支持）
_SUPPORTED_FILE_TYPES = {"pdf", "docx", "md", "txt"}


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]


def _disk_usage_gb() -> float:
    """data 目录当前占用（GB，两位小数）。"""
    total = 0
    for root, _dirs, files in os.walk("./data"):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                continue
    return round(total / 1024 ** 3, 2)


@router.get("/settings")
async def get_settings_(
    _admin: dict = Depends(require_admin),
    settings_store: SettingsStore = Depends(get_settings_store),
    env_settings: Settings = Depends(get_settings),
):
    """全量设置（分组）。llm_api_key 脱敏，llm_key_set 指示是否已配置。"""
    _ = settings_store
    db = _admin  # 占位避免未使用告警；实际读取走 deps 单例（见下）
    from .deps import _settings_store

    all_cfg = _settings_store.get_all()
    # LLM：DB 覆盖 > .env
    db_key = decrypt_value(str(all_cfg.get("llm_api_key") or ""))
    effective_key = db_key or env_settings.llm_api_key

    return {
        "quotas": {
            "quota_task_daily": all_cfg["quota_task_daily"],
            "quota_token_daily": all_cfg["quota_token_daily"],
            "quota_upload_daily": all_cfg["quota_upload_daily"],
            "quota_file_size_mb": all_cfg["quota_file_size_mb"],
            "quota_batch_count": all_cfg["quota_batch_count"],
        },
        "file": {
            "allowed_file_types": all_cfg["allowed_file_types"],
            "supported_file_types": sorted(_SUPPORTED_FILE_TYPES),
            "retain_file_days": all_cfg["retain_file_days"],
            "retain_result_days": all_cfg["retain_result_days"],
        },
        "notify": {
            "notify_task_done": all_cfg["notify_task_done"],
            "notify_task_failed": all_cfg["notify_task_failed"],
            "notify_report": all_cfg["notify_report"],
            # v1 无推送渠道，仅存储开关
            "push_channel_available": False,
        },
        "llm": {
            "llm_provider": all_cfg["llm_provider"],
            "llm_api_key_masked": _mask_key(effective_key),
            "llm_key_set": bool(effective_key),
            "llm_key_from_env": bool(not db_key and env_settings.llm_api_key),
            "llm_base_url": str(all_cfg["llm_base_url"]) or env_settings.llm_base_url,
            "llm_model": str(all_cfg["llm_model"]) or env_settings.llm_model,
        },
        "access": {
            # None 表示跟随 .env（前端显示当前生效值）
            "access_require_login": all_cfg["access_require_login"],
            "require_login_effective": bool(
                all_cfg["access_require_login"]
                if all_cfg["access_require_login"] is not None
                else env_settings.require_login
            ),
            "access_allow_register": all_cfg["access_allow_register"],
        },
        "storage": {
            "upload_dir": env_settings.upload_dir,
            "output_dir": env_settings.output_dir,
            "tmp_dir": str(all_cfg.get("tmp_dir") or ""),
            "disk_max_gb": all_cfg.get("disk_max_gb", 500),
            "disk_warn_percent": all_cfg.get("disk_warn_percent", 80),
            "disk_used_gb": _disk_usage_gb(),
            # 路径改动重启后生效
            "paths_need_restart": True,
        },
        "security": {
            "login_fail_limit": all_cfg["login_fail_limit"],
            "login_lock_minutes": all_cfg["login_lock_minutes"],
            "session_timeout_minutes": all_cfg["session_timeout_minutes"],
            "audit_log_enabled": all_cfg["audit_log_enabled"],
        },
    }


class SettingsUpdateRequest(BaseModel):
    """部分更新：仅提交需要变更的分组字段。

    llm_api_key 留空 / 不提交表示保持不变；显式传空串表示清除 DB 覆盖
    （回落 .env）。
    """

    quotas: Optional[dict[str, int]] = None
    file: Optional[dict[str, Any]] = None
    notify: Optional[dict[str, bool]] = None
    llm: Optional[dict[str, Optional[str]]] = None
    access: Optional[dict[str, bool]] = None
    storage: Optional[dict[str, Any]] = None
    security: Optional[dict[str, int]] = None


_QUOTA_FIELDS = {
    "quota_task_daily", "quota_token_daily", "quota_upload_daily",
    "quota_file_size_mb", "quota_batch_count",
}
_SECURITY_FIELDS = {"login_fail_limit", "login_lock_minutes", "session_timeout_minutes"}


@router.put("/settings")
async def update_settings(
    body: SettingsUpdateRequest,
    request: Request,
    admin: dict = Depends(require_admin),
    settings_store: SettingsStore = Depends(get_settings_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """更新系统设置（仅超级管理员）。配额数值必须非负。"""
    if admin.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="仅超级管理员可修改系统设置")

    updates: dict[str, Any] = {}

    if body.quotas:
        for k, v in body.quotas.items():
            if k not in _QUOTA_FIELDS:
                raise HTTPException(status_code=400, detail=f"未知配额项: {k}")
            if not isinstance(v, int) or v < 0:
                raise HTTPException(status_code=400, detail=f"{k} 必须为非负整数")
            updates[k] = v

    if body.file:
        f = body.file
        if "allowed_file_types" in f:
            types = f["allowed_file_types"]
            if not isinstance(types, list) or not types:
                raise HTTPException(status_code=400, detail="allowed_file_types 不能为空")
            illegal = {str(t).lower() for t in types} - _SUPPORTED_FILE_TYPES
            if illegal:
                raise HTTPException(
                    status_code=400,
                    detail=f"不支持的文件类型: {', '.join(sorted(illegal))}",
                )
            updates["allowed_file_types"] = [str(t).lower() for t in types]
        for k in ("retain_file_days", "retain_result_days"):
            if k in f:
                v = f[k]
                if not isinstance(v, int) or v < 1:
                    raise HTTPException(status_code=400, detail=f"{k} 必须为正整数")
                updates[k] = v

    if body.notify:
        for k in ("notify_task_done", "notify_task_failed", "notify_report"):
            if k in body.notify:
                updates[k] = bool(body.notify[k])

    if body.llm:
        llm = body.llm
        if "llm_provider" in llm and llm["llm_provider"]:
            updates["llm_provider"] = str(llm["llm_provider"]).strip()
        if "llm_api_key" in llm:
            raw = llm["llm_api_key"]
            if raw is None or raw == "":
                # 清除 DB 覆盖，回落 .env
                updates["llm_api_key"] = None
            else:
                updates["llm_api_key"] = encrypt_value(str(raw).strip())
        if "llm_base_url" in llm:
            updates["llm_base_url"] = (llm["llm_base_url"] or "").strip()
        if "llm_model" in llm:
            updates["llm_model"] = (llm["llm_model"] or "").strip()

    if body.access:
        if "access_require_login" in body.access:
            updates["access_require_login"] = bool(body.access["access_require_login"])
        if "access_allow_register" in body.access:
            updates["access_allow_register"] = bool(body.access["access_allow_register"])

    if body.storage:
        s = body.storage
        if "upload_dir" in s and s["upload_dir"]:
            updates["upload_dir"] = str(s["upload_dir"]).strip()
        if "output_dir" in s and s["output_dir"]:
            updates["output_dir"] = str(s["output_dir"]).strip()
        # FR-3: tmp_dir，只要键存在就写入（允许空串）
        if "tmp_dir" in s:
            updates["tmp_dir"] = str(s["tmp_dir"]).strip()
        for k in ("disk_max_gb", "disk_warn_percent"):
            if k in s:
                v = s[k]
                if not isinstance(v, (int, float)) or v < 0:
                    raise HTTPException(status_code=400, detail=f"{k} 必须为非负数值")
                updates[k] = v

    if body.security:
        for k, v in body.security.items():
            if k not in _SECURITY_FIELDS:
                raise HTTPException(status_code=400, detail=f"未知安全项: {k}")
            if not isinstance(v, int) or v < 1:
                raise HTTPException(status_code=400, detail=f"{k} 必须为正整数")
            updates[k] = v

    if not updates:
        raise HTTPException(status_code=400, detail="没有需要更新的配置")

    for k, v in updates.items():
        settings_store.set(k, v, updated_by=admin["id"])

    # FR-6.2: 审计日志（best-effort）
    try:
        audit_store.record(
            user=admin, action="settings.updated", target_type="settings",
            detail=json.dumps(sorted(updates.keys()), ensure_ascii=False),
            request=request,
        )
    except Exception:
        pass

    return {"ok": True, "updated_keys": sorted(updates.keys()), "message": "设置已保存"}
