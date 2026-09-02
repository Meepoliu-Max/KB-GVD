"""API 依赖注入（deps）。

集中管理 API 层依赖的创建，方便测试时 mock。
测试隔离：_user_store / _task_store 等模块级单例在测试中替换为
临时目录实例（见 tests/test_auth.py / tests/test_admin_modules.py fixture）。

运行时配置优先级：SettingsStore（后台可改，即时生效）> .env（Settings）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request

from kbrefiner.auth import make_user_dependency
from kbrefiner.config import Settings, get_settings
from kbrefiner.core.llm import DeepSeekClient, LLMConfig
from kbrefiner.core.sensitive import SensitiveDetector
from kbrefiner.db import (
    AccessStore,
    AuditStore,
    MessageStore,
    PasswordResetStore,
    SettingsStore,
    TaskStore,
    UserStore,
    client_ip,
)
from kbrefiner.db.settings_store import decrypt_value

# 与任务库同库不同表（data/tasks.db）
_user_store = UserStore("./data/tasks.db")
_task_store = TaskStore("./data/tasks.db")
_settings_store = SettingsStore("./data/tasks.db")
_message_store = MessageStore("./data/tasks.db")
_access_store = AccessStore("./data/tasks.db")
_password_reset_store = PasswordResetStore("./data/tasks.db")
_audit_store = AuditStore("./data/tasks.db")


def get_user_store() -> UserStore:
    """用户存储单例。"""
    return _user_store


def get_task_store() -> TaskStore:
    """任务存储单例（与 routes._task_store 同一实例）。"""
    return _task_store


def get_settings_store() -> SettingsStore:
    """系统设置存储单例。"""
    return _settings_store


def get_message_store() -> MessageStore:
    """消息存储单例。"""
    return _message_store


def get_access_store() -> AccessStore:
    """访问规则存储单例。"""
    return _access_store


def get_password_reset_store() -> PasswordResetStore:
    """密码重置令牌存储单例。"""
    return _password_reset_store


def get_audit_store() -> AuditStore:
    """审计日志存储单例。"""
    return _audit_store


# =====================================================================
# 运行时配置读取（DB 覆盖 > .env 默认）
# =====================================================================

def runtime_setting(key: str, env_default: object = None) -> object:
    """读取运行时配置：SettingsStore 覆盖值 > env_default。"""
    db_val = _settings_store.get(key)
    if db_val is None:
        return env_default
    return db_val


def require_login_enabled(env_default: object = None) -> bool:
    """前台是否强制登录（后台开关即时生效，未设置时跟随 .env）。

    env_default 允许调用方注入 .env 默认值（测试用 dependency_overrides
    覆盖 get_settings 时，经参数传入而非内部直读单例）。
    """
    if env_default is None:
        env_default = get_settings().require_login
    return bool(runtime_setting("access_require_login", env_default))


def check_ip_allowed(request: Request) -> None:
    """IP 白名单执法：启用且未命中时抛 403（回环始终放行，防锁死）。

    适用范围（PRD 9.6.1）：管理后台 API 与文件上传/处理。
    """
    enabled = bool(_settings_store.get("access_ip_whitelist_enabled"))
    if not enabled:
        return
    rules = _access_store.values("ip_whitelist")
    ip = client_ip(request)
    if not AccessStore.ip_allowed(ip, rules):
        raise HTTPException(
            status_code=403,
            detail=f"IP {ip} 不在访问白名单内，请联系管理员",
        )


def get_current_user(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Optional[dict]:
    """解析当前登录用户。

    开放模式：未登录返回 None（匿名可用）。
    强制登录模式（.env REQUIRE_LOGIN 或后台开关）：未登录/过期抛 401。
    """
    # env 默认值取注入的 settings（测试 override 生效），DB 开关优先
    require = require_login_enabled(settings.require_login)
    dep = make_user_dependency(_user_store, require_login=require)
    return dep(request)


def require_admin(request: Request, user: Optional[dict] = Depends(get_current_user)) -> dict:
    """管理员守卫：IP 白名单 > 登录 > 角色，逐层拦截。"""
    check_ip_allowed(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def get_llm_client(settings: Settings = Depends(get_settings)) -> DeepSeekClient:
    """创建 LLM 客户端实例（后台可覆盖 API Key / Base URL / 模型）。

    default_model 必须显式传入：客户端内置默认值为无效模型名
    （deepseek-v4-flash），DeepSeek API 对无效模型名静默返回空 content
    （HTTP 200），会触发大规模无效重试导致任务极慢。
    """
    db_key = decrypt_value(str(_settings_store.get("llm_api_key") or ""))
    api_key = db_key or settings.llm_api_key
    base_url = str(_settings_store.get("llm_base_url") or "") or settings.llm_base_url
    model = str(_settings_store.get("llm_model") or "") or settings.llm_model

    config = LLMConfig(
        api_key=api_key,
        base_url=base_url,
        default_model=model,
    )
    return DeepSeekClient(config=config)


def get_sensitive_detector() -> SensitiveDetector:
    """创建敏感数据检测器（无状态，可共享）。"""
    return SensitiveDetector()


__all__ = [
    "get_llm_client",
    "get_sensitive_detector",
    "get_user_store",
    "get_task_store",
    "get_settings_store",
    "get_message_store",
    "get_access_store",
    "get_password_reset_store",
    "get_audit_store",
    "get_current_user",
    "require_admin",
    "runtime_setting",
    "require_login_enabled",
    "check_ip_allowed",
]
