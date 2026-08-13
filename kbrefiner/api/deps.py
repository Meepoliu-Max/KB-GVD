"""API 依赖注入（deps）。

集中管理 API 层依赖的创建，方便测试时 mock。
"""
from __future__ import annotations

from fastapi import Depends

from kbrefiner.config import Settings, get_settings
from kbrefiner.core.llm import DeepSeekClient, LLMConfig
from kbrefiner.core.sensitive import SensitiveDetector


def get_llm_client(settings: Settings = Depends(get_settings)) -> DeepSeekClient:
    """创建 LLM 客户端实例。"""
    config = LLMConfig(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
    )
    return DeepSeekClient(config=config)


def get_sensitive_detector() -> SensitiveDetector:
    """创建敏感数据检测器（无状态，可共享）。"""
    return SensitiveDetector()


__all__ = [
    "get_llm_client",
    "get_sensitive_detector",
]