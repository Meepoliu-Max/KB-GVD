"""数据模型公共接口。

用法：
    from kbrefiner.models import KbDocument, Stage1Output, Stage2Output
"""
from kbrefiner.models.schemas import (
    CHUNK_MAX_CHARS,
    CHUNK_MIN_CHARS,
    DocType,
    DocumentInfo,
    ExceptionList,
    KbDocument,
    KnowledgeAtom,
    LOW_CONFIDENCE_THRESHOLD,
    Metadata,
    QaPair,
    QualitySummary,
    SENSITIVE_MARKER,
    SENSITIVE_MASK,
    SUMMARY_MAX_CHARS,
)
from kbrefiner.models.stage_schemas import (
    Stage1Input,
    Stage1Output,
    Stage2Chunk,
    Stage2Input,
    Stage2Output,
    Stage3Chunk,
    Stage3Input,
    Stage3Output,
    Stage4Chunk,
    Stage4Input,
    Stage4Output,
    stage2_to_stage3_input,
    stage2_to_stage4_input,
)

__all__ = [
    # 顶层 Schema
    "KbDocument",
    "DocumentInfo",
    "KnowledgeAtom",
    "Metadata",
    "QaPair",
    "ExceptionList",
    "QualitySummary",
    "DocType",
    # 常量
    "CHUNK_MIN_CHARS",
    "CHUNK_MAX_CHARS",
    "LOW_CONFIDENCE_THRESHOLD",
    "SUMMARY_MAX_CHARS",
    "SENSITIVE_MARKER",
    "SENSITIVE_MASK",
    # Stage Schema
    "Stage1Input",
    "Stage1Output",
    "Stage2Input",
    "Stage2Chunk",
    "Stage2Output",
    "Stage3Input",
    "Stage3Chunk",
    "Stage3Output",
    "Stage4Input",
    "Stage4Chunk",
    "Stage4Output",
    # 辅助函数
    "stage2_to_stage3_input",
    "stage2_to_stage4_input",
]
