"""数值精确计算与修正（迁移自 kb-preprocess/scripts/calc_metrics.py）。

变更点：
- 解耦 sys.argv：calc(data) → (data, MetricsResult)
- 不原地覆盖写文件，返回修正后的 data 与 fixes 列表
- 计算逻辑与原脚本完全一致，对齐 SKILL.md quality_summary 4 字段 + document_info 2 字段
"""
from __future__ import annotations

from typing import Any

from kbrefiner.core.validation.types import MetricsResult


def calc(data: dict[str, Any]) -> tuple[dict[str, Any], MetricsResult]:
    """精确计算所有数值指标，与 LLM 输出对比并自动修正。

    修正项：avg_confidence / low_confidence_count / exception_count /
    needs_review / total_qa_pairs / total_chunks

    Returns:
        (修正后的 data, MetricsResult)
    """
    atoms = data.get("knowledge_atoms", [])
    el = data.get("exception_list", {})

    # 1. avg_confidence: 所有 QA 的 confidence_score 平均值
    all_scores: list[int] = []
    low_conf_count = 0
    for atom in atoms:
        for qa in atom.get("qa_pairs", []):
            score = qa.get("confidence_score")
            if score is not None:
                all_scores.append(score)
                if score < 80:
                    low_conf_count += 1

    avg_confidence = round(sum(all_scores) / len(all_scores), 1) if all_scores else 0.0

    # 2. exception_count: exception_list 中所有数组的元素总数
    exception_count = sum(
        len(el[key]) for key in el if isinstance(el.get(key), list)
    )

    # 3. needs_review
    needs_review = exception_count > 0

    # 4. coverage_rate: 知识原子被 QA 有效覆盖的比例
    # 一个原子被"有效覆盖"= 至少有 1 条 QA 且 answer 不是「文档未说明」
    total_atoms = len(atoms)
    covered_atoms = 0
    for atom in atoms:
        qa_pairs = atom.get("qa_pairs", [])
        has_effective_qa = any(
            qa.get("answer", "").strip()
            and qa.get("answer", "").strip() != "文档未说明"
            for qa in qa_pairs
        )
        if has_effective_qa:
            covered_atoms += 1

    coverage_rate = round(covered_atoms / total_atoms, 2) if total_atoms > 0 else 0.0

    calculated = {
        "avg_confidence": avg_confidence,
        "low_confidence_count": low_conf_count,
        "exception_count": exception_count,
        "coverage_rate": coverage_rate,
        "needs_review": needs_review,
        "total_qa_count": len(all_scores),
    }

    # 对比并修正
    qs = data.get("quality_summary", {})
    di = data.get("document_info", {})
    fixes: list[str] = []

    if qs.get("avg_confidence") != avg_confidence:
        fixes.append(f"avg_confidence: LLM={qs.get('avg_confidence')} → 修正为{avg_confidence}")
        qs["avg_confidence"] = avg_confidence

    if qs.get("low_confidence_count") != low_conf_count:
        fixes.append(
            f"low_confidence_count: LLM={qs.get('low_confidence_count')} → 修正为{low_conf_count}"
        )
        qs["low_confidence_count"] = low_conf_count

    if qs.get("exception_count") != exception_count:
        fixes.append(f"exception_count: LLM={qs.get('exception_count')} → 修正为{exception_count}")
        qs["exception_count"] = exception_count

    if qs.get("coverage_rate") != coverage_rate:
        fixes.append(f"coverage_rate: LLM={qs.get('coverage_rate')} → 修正为{coverage_rate}")
        qs["coverage_rate"] = coverage_rate

    if qs.get("needs_review") != needs_review:
        fixes.append(f"needs_review: LLM={qs.get('needs_review')} → 修正为{needs_review}")
        qs["needs_review"] = needs_review

    # total_qa_pairs
    calc_total_qa = sum(len(a.get("qa_pairs", [])) for a in atoms)
    if di.get("total_qa_pairs") != calc_total_qa:
        fixes.append(f"total_qa_pairs: LLM={di.get('total_qa_pairs')} → 修正为{calc_total_qa}")
        di["total_qa_pairs"] = calc_total_qa

    # total_chunks
    calc_chunks = len(atoms)
    if di.get("total_chunks") != calc_chunks:
        fixes.append(f"total_chunks: LLM={di.get('total_chunks')} → 修正为{calc_chunks}")
        di["total_chunks"] = calc_chunks

    data["quality_summary"] = qs
    data["document_info"] = di

    return data, MetricsResult(calculated=calculated, fixes=fixes)
