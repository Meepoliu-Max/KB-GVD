"""4 个 Stage 的输入输出 Pydantic Schema。

用于流水线断点校验：每个 Stage 的 LLM 输出用对应的 Output Schema 校验，
解析失败 → 触发重试（retry_errors 回灌 Prompt）。

设计原则：
- 输入 Schema 用 dataclass（轻量类型提示，不强校验，因为输入来自程序内部）
- 输出 Schema 用 Pydantic BaseModel（强校验 LLM 输出，extra='ignore' 容错）
- 复用顶层 schemas.py 中的 DocType / Metadata / QaPair / ExceptionList
- 各 Stage 输出的 chunk 是中间结构（非最终 KnowledgeAtom），只含本阶段产出的字段
- exception_list 9 字段始终完整（无异常填空数组），便于后处理 merge

Schema 与 Prompt 模板输出格式严格对齐：
- Stage 1: stage1_clean.j2 → Stage1Output
- Stage 2: stage2_chunk.j2 → Stage2Output
- Stage 3: stage3_qa.j2   → Stage3Output
- Stage 4: stage4_tag.j2 → Stage4Output
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kbrefiner.models.schemas import (
    CHUNK_ID_PATTERN,
    DocType,
    ExceptionList,
    Metadata,
    QaPair,
)


# =====================================================================
# Stage 1：文档类型识别 + 敏感预扫描 + 术语检查
# =====================================================================


@dataclass
class Stage1Input:
    """Stage 1 输入（程序内部传递，不强校验）。"""

    markdown: str
    document_source: str
    doc_title: str | None = None


class Stage1Output(BaseModel):
    """Stage 1 输出（LLM 输出断点校验）。

    对应 stage1_clean.j2 的 JSON 输出格式：
    {
      "cleaned_text": "...",
      "doc_type": "制度合规|FAQ|产品活动|技术运维|教学知识",
      "sensitive_items": ["..."],
      "terminology_pending": ["..."]
    }
    """

    model_config = ConfigDict(extra="ignore")

    cleaned_text: str = Field(description="原文 Markdown，敏感数据处已标记")
    doc_type: DocType = Field(description="文档类型枚举")
    sensitive_items: list[str] = Field(
        default_factory=list,
        description="敏感数据清单，格式：位置+类型+原始值片段",
    )
    terminology_pending: list[str] = Field(
        default_factory=list,
        description="术语不一致清单，格式：称谓A / 称谓B 可能指同一实体",
    )

    @field_validator("cleaned_text")
    @classmethod
    def _check_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("cleaned_text 不能为空")
        return v


# =====================================================================
# Stage 2：语义级智能分块
# =====================================================================


@dataclass
class Stage2Input:
    """Stage 2 输入。"""

    cleaned_text: str
    doc_type: DocType
    partition_prefix: str
    title_context: str | None = None


class Stage2Chunk(BaseModel):
    """Stage 2 产出的单个分块（中间结构，无 qa_pairs / metadata）。"""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(description="格式 P{N}-C{NNN}，如 P1-C001")
    title: str = Field(description="格式【业务模块】- 具体内容")
    content: str = Field(description="从 cleaned_text 原文摘录，300~800 字目标区间")
    remark: str = Field(default="", description="无异常为空；偏离区间含【拆分颗粒度异常】")

    @field_validator("chunk_id")
    @classmethod
    def _check_chunk_id_format(cls, v: str) -> str:
        if not CHUNK_ID_PATTERN.match(v):
            raise ValueError(
                f"chunk_id 格式错误: '{v}'，应为 P{{N}}-C{{NNN}}（如 P1-C001）"
            )
        return v

    @field_validator("content")
    @classmethod
    def _check_content_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("chunk content 不能为空")
        return v


class Stage2Output(BaseModel):
    """Stage 2 输出。

    对应 stage2_chunk.j2 的 JSON 输出格式：
    {
      "chunks": [{chunk_id, title, content, remark}],
      "exception_list": {9字段}
    }
    """

    model_config = ConfigDict(extra="ignore")

    chunks: list[Stage2Chunk] = Field(description="分块列表，至少 1 个")
    exception_list: ExceptionList = Field(default_factory=ExceptionList)

    @field_validator("chunks")
    @classmethod
    def _check_chunks_not_empty(cls, v: list[Stage2Chunk]) -> list[Stage2Chunk]:
        if not v:
            raise ValueError("chunks 不能为空")
        return v


# =====================================================================
# Stage 3：多视角口语化 QA 生成 + 脱敏
# =====================================================================


@dataclass
class Stage3Input:
    """Stage 3 输入。"""

    chunks: list[dict[str, Any]]  # Stage 2 的 chunks（含 chunk_id/title/content）
    doc_type: DocType
    sensitive_items: list[str]
    chunk_id_prefix: str = "P1"


class Stage3Chunk(BaseModel):
    """Stage 3 产出的单个分块（追加 qa_pairs，不含 metadata）。"""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(description="原样复制 Stage 2 传入的 chunk_id")
    qa_pairs: list[QaPair] = Field(description="3~5 条口语化 QA")

    @field_validator("chunk_id")
    @classmethod
    def _check_chunk_id_format(cls, v: str) -> str:
        if not CHUNK_ID_PATTERN.match(v):
            raise ValueError(
                f"chunk_id 格式错误: '{v}'，应为 P{{N}}-C{{NNN}}"
            )
        return v

    @field_validator("qa_pairs")
    @classmethod
    def _check_qa_not_empty(cls, v: list[QaPair]) -> list[QaPair]:
        if not v:
            raise ValueError("qa_pairs 不能为空（每个 chunk 至少 3 条 QA）")
        return v


class Stage3Output(BaseModel):
    """Stage 3 输出。

    对应 stage3_qa.j2 的 JSON 输出格式：
    {
      "chunks": [{chunk_id, qa_pairs: [{question, answer, keywords, confidence_score, remark}]}],
      "exception_list": {9字段}
    }
    """

    model_config = ConfigDict(extra="ignore")

    chunks: list[Stage3Chunk] = Field(description="QA 列表，长度与输入 chunks 相同")
    exception_list: ExceptionList = Field(default_factory=ExceptionList)

    @field_validator("chunks")
    @classmethod
    def _check_chunks_not_empty(cls, v: list[Stage3Chunk]) -> list[Stage3Chunk]:
        if not v:
            raise ValueError("chunks 不能为空")
        return v


# =====================================================================
# Stage 4：多维元数据自动打标
# =====================================================================


@dataclass
class Stage4Input:
    """Stage 4 输入。"""

    chunks: list[dict[str, Any]]  # Stage 2 的 chunks（与 Stage 3 相同输入，可并行）
    doc_type: DocType


class Stage4Chunk(BaseModel):
    """Stage 4 产出的单个分块（追加 metadata + doc_type，不含 qa_pairs）。"""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(description="原样复制 Stage 2 传入的 chunk_id")
    doc_type: DocType = Field(description="原样复制传入的文档类型")
    metadata: Metadata = Field(description="5 字段缺一不可")
    remark: str = Field(default="", description="通常为空；过期内容或需人工注意时备注")

    @field_validator("chunk_id")
    @classmethod
    def _check_chunk_id_format(cls, v: str) -> str:
        if not CHUNK_ID_PATTERN.match(v):
            raise ValueError(
                f"chunk_id 格式错误: '{v}'，应为 P{{N}}-C{{NNN}}"
            )
        return v


class Stage4Output(BaseModel):
    """Stage 4 输出。

    对应 stage4_tag.j2 的 JSON 输出格式：
    {
      "chunks": [{chunk_id, doc_type, metadata: {5字段}, remark}],
      "exception_list": {9字段}
    }
    """

    model_config = ConfigDict(extra="ignore")

    chunks: list[Stage4Chunk] = Field(description="打标列表，长度与输入 chunks 相同")
    exception_list: ExceptionList = Field(default_factory=ExceptionList)

    @field_validator("chunks")
    @classmethod
    def _check_chunks_not_empty(cls, v: list[Stage4Chunk]) -> list[Stage4Chunk]:
        if not v:
            raise ValueError("chunks 不能为空")
        return v


# =====================================================================
# Stage 间数据流转辅助函数
# =====================================================================


def stage2_to_stage3_input(stage2_output: Stage2Output, doc_type: DocType,
                           sensitive_items: list[str]) -> Stage3Input:
    """把 Stage 2 输出转为 Stage 3 输入。"""
    chunks = [
        {
            "chunk_id": c.chunk_id,
            "title": c.title,
            "content": c.content,
        }
        for c in stage2_output.chunks
    ]
    # 从任意 chunk_id 提取 partition_prefix（如 P1-C001 → P1）
    prefix = stage2_output.chunks[0].chunk_id.split("-")[0] if stage2_output.chunks else "P1"
    return Stage3Input(
        chunks=chunks,
        doc_type=doc_type,
        sensitive_items=sensitive_items,
        chunk_id_prefix=prefix,
    )


def stage2_to_stage4_input(stage2_output: Stage2Output, doc_type: DocType) -> Stage4Input:
    """把 Stage 2 输出转为 Stage 4 输入（与 Stage 3 输入相同，可并行）。"""
    chunks = [
        {
            "chunk_id": c.chunk_id,
            "title": c.title,
            "content": c.content,
        }
        for c in stage2_output.chunks
    ]
    return Stage4Input(chunks=chunks, doc_type=doc_type)


__all__ = [
    # Stage 1
    "Stage1Input",
    "Stage1Output",
    # Stage 2
    "Stage2Input",
    "Stage2Chunk",
    "Stage2Output",
    # Stage 3
    "Stage3Input",
    "Stage3Chunk",
    "Stage3Output",
    # Stage 4
    "Stage4Input",
    "Stage4Chunk",
    "Stage4Output",
    # 辅助函数
    "stage2_to_stage3_input",
    "stage2_to_stage4_input",
]
