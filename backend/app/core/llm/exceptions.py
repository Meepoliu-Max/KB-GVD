"""LLM 客户端异常体系。

4 级异常，与 parser 模块风格一致：
- LlmError：基础异常
- ConfigurationError：配置错误（API Key 缺失、模型名非法等）
- ApiError：API 调用失败（HTTP 错误、限流、服务端错误）
- EmptyContentError：JSON Output 模式下返回空 content（DeepSeek 已知问题）
- JsonParseError：返回内容不是合法 JSON
"""
from __future__ import annotations


class LlmError(Exception):
    """LLM 客户端基础异常。"""


class ConfigurationError(LlmError):
    """配置错误（API Key 缺失、模型名非法、参数无效等）。"""


class ApiError(LlmError):
    """API 调用失败（HTTP 错误、限流 429、服务端 5xx、超时）。"""

    def __init__(self, message: str, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class EmptyContentError(LlmError):
    """JSON Output 模式下 API 返回空 content。

    DeepSeek 官方文档已知问题：使用 response_format=json_object 时，
    API 有概率返回空 content。需要重试机制兜底。
    """


class JsonParseError(LlmError):
    """返回内容无法解析为合法 JSON。"""

    def __init__(self, message: str, raw_content: str | None = None):
        super().__init__(message)
        self.raw_content = raw_content


__all__ = [
    "LlmError",
    "ConfigurationError",
    "ApiError",
    "EmptyContentError",
    "JsonParseError",
]
