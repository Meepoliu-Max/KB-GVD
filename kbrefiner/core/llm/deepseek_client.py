"""LLM 客户端封装（通用 OpenAI 兼容）。

基于 OpenAI SDK，兼容所有 OpenAI 格式的 LLM API：
- DeepSeek（默认）
- OpenAI（GPT 系列）
- 本地 Ollama / vLLM 等

提供：
- 模型切换：默认模型（快速）/ Pro 模型（复杂任务）
- JSON Output：response_format={'type': 'json_object'}，自动解析返回 dict
- 重试机制：空 content / 限流 429 / 服务端 5xx 自动重试，指数退避
- 超时控制：单次请求超时 + 总重试预算
- 异步支持：async def chat_async（流水线并行调用用）

用法：
    from kbrefiner.core.llm import LLMClient

    client = LLMClient()  # 从环境变量读取 API Key

    # 同步调用（JSON Output）
    result = client.chat_json(
        system_prompt="你是助手，输出 JSON",
        user_prompt="生成 {\"a\": 1}",
    )
    # result 是 dict

    # 指定模型
    result = client.chat_json(..., model="deepseek-v4-pro")

    # 异步调用
    result = await client.chat_json_async(...)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from openai import APIError, APITimeoutError, AsyncOpenAI, OpenAI, RateLimitError

from kbrefiner.core.llm.exceptions import (
    ApiError,
    ConfigurationError,
    EmptyContentError,
    JsonParseError,
    LlmError,
)

logger = logging.getLogger(__name__)

# 默认 BASE URL（DeepSeek 官方，OpenAI 兼容格式）
DEFAULT_BASE_URL = "https://api.deepseek.com"


class Model(str, Enum):
    """模型枚举（向后兼容 DeepSeek 命名）。

    V4_FLASH：快速模型，默认用于 Stage 1/2/4
    V4_PRO：复杂推理模型，用于 Stage 3 QA 生成等复杂任务

    注意：使用非 DeepSeek 模型时，可直接传字符串模型名，无需使用此枚举。
    """

    V4_FLASH = "deepseek-v4-flash"
    V4_PRO = "deepseek-v4-pro"


@dataclass
class ChatResult:
    """单次聊天调用结果。

    Attributes:
        content: 模型返回的文本内容（JSON Output 模式下是 JSON 字符串）
        parsed: 解析后的 dict（仅 JSON Output 模式下有值）
        model: 实际使用的模型名
        usage: token 使用统计（含 prompt_tokens/completion_tokens/total_tokens）
        raw: 原始响应对象（调试用）
        attempts: 实际尝试次数（含首次失败的重试）
    """

    content: str
    parsed: dict[str, Any] | None = None
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None
    attempts: int = 1


@dataclass
class LLMConfig:
    """LLM 客户端配置。

    所有字段有合理默认值，调用方可按需覆盖。
    """

    api_key: str = ""  # 空则从 LLM_API_KEY 环境变量读取
    base_url: str = DEFAULT_BASE_URL
    # 默认模型：必须是服务商真实模型名（deepseek-chat）。
    # 历史默认值 Model.V4_FLASH（"deepseek-v4-flash"）为虚构名，
    # DeepSeek API 对无效模型名静默返回空 content（HTTP 200），引发重试风暴。
    default_model: Model | str = "deepseek-chat"
    max_retries: int = 3  # 含首次共 max_retries+1 次尝试
    retry_base_delay: float = 1.0  # 首次重试延迟（秒），指数退避
    retry_max_delay: float = 30.0  # 最大重试延迟
    timeout: float = 300.0  # 单次请求超时（秒），reasoner 模型需要更长推理时间
    max_tokens: int = 16384  # 足够大避免 JSON 截断（模型不支持时 API 会自动截断）
    temperature: float = 0.0  # 默认 0 保证确定性输出（流水线场景）


def _get_config(api_key: str | None, config: LLMConfig | None) -> LLMConfig:
    """合并显式 api_key 与 LLMConfig，最终从环境变量兜底。

    读取顺序：显式参数 > LLM_API_KEY > DEEPSEEK_API_KEY（向后兼容）
    """
    cfg = config or LLMConfig()
    if api_key:
        cfg.api_key = api_key
    if not cfg.api_key:
        cfg.api_key = os.environ.get("LLM_API_KEY", "") or os.environ.get(
            "DEEPSEEK_API_KEY", ""
        )
    if not cfg.api_key:
        raise ConfigurationError(
            "未配置 API Key。请传入 api_key 参数，"
            "或设置 LLM_API_KEY 环境变量（.env 文件中配置）。"
        )
    return cfg


def _is_retryable_api_error(e: Exception) -> bool:
    """判断 OpenAI SDK 异常是否可重试（429 限流 / 5xx 服务端 / 超时）。"""
    if isinstance(e, (APITimeoutError, RateLimitError)):
        return True
    if isinstance(e, APIError):
        status = getattr(e, "status_code", None) or getattr(
            getattr(e, "response", None), "status_code", None
        )
        if status is not None and (status == 429 or status >= 500):
            return True
    return False


def _status_from_error(e: Exception) -> int | None:
    """从 OpenAI SDK 异常提取 HTTP 状态码。"""
    if isinstance(e, APIError):
        status = getattr(e, "status_code", None)
        if status:
            return status
        resp = getattr(e, "response", None)
        return getattr(resp, "status_code", None)
    return None


class DeepSeekClient:
    """LLM 客户端（同步 + 异步），兼容所有 OpenAI 格式 API。

    封装 OpenAI SDK，提供 JSON Output / 模型切换 / 重试 / 超时。
    流水线 Stage 调用方只需关心 system_prompt + user_prompt，得到 dict 结果。

    可通过 LLMClient 别名使用：
        from kbrefiner.core.llm import LLMClient
        client = LLMClient()
    """

    def __init__(
        self,
        api_key: str | None = None,
        config: LLMConfig | None = None,
    ):
        self._config = _get_config(api_key, config)
        self._sync_client: OpenAI | None = None
        self._async_client: AsyncOpenAI | None = None
        # 累计 Token 消耗（实例生命周期内所有成功调用，含重试）
        self.usage_total: dict[str, int] = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        }

    def _accumulate_usage(self, usage: dict | None) -> None:
        """把一次成功调用的 usage 累加进 usage_total。"""
        if not usage:
            return
        for key in self.usage_total:
            self.usage_total[key] += int(usage.get(key) or 0)

    @property
    def config(self) -> LLMConfig:
        return self._config

    # =================================================================
    # 同步接口
    # =================================================================

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Model | str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """同步调用，返回纯文本结果。

        Args:
            system_prompt: 系统提示
            user_prompt: 用户提示
            model: 模型，None 用配置默认值
            temperature: 温度，None 用配置默认值（0.0）
            max_tokens: 最大输出 token，None 用配置默认值

        Returns:
            ChatResult: content 是纯文本
        """
        return self._call_sync(
            system_prompt, user_prompt, model, temperature, max_tokens, json_mode=False
        )

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Model | str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """同步调用 JSON Output 模式，返回解析后的 dict。

        自动处理 DeepSeek 已知的「JSON Output 返回空 content」问题：
        空 content 触发 EmptyContentError 并自动重试。

        Returns:
            ChatResult: parsed 是 dict，content 是原始 JSON 字符串

        Raises:
            JsonParseError: 重试耗尽后仍无法解析 JSON
            ApiError: 重试耗尽后 API 仍失败
            ConfigurationError: API Key 未配置
        """
        result = self._call_sync(
            system_prompt, user_prompt, model, temperature, max_tokens, json_mode=True
        )
        # 解析 JSON
        result.parsed = self._parse_json(result.content)
        return result

    # =================================================================
    # 异步接口
    # =================================================================

    async def chat_async(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Model | str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """异步调用，返回纯文本结果。"""
        return await self._call_async(
            system_prompt, user_prompt, model, temperature, max_tokens, json_mode=False
        )

    async def chat_json_async(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Model | str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """异步调用 JSON Output 模式，返回解析后的 dict。"""
        result = await self._call_async(
            system_prompt, user_prompt, model, temperature, max_tokens, json_mode=True
        )
        result.parsed = self._parse_json(result.content)
        return result

    # =================================================================
    # 内部实现
    # =================================================================

    def _get_sync_client(self) -> OpenAI:
        if self._sync_client is None:
            self._sync_client = OpenAI(
                api_key=self._config.api_key,
                base_url=self._config.base_url,
                timeout=self._config.timeout,
            )
        return self._sync_client

    def _get_async_client(self) -> AsyncOpenAI:
        if self._async_client is None:
            self._async_client = AsyncOpenAI(
                api_key=self._config.api_key,
                base_url=self._config.base_url,
                timeout=self._config.timeout,
            )
        return self._async_client

    def _resolve_model(self, model: Model | str | None) -> str:
        if model is None:
            dm = self._config.default_model
            if isinstance(dm, Model):
                return dm.value
            return str(dm)
        if isinstance(model, Model):
            return model.value
        return model

    def _build_messages(
        self, system_prompt: str, user_prompt: str
    ) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _call_sync(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Model | str | None,
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
    ) -> ChatResult:
        """同步调用核心，含重试逻辑。"""
        model_name = self._resolve_model(model)
        temp = temperature if temperature is not None else self._config.temperature
        tokens = max_tokens if max_tokens is not None else self._config.max_tokens
        messages = self._build_messages(system_prompt, user_prompt)

        last_error: Exception | None = None
        total_attempts = self._config.max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": temp,
                    "max_tokens": tokens,
                }
                # 不使用 response_format={'type': 'json_object'}，
                # DeepSeek JSON Output 模式在复杂 prompt 下有返回空 content 的已知问题。
                # 改为普通模式输出，由 _parse_json() 手动解析（容错 markdown 代码块包裹）。

                client = self._get_sync_client()
                response = client.chat.completions.create(**kwargs)

                content = response.choices[0].message.content or ""

                if not content.strip():
                    raise EmptyContentError(
                        f"API 返回空 content（attempt {attempt}/{total_attempts}）"
                    )

                usage = {}
                if response.usage:
                    usage = {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    }
                self._accumulate_usage(usage)

                return ChatResult(
                    content=content,
                    model=model_name,
                    usage=usage,
                    raw=response,
                    attempts=attempt,
                )

            except EmptyContentError as e:
                last_error = e
                logger.warning(
                    "空 content，将重试 (model=%s, attempt %d/%d): %s"
                    "（若持续为空，请检查 LLM_MODEL 是否为服务商真实模型名，"
                    "DeepSeek 对无效模型名会静默返回空 content）",
                    model_name, attempt, total_attempts, e,
                )
                self._sleep_for_retry(attempt)
            except Exception as e:
                last_error = e
                if _is_retryable_api_error(e) and attempt < total_attempts:
                    status = _status_from_error(e)
                    logger.warning(
                        "API 调用失败 (status=%s, attempt %d/%d)，将重试: %s",
                        status, attempt, total_attempts, e,
                    )
                    self._sleep_for_retry(attempt)
                else:
                    status = _status_from_error(e)
                    retryable = _is_retryable_api_error(e)
                    raise ApiError(
                        f"API 调用失败: {e}",
                        status_code=status,
                        retryable=retryable,
                    ) from e

        # 重试耗尽
        if isinstance(last_error, EmptyContentError):
            raise EmptyContentError(
                f"重试 {self._config.max_retries} 次后仍返回空 content"
            ) from last_error
        raise ApiError(
            f"重试 {self._config.max_retries} 次后仍失败: {last_error}",
            retryable=False,
        ) from last_error

    async def _call_async(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Model | str | None,
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
    ) -> ChatResult:
        """异步调用核心，含重试逻辑。"""
        model_name = self._resolve_model(model)
        temp = temperature if temperature is not None else self._config.temperature
        tokens = max_tokens if max_tokens is not None else self._config.max_tokens
        messages = self._build_messages(system_prompt, user_prompt)

        last_error: Exception | None = None
        total_attempts = self._config.max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": temp,
                    "max_tokens": tokens,
                }
                # 不使用 response_format={'type': 'json_object'}，
                # DeepSeek JSON Output 模式在复杂 prompt 下有返回空 content 的已知问题。
                # 改为普通模式输出，由 _parse_json() 手动解析（容错 markdown 代码块包裹）。

                client = self._get_async_client()
                response = await client.chat.completions.create(**kwargs)

                content = response.choices[0].message.content or ""

                if not content.strip():
                    raise EmptyContentError(
                        f"API 返回空 content（attempt {attempt}/{total_attempts}）"
                    )

                usage = {}
                if response.usage:
                    usage = {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    }
                self._accumulate_usage(usage)

                return ChatResult(
                    content=content,
                    model=model_name,
                    usage=usage,
                    raw=response,
                    attempts=attempt,
                )

            except EmptyContentError as e:
                last_error = e
                logger.warning(
                    "空 content，将重试 (model=%s, attempt %d/%d): %s"
                    "（若持续为空，请检查 LLM_MODEL 是否为服务商真实模型名，"
                    "DeepSeek 对无效模型名会静默返回空 content）",
                    model_name, attempt, total_attempts, e,
                )
                await self._async_sleep_for_retry(attempt)
            except Exception as e:
                last_error = e
                if _is_retryable_api_error(e) and attempt < total_attempts:
                    status = _status_from_error(e)
                    logger.warning(
                        "API 调用失败 (status=%s, attempt %d/%d)，将重试: %s",
                        status, attempt, total_attempts, e,
                    )
                    await self._async_sleep_for_retry(attempt)
                else:
                    status = _status_from_error(e)
                    retryable = _is_retryable_api_error(e)
                    raise ApiError(
                        f"API 调用失败: {e}",
                        status_code=status,
                        retryable=retryable,
                    ) from e

        if isinstance(last_error, EmptyContentError):
            raise EmptyContentError(
                f"重试 {self._config.max_retries} 次后仍返回空 content"
            ) from last_error
        raise ApiError(
            f"重试 {self._config.max_retries} 次后仍失败: {last_error}",
            retryable=False,
        ) from last_error

    # =================================================================
    # JSON 解析
    # =================================================================

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        """解析 JSON 字符串，容错处理 markdown 代码块包裹。"""
        text = content.strip()
        # 容错：LLM 有时会用 ```json ... ``` 包裹
        if text.startswith("```"):
            # 去掉首行 ```json 和末尾 ```
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            # 容错：LLM 偶尔在字符串值内输出裸控制字符（换行/制表符，
            # 报错 "Invalid control character"），strict=False 允许后即可解析。
            # 该场景在含表格的规范类文档（SLA/SOP 多行单元格）中高发。
            try:
                parsed = json.loads(text, strict=False)
            except json.JSONDecodeError:
                raise JsonParseError(
                    f"无法解析为 JSON: {e}",
                    raw_content=content,
                ) from e

        if not isinstance(parsed, dict):
            raise JsonParseError(
                f"JSON 顶层不是对象，而是 {type(parsed).__name__}",
                raw_content=content,
            )
        return parsed

    # =================================================================
    # 重试退避
    # =================================================================

    def _sleep_for_retry(self, attempt: int) -> None:
        """指数退避同步等待。"""
        delay = min(
            self._config.retry_base_delay * (2 ** (attempt - 1)),
            self._config.retry_max_delay,
        )
        time.sleep(delay)

    async def _async_sleep_for_retry(self, attempt: int) -> None:
        """指数退避异步等待。"""
        delay = min(
            self._config.retry_base_delay * (2 ** (attempt - 1)),
            self._config.retry_max_delay,
        )
        await asyncio.sleep(delay)

    # =================================================================
    # 资源清理
    # =================================================================

    def close(self) -> None:
        """关闭客户端连接。"""
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None
        # AsyncOpenAI 没有同步 close，由 aclose 异步关闭
        self._async_client = None

    async def aclose(self) -> None:
        """异步关闭客户端连接。"""
        if self._async_client is not None:
            await self._async_client.close()
            self._async_client = None
        self._sync_client = None


# LLMClient 别名：推荐使用的通用名称
LLMClient = DeepSeekClient


__all__ = [
    "DeepSeekClient",
    "LLMClient",
    "Model",
    "LLMConfig",
    "ChatResult",
    "DEFAULT_BASE_URL",
]
