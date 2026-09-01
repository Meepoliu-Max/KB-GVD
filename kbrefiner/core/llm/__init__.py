"""LLM 客户端公共接口。

用法：
    from kbrefiner.core.llm import LLMClient

    client = LLMClient()  # 从环境变量读取 API Key

    # Stage 1/2/4：默认用快速模型
    result = client.chat_json(system, user)

    # Stage 3：复杂 QA 生成，切 Pro 模型
    result = client.chat_json(system, user, model="deepseek-v4-pro")

    # 异步并行（Stage 3/4 并行）
    import asyncio
    t3 = client.chat_json_async(s3, u3, model="deepseek-v4-pro")
    t4 = client.chat_json_async(s4, u4)
    r3, r4 = asyncio.gather(t3, t4)
"""
from kbrefiner.core.llm.deepseek_client import (
    DEFAULT_BASE_URL,
    ChatResult,
    DeepSeekClient,
    LLMClient,
    LLMConfig,
    Model,
)
from kbrefiner.core.llm.exceptions import (
    ApiError,
    ConfigurationError,
    EmptyContentError,
    JsonParseError,
    LlmError,
)

__all__ = [
    # 客户端
    "DeepSeekClient",
    "LLMClient",
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
