"""流水线编排公共接口。

用法：
    from app.core.pipeline import Pipeline, PipelineConfig
    from app.core.llm import DeepSeekClient

    client = DeepSeekClient(api_key="sk-xxx")
    pipeline = Pipeline(client, PipelineConfig(output_dir=Path("./output")))
    doc = await pipeline.run(markdown, document_source="policy.pdf")
"""
from app.core.pipeline.orchestrator import (
    Pipeline,
    PipelineConfig,
    ProgressCallback,
    StageCompleteCallback,
)
from app.core.pipeline.postprocess import merge_exception_lists, merge_stages_to_document
from app.core.pipeline.stage_runner import (
    DEFAULT_STAGE_RETRIES,
    StageValidationError,
    run_stage1,
    run_stage2,
    run_stage3,
    run_stage4,
)

__all__ = [
    # 编排器
    "Pipeline",
    "PipelineConfig",
    "ProgressCallback",
    "StageCompleteCallback",
    # Stage 执行器
    "run_stage1",
    "run_stage2",
    "run_stage3",
    "run_stage4",
    "StageValidationError",
    "DEFAULT_STAGE_RETRIES",
    # 后处理
    "merge_exception_lists",
    "merge_stages_to_document",
]
