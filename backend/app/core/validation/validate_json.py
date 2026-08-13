"""JSON 格式校验（迁移自 kb-preprocess/scripts/validate_json.py）。

变更点：
- 解耦 sys.argv：validate(data, raw_text=None) → ValidationResult
- 中文引号检查改为可选 raw_text 参数（原脚本从 sys.argv[1] 文件读取）
- 不写 .validate_result.json，结果由调用方处理
- 校验逻辑与原脚本完全一致，对齐 SKILL.md 8 条格式红线
"""
from __future__ import annotations

import re
from typing import Any

from app.core.validation.types import ValidationResult

# chunk_id 格式：P{N}-C{NNN}
_CHUNK_ID_RE = re.compile(r"^P\d+-C\d+$")
# title 需带【上下文归属】
_TITLE_RE = re.compile(r"^【.+】")
# Answer 疑似建议性表述
_SUGGESTION_PATTERNS = ["建议联系", "建议您", "可以尝试", "可以联系"]
# 中文引号
_CN_QUOTES = ("\u201c", "\u201d", "\u2018", "\u2019")


def validate(data: dict[str, Any], raw_text: str | None = None) -> ValidationResult:
    """校验 LLM 输出的 JSON 结构与字段格式。

    Args:
        data: 解析后的 JSON dict
        raw_text: 原始 JSON 文本（可选，用于中文引号检测）

    Returns:
        ValidationResult: valid / errors / warnings
    """
    errors: list[str] = []
    warnings: list[str] = []

    # 1. 顶层结构
    required_top = ["document_info", "knowledge_atoms", "exception_list", "quality_summary"]
    for key in required_top:
        if key not in data:
            errors.append(f"缺少顶层字段: {key}")

    if "document_info" not in data:
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    # 2. document_info
    di = data["document_info"]
    for key in ["source", "doc_type", "total_chunks", "total_qa_pairs", "processing_date"]:
        if key not in di:
            errors.append(f"document_info缺少字段: {key}")

    # 3. knowledge_atoms
    atoms = data.get("knowledge_atoms", [])
    if len(atoms) == 0:
        errors.append("knowledge_atoms为空")

    # 3a. 数量一致性
    if di.get("total_chunks") != len(atoms):
        errors.append(
            f"total_chunks({di.get('total_chunks')})与实际知识原子数({len(atoms)})不一致"
        )

    total_qa = 0
    for i, atom in enumerate(atoms):
        prefix = f"知识原子[{i}]"

        # chunk_id 格式
        chunk_id = atom.get("chunk_id", "")
        if not _CHUNK_ID_RE.match(chunk_id):
            errors.append(f"{prefix} chunk_id格式错误: '{chunk_id}'，应为P{{N}}-C{{NNN}}")

        # title 格式（软性，warning）
        title = atom.get("title", "")
        if title and not _TITLE_RE.match(title):
            warnings.append(f"{prefix} title缺少【上下文归属】: '{title}'")

        # 必填字段
        for key in ["chunk_id", "title", "content", "doc_type", "metadata", "qa_pairs", "remark"]:
            if key not in atom:
                errors.append(f"{prefix} 缺少字段: {key}")

        # metadata 字段
        meta = atom.get("metadata", {})
        for key in [
            "target_audience", "business_module", "knowledge_type",
            "version_timeliness", "summary",
        ]:
            if key not in meta:
                errors.append(f"{prefix}.metadata 缺少字段: {key}")

        # summary 长度（软性）
        summary = meta.get("summary", "")
        if summary and len(summary) > 20:
            warnings.append(f"{prefix} summary超过20字({len(summary)}字): '{summary}'")

        # qa_pairs
        qas = atom.get("qa_pairs", [])
        total_qa += len(qas)
        for j, qa in enumerate(qas):
            qprefix = f"{prefix} QA[{j}]"
            for key in ["question", "answer", "keywords", "confidence_score", "remark"]:
                if key not in qa:
                    errors.append(f"{qprefix} 缺少字段: {key}")

            score = qa.get("confidence_score")
            if score is not None and not (0 <= score <= 100):
                errors.append(f"{qprefix} confidence_score超出范围: {score}")

            if len(qa.get("keywords", [])) == 0:
                warnings.append(f"{qprefix} keywords为空")

            answer = qa.get("answer", "")
            for pat in _SUGGESTION_PATTERNS:
                if pat in answer and "文档未说明" not in answer:
                    warnings.append(f"{qprefix} Answer可能含有原文外的建议: 包含'{pat}'")

    # 3b. total_qa_pairs 一致性
    if di.get("total_qa_pairs") != total_qa:
        errors.append(
            f"total_qa_pairs({di.get('total_qa_pairs')})与实际QA数({total_qa})不一致"
        )

    # 4. exception_list（9 个字段，均为数组）
    el = data.get("exception_list", {})
    el_required = [
        "content_conflicts", "missing_info", "vague_items", "expired_items",
        "chunk_anomalies", "truncated_items", "sensitive_items",
        "low_confidence_qa", "terminology_pending",
    ]
    for key in el_required:
        if key not in el:
            errors.append(f"exception_list缺少字段: {key}")
        elif not isinstance(el.get(key), list):
            errors.append(f"exception_list.{key} 不是数组")

    # 5. quality_summary
    qs = data.get("quality_summary", {})
    for key in ["avg_confidence", "low_confidence_count", "exception_count", "needs_review"]:
        if key not in qs:
            errors.append(f"quality_summary缺少字段: {key}")

    if "needs_review" in qs and not isinstance(qs["needs_review"], bool):
        errors.append(f"quality_summary.needs_review 不是布尔值: {qs['needs_review']}")

    # 6. 中文引号检查（可选，需调用方传入原始文本）
    if raw_text is not None:
        if any(q in raw_text for q in _CN_QUOTES):
            warnings.append("JSON中包含中文引号（""''），应使用ASCII引号")

    return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)
