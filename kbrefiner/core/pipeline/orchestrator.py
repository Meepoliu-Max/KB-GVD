"""流水线编排器：串联 4 阶段 + Stage 3/4 并行 + 持久化 + 断点续跑。

执行流程：
  Stage 1 (Clean)  →  Stage 2 (Chunk)  →  ┌─ Stage 3 (QA)   ─┐  →  Postprocess (Merge)  →  Final
                                          └─ Stage 4 (Tag)  ─┘
                                              （并行 asyncio.gather）

特性：
- 中间结果持久化：每个 Stage 成功后存 JSON 到 output_dir
- 断点续跑：检测到已存在的中间结果则跳过该 Stage
- 进度回调：on_stage_complete(stage_name, output) 可选
- 异常处理：Stage 失败抛出，中间结果已保存可续跑

用法：
    from kbrefiner.core.pipeline import Pipeline, PipelineConfig
    from kbrefiner.core.llm import DeepSeekClient, Model

    client = DeepSeekClient(api_key="sk-xxx")
    config = PipelineConfig(
        output_dir=Path("./output"),
        stage3_model=Model.V4_PRO,  # Stage 3 用复杂模型
    )
    pipeline = Pipeline(client, config)

    # 完整执行
    doc = await pipeline.run(markdown, document_source="policy.pdf")

    # 断点续跑（output_dir 已有中间结果）
    doc = await pipeline.run(markdown, document_source="policy.pdf")
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kbrefiner.core.llm import DeepSeekClient, Model
from kbrefiner.core.parser import ParsedDocument
from kbrefiner.core.pipeline.postprocess import merge_stages_to_document
from kbrefiner.core.pipeline.stage_runner import (
    DEFAULT_STAGE_RETRIES,
    StageValidationError,
    run_stage1,
    run_stage2,
    run_stage3,
    run_stage4,
)
from kbrefiner.models import (
    DocType,
    KbDocument,
    Stage1Input,
    Stage1Output,
    Stage2Input,
    Stage2Output,
    Stage3Input,
    Stage3Output,
    Stage4Input,
    Stage4Output,
    stage2_to_stage3_input,
    stage2_to_stage4_input,
)

logger = logging.getLogger(__name__)

# 进度回调类型：(stage_name, attempt, status) -> None
ProgressCallback = Callable[[str, int, str], None]
# Stage 完成回调类型：(stage_name, output) -> None
StageCompleteCallback = Callable[[str, Any], None]


@dataclass
class PipelineConfig:
    """流水线配置。

    Attributes:
        output_dir: 中间结果与最终结果输出目录
        stage_retries: 断点校验失败重试次数（不含首次）
        stage1_model: Stage 1 模型（None 用客户端默认）
        stage2_model: Stage 2 模型
        stage3_model: Stage 3 模型（建议 Pro，QA 生成复杂）
        stage4_model: Stage 4 模型
        enable_checkpoint: 是否启用断点续跑（检测已存在的中间结果）
        partition_prefix: 分片编号前缀（默认 P1，多文档时按 P2/P3 递增）
        stage34_batch_size: Stage 3/4 分批大小。chunks 超过该值时拆成
            多批并行调用 LLM 再合并（大文档显著提速），0 或 1 表示不分批
        stage34_concurrency: Stage 3/4 批间最大并行数
    """

    output_dir: Path = field(default_factory=lambda: Path("./output"))
    stage_retries: int = DEFAULT_STAGE_RETRIES
    stage1_model: Model | str | None = None
    stage2_model: Model | str | None = None
    stage3_model: Model | str | None = None
    stage4_model: Model | str | None = None
    enable_checkpoint: bool = True
    partition_prefix: str = "P1"
    stage34_batch_size: int = 4
    stage34_concurrency: int = 4


class Pipeline:
    """知识库预处理流水线编排器。"""

    def __init__(self, llm_client: DeepSeekClient, config: PipelineConfig | None = None):
        self._client = llm_client
        self._config = config or PipelineConfig()
        self._output_dir = self._config.output_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def config(self) -> PipelineConfig:
        return self._config

    # =================================================================
    # 主入口
    # =================================================================

    async def run(
        self,
        markdown: str,
        document_source: str,
        doc_title: str | None = None,
        on_progress: ProgressCallback | None = None,
        on_stage_complete: StageCompleteCallback | None = None,
    ) -> KbDocument:
        """执行完整流水线。

        Args:
            markdown: MinerU 解析后的 Markdown 文本
            document_source: 原始文档来源（文件名）
            doc_title: 文档标题（可选，默认用 document_source）
            on_progress: 进度回调
            on_stage_complete: 每个 Stage 完成回调

        Returns:
            最终 KbDocument
        """
        logger.info("流水线启动: source=%s", document_source)

        # Stage 1
        stage1_output = await self._exec_stage1(
            markdown, document_source, doc_title, on_progress, on_stage_complete
        )

        # Stage 2
        stage2_output = await self._exec_stage2(
            stage1_output, on_progress, on_stage_complete
        )

        # Stage 3 + Stage 4 并行
        stage3_output, stage4_output = await self._exec_stage3_and_4_parallel(
            stage1_output, stage2_output, on_progress, on_stage_complete
        )

        # Postprocess: merge 4 阶段输出
        logger.info("开始后处理 merge")
        doc = merge_stages_to_document(
            stage1=stage1_output,
            stage2=stage2_output,
            stage3=stage3_output,
            stage4=stage4_output,
            document_source=document_source,
        )

        # 持久化最终结果
        final_path = self._output_dir / "final.json"
        final_path.write_text(
            doc.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("流水线完成，最终结果已保存: %s", final_path)

        if on_stage_complete:
            on_stage_complete("Postprocess", doc)

        return doc

    # =================================================================
    # 各 Stage 执行（含断点续跑）
    # =================================================================

    async def _exec_stage1(
        self,
        markdown: str,
        document_source: str,
        doc_title: str | None,
        on_progress: ProgressCallback | None,
        on_stage_complete: StageCompleteCallback | None,
    ) -> Stage1Output:
        # 断点续跑检查
        checkpoint = self._load_checkpoint("stage1")
        if checkpoint is not None:
            logger.info("Stage 1 命中断点，跳过 LLM 调用")
            output = Stage1Output.model_validate(checkpoint)
            if on_stage_complete:
                on_stage_complete("Stage1-Clean", output)
            return output

        stage_input = Stage1Input(
            markdown=markdown,
            document_source=document_source,
            doc_title=doc_title,
        )
        output = await run_stage1(
            stage_input, self._client,
            model=self._config.stage1_model,
            max_retries=self._config.stage_retries,
            on_progress=on_progress,
        )
        self._save_checkpoint("stage1", output)
        if on_stage_complete:
            on_stage_complete("Stage1-Clean", output)
        return output

    async def _exec_stage2(
        self,
        stage1_output: Stage1Output,
        on_progress: ProgressCallback | None,
        on_stage_complete: StageCompleteCallback | None,
    ) -> Stage2Output:
        checkpoint = self._load_checkpoint("stage2")
        if checkpoint is not None:
            logger.info("Stage 2 命中断点，跳过 LLM 调用")
            output = Stage2Output.model_validate(checkpoint)
            if on_stage_complete:
                on_stage_complete("Stage2-Chunk", output)
            return output

        stage_input = Stage2Input(
            cleaned_text=stage1_output.cleaned_text,
            doc_type=stage1_output.doc_type,
            partition_prefix=self._config.partition_prefix,
        )
        output = await run_stage2(
            stage_input, self._client,
            model=self._config.stage2_model,
            max_retries=self._config.stage_retries,
            on_progress=on_progress,
        )
        self._save_checkpoint("stage2", output)
        if on_stage_complete:
            on_stage_complete("Stage2-Chunk", output)
        return output

    async def _exec_stage3_and_4_parallel(
        self,
        stage1_output: Stage1Output,
        stage2_output: Stage2Output,
        on_progress: ProgressCallback | None,
        on_stage_complete: StageCompleteCallback | None,
    ) -> tuple[Stage3Output, Stage4Output]:
        """Stage 3 和 Stage 4 并行执行（输入相同）。

        Stage 3/4 输出 token 量与 chunk 数成正比，是流水线耗时大头。
        chunks 较多时按 stage34_batch_size 拆批并行调用 LLM，再按批顺序
        合并 chunks 与 exception_list，墙钟时间从 O(N) 降为 O(N/批次并行度)。
        """
        # 准备两个 Stage 的输入
        stage3_input = stage2_to_stage3_input(
            stage2_output, stage1_output.doc_type, stage1_output.sensitive_items
        )
        stage4_input = stage2_to_stage4_input(
            stage2_output, stage1_output.doc_type
        )

        # 断点续跑检查
        s3_checkpoint = self._load_checkpoint("stage3")
        s4_checkpoint = self._load_checkpoint("stage4")

        async def _run_stage3():
            if s3_checkpoint is not None:
                logger.info("Stage 3 命中断点，跳过 LLM 调用")
                output = Stage3Output.model_validate(s3_checkpoint)
                if on_stage_complete:
                    on_stage_complete("Stage3-QA", output)
                return output
            batches = self._split_chunks(stage3_input.chunks)
            if len(batches) > 1:
                logger.info("Stage 3 分批并行: %d chunks → %d 批", len(stage3_input.chunks), len(batches))
            outputs = await self._run_batches_parallel(
                batches,
                lambda batch: run_stage3(
                    Stage3Input(
                        chunks=batch,
                        doc_type=stage3_input.doc_type,
                        sensitive_items=stage3_input.sensitive_items,
                        chunk_id_prefix=stage3_input.chunk_id_prefix,
                    ),
                    self._client,
                    model=self._config.stage3_model,
                    max_retries=self._config.stage_retries,
                    on_progress=on_progress,
                ),
                merge=lambda parts: Stage3Output(
                    chunks=[c for p in parts for c in p.chunks],
                    exception_list=self._merge_exception_lists(p.exception_list for p in parts),
                ),
                stage_name="Stage3-QA",
            )
            self._save_checkpoint("stage3", outputs)
            if on_stage_complete:
                on_stage_complete("Stage3-QA", outputs)
            return outputs

        async def _run_stage4():
            if s4_checkpoint is not None:
                logger.info("Stage 4 命中断点，跳过 LLM 调用")
                output = Stage4Output.model_validate(s4_checkpoint)
                if on_stage_complete:
                    on_stage_complete("Stage4-Tag", output)
                return output
            batches = self._split_chunks(stage4_input.chunks)
            if len(batches) > 1:
                logger.info("Stage 4 分批并行: %d chunks → %d 批", len(stage4_input.chunks), len(batches))
            outputs = await self._run_batches_parallel(
                batches,
                lambda batch: run_stage4(
                    Stage4Input(
                        chunks=batch,
                        doc_type=stage4_input.doc_type,
                    ),
                    self._client,
                    model=self._config.stage4_model,
                    max_retries=self._config.stage_retries,
                    on_progress=on_progress,
                ),
                merge=lambda parts: Stage4Output(
                    chunks=[c for p in parts for c in p.chunks],
                    exception_list=self._merge_exception_lists(p.exception_list for p in parts),
                ),
                stage_name="Stage4-Tag",
            )
            self._save_checkpoint("stage4", outputs)
            if on_stage_complete:
                on_stage_complete("Stage4-Tag", outputs)
            return outputs

        # 并行执行
        logger.info("Stage 3 / Stage 4 并行执行")
        stage3_output, stage4_output = await asyncio.gather(
            _run_stage3(), _run_stage4()
        )
        return stage3_output, stage4_output

    # =================================================================
    # Stage 3/4 分批并行辅助
    # =================================================================

    def _split_chunks(self, chunks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """按 stage34_batch_size 将 chunks 切成有序批次（保持原顺序）。"""
        size = self._config.stage34_batch_size
        if size <= 1 or len(chunks) <= size:
            return [chunks]
        return [chunks[i:i + size] for i in range(0, len(chunks), size)]

    async def _run_batches_parallel(
        self,
        batches: list[list[dict[str, Any]]],
        run_batch,
        merge,
        stage_name: str,
    ):
        """并行执行各批次并合并结果。单批失败则整阶段失败（与不分批行为一致）。"""
        if len(batches) == 1:
            return await run_batch(batches[0])
        semaphore = asyncio.Semaphore(max(1, self._config.stage34_concurrency))

        async def _guarded(batch):
            async with semaphore:
                return await run_batch(batch)

        parts = await asyncio.gather(*[_guarded(b) for b in batches])
        merged = merge(parts)
        logger.info("%s 分批合并完成: %d 批 → %d chunks", stage_name, len(batches), len(merged.chunks))
        return merged

    @staticmethod
    def _merge_exception_lists(exception_lists):
        """合并多批的 exception_list（9 个字段均为字符串列表，直接拼接）。"""
        from kbrefiner.models.schemas import ExceptionList

        result = ExceptionList()
        for el in exception_lists:
            for field_name in type(result).model_fields:
                items = getattr(el, field_name, None) or []
                getattr(result, field_name).extend(items)
        return result

    # =================================================================
    # 持久化（断点续跑）
    # =================================================================

    def _save_checkpoint(self, stage_name: str, output: Any) -> None:
        """保存 Stage 中间结果到 output_dir/{stage_name}.json。"""
        if not self._config.enable_checkpoint:
            return
        path = self._output_dir / f"{stage_name}.json"
        if hasattr(output, "model_dump_json"):
            data = output.model_dump_json(indent=2, ensure_ascii=False)
        else:
            data = json.dumps(output, ensure_ascii=False, indent=2, default=str)
        path.write_text(data, encoding="utf-8")
        logger.info("已保存 %s 中间结果: %s", stage_name, path)

    def _load_checkpoint(self, stage_name: str) -> dict | None:
        """加载已存在的中间结果，不存在返回 None。"""
        if not self._config.enable_checkpoint:
            return None
        path = self._output_dir / f"{stage_name}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            logger.info("加载 %s 中间结果: %s", stage_name, path)
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("加载 %s 中间结果失败，将重新执行: %s", stage_name, e)
            return None

    def clear_checkpoints(self) -> None:
        """清除所有中间结果（重新执行流水线时调用）。"""
        for stage_name in ["stage1", "stage2", "stage3", "stage4", "final"]:
            path = self._output_dir / f"{stage_name}.json"
            if path.exists():
                path.unlink()
                logger.info("已删除 %s", path)


__all__ = [
    "Pipeline",
    "PipelineConfig",
    "ProgressCallback",
    "StageCompleteCallback",
]
