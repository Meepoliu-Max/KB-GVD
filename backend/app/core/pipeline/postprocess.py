"""后处理：合并 Stage 2/3/4 输出 → 最终 KbDocument。

流水线 4 阶段产出 3 份中间结果（Stage 1 产出 document_info.doc_type，Stage 2 产出 chunks，Stage 3 产出 qa_pairs，Stage 4 产出 metadata），需要 merge 为顶层 KbDocument。

merge 步骤：
1. 以 Stage 2 的 chunks 为基准（chunk_id 顺序）
2. 按 chunk_id 匹配 Stage 3 的 qa_pairs 和 Stage 4 的 metadata
3. 合并为 KnowledgeAtom 列表
4. 归集 4 个 Stage 的 exception_list（9 字段累加去重）
5. 用 validation 模块重算 quality_summary（不可信 LLM 输出的数值）
6. 组装 KbDocument

异常处理：
- chunk_id 不匹配（Stage 3/4 缺少某个 chunk）→ 登记到 exception_list.missing_info
"""
from __future__ import annotations

import logging
from datetime import date

from app.core.validation import run as run_validation
from app.models import (
    DocType,
    DocumentInfo,
    ExceptionList,
    KbDocument,
    KnowledgeAtom,
    QualitySummary,
    Stage1Output,
    Stage2Output,
    Stage3Output,
    Stage4Output,
)

logger = logging.getLogger(__name__)

# exception_list 9 字段名
_EXCEPTION_FIELDS = [
    "content_conflicts",
    "missing_info",
    "vague_items",
    "expired_items",
    "chunk_anomalies",
    "truncated_items",
    "sensitive_items",
    "low_confidence_qa",
    "terminology_pending",
]


def merge_exception_lists(*exception_lists: ExceptionList) -> ExceptionList:
    """合并多个 exception_list，9 字段各自累加去重。

    Args:
        exception_lists: 任意数量的 ExceptionList（来自 4 个 Stage）

    Returns:
        合并后的 ExceptionList
    """
    merged_data: dict[str, list[str]] = {f: [] for f in _EXCEPTION_FIELDS}

    for exc in exception_lists:
        for field in _EXCEPTION_FIELDS:
            items = getattr(exc, field, [])
            for item in items:
                if item not in merged_data[field]:  # 去重
                    merged_data[field].append(item)

    return ExceptionList(**merged_data)


def merge_stages_to_document(
    stage1: Stage1Output,
    stage2: Stage2Output,
    stage3: Stage3Output,
    stage4: Stage4Output,
    document_source: str,
) -> KbDocument:
    """合并 4 个 Stage 输出为最终 KbDocument。

    Args:
        stage1: Stage 1 输出（doc_type / sensitive_items / terminology_pending）
        stage2: Stage 2 输出（chunks: chunk_id/title/content/remark）
        stage3: Stage 3 输出（chunks: chunk_id/qa_pairs）
        stage4: Stage 4 输出（chunks: chunk_id/doc_type/metadata/remark）
        document_source: 原始文档来源（文件名）

    Returns:
        KbDocument: 最终文档，quality_summary 已用 validation 模块重算
    """
    # 1. 以 Stage 2 chunks 为基准，构建 chunk_id → 数据的索引
    stage2_by_id = {c.chunk_id: c for c in stage2.chunks}
    stage3_by_id = {c.chunk_id: c for c in stage3.chunks}
    stage4_by_id = {c.chunk_id: c for c in stage4.chunks}

    # 用于收集 merge 过程中发现的异常
    merge_missing: list[str] = []

    knowledge_atoms: list[KnowledgeAtom] = []

    for chunk_id, s2_chunk in stage2_by_id.items():
        # 匹配 Stage 3 的 qa_pairs
        s3_chunk = stage3_by_id.get(chunk_id)
        if s3_chunk is None:
            merge_missing.append(f"{chunk_id}: Stage 3 缺少 qa_pairs")
            qa_pairs = []
        else:
            qa_pairs = s3_chunk.qa_pairs

        # 匹配 Stage 4 的 metadata
        s4_chunk = stage4_by_id.get(chunk_id)
        if s4_chunk is None:
            merge_missing.append(f"{chunk_id}: Stage 4 缺少 metadata")
            # 构造一个全【人工补全项】的默认 metadata
            from app.models import Metadata
            metadata = Metadata(
                target_audience="【人工补全项】",
                business_module="【人工补全项】",
                knowledge_type="【人工补全项】",
                version_timeliness="【人工补全项】",
                summary="【人工补全项】",
            )
            doc_type = stage1.doc_type
            remark = s2_chunk.remark + "；Stage 4 缺少 metadata"
        else:
            metadata = s4_chunk.metadata
            doc_type = s4_chunk.doc_type
            # 合并 remark（Stage 2 + Stage 4）
            remarks = [r for r in [s2_chunk.remark, s4_chunk.remark] if r.strip()]
            remark = "；".join(remarks)

        atom = KnowledgeAtom(
            chunk_id=chunk_id,
            title=s2_chunk.title,
            content=s2_chunk.content,
            doc_type=doc_type,
            metadata=metadata,
            qa_pairs=qa_pairs,
            remark=remark,
        )
        knowledge_atoms.append(atom)

    # 2. 归集 exception_list（Stage 1/2/3/4 + merge 缺失）
    # Stage 1 的 exception_list 字段分散在 sensitive_items 和 terminology_pending
    stage1_exc = ExceptionList(
        sensitive_items=stage1.sensitive_items,
        terminology_pending=stage1.terminology_pending,
    )
    merged_exc = merge_exception_lists(
        stage1_exc,
        stage2.exception_list,
        stage3.exception_list,
        stage4.exception_list,
    )
    # 追加 merge 过程的缺失
    if merge_missing:
        merged_exc.missing_info.extend(merge_missing)

    # 3. 组装 document_info
    total_chunks = len(knowledge_atoms)
    total_qa_pairs = sum(len(a.qa_pairs) for a in knowledge_atoms)
    document_info = DocumentInfo(
        source=document_source,
        doc_type=stage1.doc_type,
        total_chunks=total_chunks,
        total_qa_pairs=total_qa_pairs,
        processing_date=date.today().isoformat(),
    )

    # 4. 初始 quality_summary（后面用 validation 重算覆盖）
    quality_summary = QualitySummary(
        avg_confidence=0.0,
        low_confidence_count=0,
        exception_count=0,
        needs_review=False,
    )

    # 5. 组装 KbDocument
    doc = KbDocument(
        document_info=document_info,
        knowledge_atoms=knowledge_atoms,
        exception_list=merged_exc,
        quality_summary=quality_summary,
    )

    # 6. 用 validation 模块重算 quality_summary（不可信 LLM 输出的数值）
    doc_dict = doc.model_dump()
    report = run_validation(doc_dict)
    # 用修正后的 data 重建 doc
    doc = KbDocument.model_validate(report.data)

    logger.info(
        "merge 完成: %d chunks, %d QA pairs, %d exceptions",
        total_chunks, total_qa_pairs, doc.total_exception_count(),
    )

    return doc


__all__ = [
    "merge_exception_lists",
    "merge_stages_to_document",
]
