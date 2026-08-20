"""DeepSeek LLM 客户端单元测试。

全部用 mock 模拟 OpenAI SDK 的 chat.completions.create，
不发起真实网络请求。

覆盖：
- 模型切换（V4_FLASH 默认 / V4_PRO 显式指定 / 字符串模型名）
- JSON Output 解析（标准 JSON / markdown 代码块包裹 / 顶层非对象）
- 空 content 自动重试（DeepSeek 已知问题）
- 限流 429 自动重试（指数退避）
- 5xx 服务端错误自动重试
- 4xx 客户端错误不重试直接抛出
- 超时自动重试
- 配置错误（API Key 缺失）
- 异步接口
- ChatResult 字段完整性

运行：python -m unittest tests.test_llm -v
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

from openai import APIError, APITimeoutError, RateLimitError

from kbrefiner.core.llm import (
    ApiError,
    ChatResult,
    ConfigurationError,
    DeepSeekClient,
    EmptyContentError,
    JsonParseError,
    LLMConfig,
    Model,
)
from kbrefiner.core.llm.deepseek_client import _is_retryable_api_error


# =====================================================================
# Mock 工具
# =====================================================================


def _make_response(
    content: str = "",
    model: str = "deepseek-v4-flash",
    prompt_tokens: int = 100,
    completion_tokens: int = 200,
):
    """构造一个模拟的 OpenAI ChatCompletion 响应对象。"""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.usage = MagicMock(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    response.model = model
    return response


def _make_api_error(status_code: int, message: str = "error") -> APIError:
    """构造一个模拟的 OpenAI APIError。"""
    return APIError(
        message=message,
        request=MagicMock(),
        body=None,
    )


def _make_client(api_key: str = "test-key", **config_kwargs) -> DeepSeekClient:
    """构造测试用客户端，跳过环境变量读取。"""
    cfg = LLMConfig(
        api_key=api_key,
        max_retries=config_kwargs.get("max_retries", 2),
        retry_base_delay=0.01,  # 测试用极小延迟
        retry_max_delay=0.05,
        timeout=10.0,
    )
    return DeepSeekClient(config=cfg)


# =====================================================================
# 配置与初始化测试
# =====================================================================


class TestConfiguration(unittest.TestCase):
    """配置与初始化。"""

    def test_missing_api_key_raises(self):
        """API Key 未配置应抛出 ConfigurationError。"""
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigurationError):
                DeepSeekClient()

    def test_api_key_from_param(self):
        client = DeepSeekClient(api_key="param-key")
        self.assertEqual(client.config.api_key, "param-key")

    def test_api_key_from_env(self):
        with patch.dict("os.environ", {"LLM_API_KEY": "env-key"}):
            client = DeepSeekClient()
            self.assertEqual(client.config.api_key, "env-key")

    def test_default_model_is_deepseek_chat(self):
        # 默认必须是服务商真实模型名；虚构名（deepseek-v4-flash）会被
        # DeepSeek API 静默返回空 content，引发重试风暴
        client = DeepSeekClient(api_key="k")
        self.assertEqual(client.config.default_model, "deepseek-chat")

    def test_custom_default_model(self):
        cfg = LLMConfig(api_key="k", default_model=Model.V4_PRO)
        client = DeepSeekClient(config=cfg)
        self.assertEqual(client.config.default_model, Model.V4_PRO)


# =====================================================================
# 模型切换测试
# =====================================================================


class TestModelSwitching(unittest.TestCase):
    """模型切换。"""

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_default_model_deepseek_chat(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response("hello")

        client = _make_client()
        client.chat("sys", "usr")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        self.assertEqual(call_kwargs["model"], "deepseek-chat")

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_explicit_v4_pro(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response("hello")

        client = _make_client()
        client.chat("sys", "usr", model=Model.V4_PRO)

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        self.assertEqual(call_kwargs["model"], "deepseek-v4-pro")

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_string_model_name(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response("hello")

        client = _make_client()
        client.chat("sys", "usr", model="deepseek-v4-pro")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        self.assertEqual(call_kwargs["model"], "deepseek-v4-pro")


# =====================================================================
# JSON Output 测试
# =====================================================================


class TestJsonOutput(unittest.TestCase):
    """JSON Output 模式。"""

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_json_mode_sets_response_format(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response(
            '{"a": 1, "b": "text"}'
        )

        client = _make_client()
        client.chat_json("sys", "usr")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        # 不再使用 response_format json_object（DeepSeek 复杂 prompt 下返回空 content）
        self.assertNotIn("response_format", call_kwargs)

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_plain_chat_no_response_format(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response("plain text")

        client = _make_client()
        client.chat("sys", "usr")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        self.assertNotIn("response_format", call_kwargs)

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_json_parsed_to_dict(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response(
            '{"name": "test", "value": 42}'
        )

        client = _make_client()
        result = client.chat_json("sys", "usr")

        self.assertIsInstance(result.parsed, dict)
        self.assertEqual(result.parsed["name"], "test")
        self.assertEqual(result.parsed["value"], 42)

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_json_with_markdown_code_block(self, mock_openai_cls):
        """LLM 用 ```json ... ``` 包裹 JSON 时应能正确解析。"""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response(
            '```json\n{"key": "value"}\n```'
        )

        client = _make_client()
        result = client.chat_json("sys", "usr")

        self.assertEqual(result.parsed["key"], "value")

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_json_parse_error_on_invalid_json(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response(
            "这不是JSON"
        )

        client = _make_client()
        with self.assertRaises(JsonParseError) as ctx:
            client.chat_json("sys", "usr")
        self.assertEqual(ctx.exception.raw_content, "这不是JSON")

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_json_parse_error_on_non_object_top_level(self, mock_openai_cls):
        """JSON 顶层是数组而非对象应报错。"""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response(
            '[1, 2, 3]'
        )

        client = _make_client()
        with self.assertRaises(JsonParseError):
            client.chat_json("sys", "usr")


# =====================================================================
# 空 content 重试测试
# =====================================================================


class TestEmptyContentRetry(unittest.TestCase):
    """DeepSeek 已知问题：JSON Output 返回空 content 自动重试。"""

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_empty_content_retries_then_succeeds(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        # 第 1 次空 content，第 2 次正常
        mock_client.chat.completions.create.side_effect = [
            _make_response(""),
            _make_response('{"ok": true}'),
        ]

        client = _make_client(max_retries=3)
        result = client.chat_json("sys", "usr")

        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.parsed["ok"], True)
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_empty_content_exhausts_retries(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response("")

        client = _make_client(max_retries=2)
        with self.assertRaises(EmptyContentError):
            client.chat_json("sys", "usr")
        # 1 首次 + 2 重试 = 3 次
        self.assertEqual(mock_client.chat.completions.create.call_count, 3)


# =====================================================================
# API 错误重试测试
# =====================================================================


class TestApiErrorRetry(unittest.TestCase):
    """API 错误的重试策略。"""

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_rate_limit_429_retries(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        error = RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429),
            body=None,
        )
        mock_client.chat.completions.create.side_effect = [
            error,
            _make_response("ok"),
        ]

        client = _make_client(max_retries=2)
        result = client.chat("sys", "usr")

        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.content, "ok")

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_timeout_retries(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        error = APITimeoutError(request=MagicMock())
        mock_client.chat.completions.create.side_effect = [
            error,
            _make_response("ok"),
        ]

        client = _make_client(max_retries=2)
        result = client.chat("sys", "usr")
        self.assertEqual(result.attempts, 2)

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_5xx_retries(self, mock_openai_cls):
        """500/502/503 等服务端错误应重试。"""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        # 用 APIError 模拟 503
        error = APIError(
            message="service unavailable",
            request=MagicMock(),
            body=None,
        )
        error.status_code = 503
        mock_client.chat.completions.create.side_effect = [
            error,
            _make_response("ok"),
        ]

        client = _make_client(max_retries=2)
        result = client.chat("sys", "usr")
        self.assertEqual(result.attempts, 2)

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_4xx_no_retry(self, mock_openai_cls):
        """400/401/403 等客户端错误不重试，直接抛出。"""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        error = APIError(
            message="bad request",
            request=MagicMock(),
            body=None,
        )
        error.status_code = 400
        mock_client.chat.completions.create.return_value = error
        mock_client.chat.completions.create.side_effect = error

        client = _make_client(max_retries=3)
        with self.assertRaises(ApiError):
            client.chat("sys", "usr")
        # 只调用 1 次，不重试
        self.assertEqual(mock_client.chat.completions.create.call_count, 1)

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_retries_exhausted_raises_api_error(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        error = APITimeoutError(request=MagicMock())
        mock_client.chat.completions.create.side_effect = error

        client = _make_client(max_retries=2)
        with self.assertRaises(ApiError):
            client.chat("sys", "usr")
        self.assertEqual(mock_client.chat.completions.create.call_count, 3)


# =====================================================================
# is_retryable 判断测试
# =====================================================================


class TestIsRetryable(unittest.TestCase):
    """_is_retryable_api_error 判断逻辑。"""

    def test_rate_limit_retryable(self):
        error = RateLimitError(
            message="x", response=MagicMock(status_code=429), body=None
        )
        self.assertTrue(_is_retryable_api_error(error))

    def test_timeout_retryable(self):
        error = APITimeoutError(request=MagicMock())
        self.assertTrue(_is_retryable_api_error(error))

    def test_5xx_retryable(self):
        error = APIError(message="x", request=MagicMock(), body=None)
        error.status_code = 502
        self.assertTrue(_is_retryable_api_error(error))

    def test_4xx_not_retryable(self):
        error = APIError(message="x", request=MagicMock(), body=None)
        error.status_code = 401
        self.assertFalse(_is_retryable_api_error(error))


# =====================================================================
# ChatResult 完整性测试
# =====================================================================


class TestChatResult(unittest.TestCase):
    """ChatResult 字段完整性。"""

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_result_fields(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response(
            '{"x": 1}', model="deepseek-v4-pro", prompt_tokens=50, completion_tokens=100
        )

        client = _make_client()
        result = client.chat_json("sys", "usr", model=Model.V4_PRO)

        self.assertEqual(result.content, '{"x": 1}')
        self.assertEqual(result.parsed, {"x": 1})
        self.assertEqual(result.model, "deepseek-v4-pro")
        self.assertEqual(result.usage["prompt_tokens"], 50)
        self.assertEqual(result.usage["completion_tokens"], 100)
        self.assertEqual(result.usage["total_tokens"], 150)
        self.assertEqual(result.attempts, 1)

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_messages_structure(self, mock_openai_cls):
        """验证传给 API 的 messages 结构正确。"""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response("ok")

        client = _make_client()
        client.chat("系统提示", "用户提示")

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        messages = call_kwargs["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0], {"role": "system", "content": "系统提示"})
        self.assertEqual(messages[1], {"role": "user", "content": "用户提示"})


# =====================================================================
# 异步接口测试
# =====================================================================


class TestAsyncInterface(unittest.TestCase):
    """异步接口。"""

    @patch("kbrefiner.core.llm.deepseek_client.AsyncOpenAI")
    def test_async_chat_json(self, mock_async_cls):
        mock_client = MagicMock()
        mock_async_cls.return_value = mock_client
        # async create 返回 MagicMock 时需要用 async mock
        async_mock = MagicMock()
        async_mock.chat.completions.create = MagicMock(
            return_value=_make_response('{"async": true}')
        )
        # AsyncOpenAI 的 create 是 async 方法，需要包装
        async def async_create(**kwargs):
            return _make_response('{"async": true}')

        mock_client.chat.completions.create = async_create

        client = _make_client()
        result = asyncio.run(client.chat_json_async("sys", "usr"))

        self.assertEqual(result.parsed["async"], True)

    @patch("kbrefiner.core.llm.deepseek_client.AsyncOpenAI")
    def test_async_empty_content_retry(self, mock_async_cls):
        mock_client = MagicMock()
        mock_async_cls.return_value = mock_client

        call_count = [0]

        async def async_create(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response("")
            return _make_response('{"ok": 1}')

        mock_client.chat.completions.create = async_create

        client = _make_client(max_retries=3)
        result = asyncio.run(client.chat_json_async("sys", "usr"))

        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.parsed["ok"], 1)


# =====================================================================
# 超时参数传递测试
# =====================================================================


class TestTimeoutPassing(unittest.TestCase):
    """超时参数正确传递给 OpenAI SDK。"""

    @patch("kbrefiner.core.llm.deepseek_client.OpenAI")
    def test_timeout_passed_to_client(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_response("ok")

        cfg = LLMConfig(api_key="k", timeout=30.0)
        client = DeepSeekClient(config=cfg)
        client.chat("sys", "usr")

        # OpenAI 构造时应传入 timeout
        _, kwargs = mock_openai_cls.call_args
        self.assertEqual(kwargs["timeout"], 30.0)


if __name__ == "__main__":
    unittest.main()
