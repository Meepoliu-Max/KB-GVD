"""单 Stage 执行器：渲染 Prompt → 调用 LLM → 断点校验 → 失败重试。

每个 Stage 的执行流程：
1. 用 prompts 模块渲染 system/user prompt
2. 调用 DeepSeekClient.chat_json_async（JSON Output 模式）
3. 用对应 StageXOutput Pydantic Schema 做断点校验
4. 校验失败 → 收集错误信息 → retry_errors 回灌 Prompt → 重新调用 LLM
5. 重试耗尽 → 抛出 StageValidationError
6. 成功 → 返回 StageXOutput 对象

设计要点：
- 泛型 _run_stage_with_retry 统一处理「调用 LLM + 断点校验 + 重试」
- 4 个 run_stageN 函数只负责组装参数（渲染函数 + 校验 Schema）
- 重试时把上一次的 raw JSON 输出也回灌（previous_output），让 LLM 增量修正
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Type, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.llm import DeepSeekClient, Model
from app.core.pipeline.prompts import (
    render_stage1,
    render_stage2,
    render_stage3,
    render_stage4,
)
from app.models import (
    DocType,
    Stage1Input,
    Stage1Output,
    Stage2Input,
    Stage2Output,
    Stage3Input,
    Stage3Output,
    Stage4Input,
    Stage4Output,
)

logger = logging.getLogger(__name__)

# Pydantic 模型类型变量
T = TypeVar("T", bound=BaseModel)

# 默认断点校验重试次数（不含首次）
DEFAULT_STAGE_RETRIES = 2


class StageValidationError(Exception):
    """Stage 断点校验失败且重试耗尽。"""

    def __init__(self, stage_name: str, errors: list[str], raw_output: str | None = None):
        self.stage_name = stage_name
        self.errors = errors
        self.raw_output = raw_output
        super().__init__(
            f"{stage_name} 断点校验失败（重试 {len(errors)} 次后仍不通过）: "
            f"{'; '.join(errors[:3])}"
        )


def _extract_validation_errors(e: ValidationError) -> list[str]:
    """从 Pydantic ValidationError 提取人类可读的错误列表。"""
    errors: list[str] = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"]
        errors.append(f"{loc}: {msg}")
    return errors


async def _run_stage_with_retry(
    stage_name: str,
    render_fn,
    render_kwargs: dict[str, Any],
    output_schema: Type[T],
    llm_client: DeepSeekClient,
    model: Model | str | None = None,
    max_retries: int = DEFAULT_STAGE_RETRIES,
    on_progress=None,
) -> T:
    """通用 Stage 执行器：渲染 → 调用 LLM → 断点校验 → 重试。

    Args:
        stage_name: Stage 名称（日志/错误信息用）
        render_fn: 渲染函数（render_stage1/2/3/4 之一）
        render_kwargs: 渲染函数的关键字参数
        output_schema: 输出 Pydantic Schema 类（Stage1Output 等）
        llm_client: DeepSeek 客户端
        model: LLM 模型（None 用客户端默认）
        max_retries: 断点校验失败后的重试次数（不含首次，默认 2）
        on_progress: 可选回调 fn(stage_name, attempt, status) → None

    Returns:
        校验通过的 StageXOutput 对象

    Raises:
        StageValidationError: 重试耗尽后仍校验失败
    """
    total_attempts = max_retries + 1
    retry_errors: list[str] | None = None
    previous_output: str | None = None
    last_errors: list[str] = []

    for attempt in range(1, total_attempts + 1):
        # 1. 渲染 Prompt（首次无 retry_errors，重试时注入上次错误）
        system_prompt, user_prompt = render_fn(
            retry_errors=retry_errors,
            previous_output=previous_output,
            **render_kwargs,
        )

        if on_progress:
            on_progress(stage_name, attempt, "calling_llm")

        # 2. 调用 LLM（JSON Output 模式）
        try:
            result = await llm_client.chat_json_async(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
            )
        except Exception as e:
            logger.error("%s LLM 调用失败 (attempt %d): %s", stage_name, attempt, e)
            if attempt < total_attempts:
                retry_errors = [f"LLM 调用异常: {e}"]
                previous_output = None
                continue
            raise StageValidationError(
                stage_name, [f"LLM 调用失败: {e}"], None
            ) from e

        raw_json = result.content

        # 3. 断点校验
        if on_progress:
            on_progress(stage_name, attempt, "validating")

        try:
            validated = output_schema.model_validate(result.parsed)
            if on_progress:
                on_progress(stage_name, attempt, "success")
            logger.info(
                "%s 断点校验通过 (attempt %d, tokens=%s)",
                stage_name, attempt, result.usage,
            )
            return validated

        except ValidationError as e:
            last_errors = _extract_validation_errors(e)
            logger.warning(
                "%s 断点校验失败 (attempt %d/%d): %s",
                stage_name, attempt, total_attempts, last_errors,
            )
            if attempt < total_attempts:
                retry_errors = last_errors
                previous_output = raw_json
                if on_progress:
                    on_progress(stage_name, attempt, "retrying")
            else:
                raise StageValidationError(
                    stage_name, last_errors, raw_json
                ) from e

    # 理论上不会走到这里
    raise StageValidationError(stage_name, last_errors, previous_output)


# =====================================================================
# 4 个 Stage 对外接口
# =====================================================================


async def run_stage1(
    stage_input: Stage1Input,
    llm_client: DeepSeekClient,
    model: Model | str | None = None,
    max_retries: int = DEFAULT_STAGE_RETRIES,
    on_progress=None,
) -> Stage1Output:
    """Stage 1：文档类型识别 + 敏感预扫描 + 术语检查。"""
    return await _run_stage_with_retry(
        stage_name="Stage1-Clean",
        render_fn=render_stage1,
        render_kwargs={
            "markdown": stage_input.markdown,
            "document_source": stage_input.document_source,
            "doc_title": stage_input.doc_title,
        },
        output_schema=Stage1Output,
        llm_client=llm_client,
        model=model,
        max_retries=max_retries,
        on_progress=on_progress,
    )


async def run_stage2(
    stage_input: Stage2Input,
    llm_client: DeepSeekClient,
    model: Model | str | None = None,
    max_retries: int = DEFAULT_STAGE_RETRIES,
    on_progress=None,
) -> Stage2Output:
    """Stage 2：语义级智能分块。"""
    return await _run_stage_with_retry(
        stage_name="Stage2-Chunk",
        render_fn=render_stage2,
        render_kwargs={
            "cleaned_text": stage_input.cleaned_text,
            "doc_type": stage_input.doc_type.value,
            "partition_prefix": stage_input.partition_prefix,
            "title_context": stage_input.title_context,
        },
        output_schema=Stage2Output,
        llm_client=llm_client,
        model=model,
        max_retries=max_retries,
        on_progress=on_progress,
    )


async def run_stage3(
    stage_input: Stage3Input,
    llm_client: DeepSeekClient,
    model: Model | str | None = None,
    max_retries: int = DEFAULT_STAGE_RETRIES,
    on_progress=None,
) -> Stage3Output:
    """Stage 3：多视角口语化 QA 生成 + 脱敏。"""
    return await _run_stage_with_retry(
        stage_name="Stage3-QA",
        render_fn=render_stage3,
        render_kwargs={
            "chunks": stage_input.chunks,
            "doc_type": stage_input.doc_type.value,
            "sensitive_items": stage_input.sensitive_items,
            "chunk_id_prefix": stage_input.chunk_id_prefix,
        },
        output_schema=Stage3Output,
        llm_client=llm_client,
        model=model,
        max_retries=max_retries,
        on_progress=on_progress,
    )


async def run_stage4(
    stage_input: Stage4Input,
    llm_client: DeepSeekClient,
    model: Model | str | None = None,
    max_retries: int = DEFAULT_STAGE_RETRIES,
    on_progress=None,
) -> Stage4Output:
    """Stage 4：多维元数据自动打标。"""
    return await _run_stage_with_retry(
        stage_name="Stage4-Tag",
        render_fn=render_stage4,
        render_kwargs={
            "chunks": stage_input.chunks,
            "doc_type": stage_input.doc_type.value,
        },
        output_schema=Stage4Output,
        llm_client=llm_client,
        model=model,
        max_retries=max_retries,
        on_progress=on_progress,
    )


__all__ = [
    "run_stage1",
    "run_stage2",
    "run_stage3",
    "run_stage4",
    "StageValidationError",
    "DEFAULT_STAGE_RETRIES",
]
