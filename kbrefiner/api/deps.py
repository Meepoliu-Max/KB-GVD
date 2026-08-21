"""API 依赖注入（deps）。

集中管理 API 层依赖的创建，方便测试时 mock。
测试隔离：_user_store / _task_store 与 routes._task_store 均为模块级
单例，测试中替换为临时目录实例（见 tests/test_auth.py fixture）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request

from kbrefiner.auth import make_user_dependency
from kbrefiner.config import Settings, get_settings
from kbrefiner.core.llm import DeepSeekClient, LLMConfig
from kbrefiner.core.sensitive import SensitiveDetector
from kbrefiner.db import TaskStore, UserStore

# 与任务库同库不同表（data/tasks.db）
_user_store = UserStore("./data/tasks.db")
_task_store = TaskStore("./data/tasks.db")


def get_user_store() -> UserStore:
    """用户存储单例。"""
    return _user_store


def get_task_store() -> TaskStore:
    """任务存储单例（与 routes._task_store 同一实例）。"""
    return _task_store


def get_current_user(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Optional[dict]:
    """解析当前登录用户。

    require_login=False（默认，开源单机模式）：未登录返回 None（匿名可用）。
    require_login=True：未登录/过期抛 401。

    settings 通过 Depends 注入（而非直调 get_settings()），
    以便测试用 dependency_overrides 覆盖 require_login。
    """
    dep = make_user_dependency(_user_store, require_login=settings.require_login)
    return dep(request)


def require_admin(user: Optional[dict] = Depends(get_current_user)) -> dict:
    """管理员守卫：未登录 401、已登录但非管理员 403。"""
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def get_llm_client(settings: Settings = Depends(get_settings)) -> DeepSeekClient:
    """创建 LLM 客户端实例。

    default_model 必须显式传入 settings.llm_model：
    客户端内置默认值为无效模型名（deepseek-v4-flash），
    DeepSeek API 对无效模型名静默返回空 content（HTTP 200），
    会触发大规模无效重试导致任务极慢。
    """
    config = LLMConfig(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        default_model=settings.llm_model,
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
    "get_current_user",
    "require_admin",
]
