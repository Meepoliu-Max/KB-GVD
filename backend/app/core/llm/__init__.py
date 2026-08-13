"""LLM 客户端公共接口。

用法：
    from app.core.llm import DeepSeekClient, Model

    client = DeepSeekClient()

    # Stage 1/2/4：默认用 V4-Flash
    result = client.chat_json(system, user)

    # Stage 3：复杂 QA 生成，切 V4-Pro
    result = client.chat_json(system, user, model=Model.V4_PRO)

    # 异步并行（Stage 3/4 并行）
    import asyncio
    t3 = client.chat_json_async(s3, u3, model=Model.V4_PRO)
    t4 = client.chat_json_async(s4, u4)
    r3, r4 = asyncio.gather(t3, t4)
"""
from app.core.llm.deepseek_client import (
    DEFAULT_BASE_URL,
    ChatResult,
    DeepSeekClient,
    LLMConfig,
    Model,
)
from app.core.llm.exceptions import (
    ApiError,
    ConfigurationError,
    EmptyContentError,
    JsonParseError,
    LlmError,
)

__all__ = [
    # 客户端
    "DeepSeekClient",
    "Model",
    "LLMConfig",
    "ChatResult",
    "DEFAULT_BASE_URL",
    # 异常
    "LlmError",
    "ConfigurationError",
    "ApiError",
    "EmptyContentError",
    "JsonParseError",
]
