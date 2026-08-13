"""人工复核清单生成（迁移自 kb-preprocess/scripts/format_review.py）。

变更点：
- 解耦 sys.argv：generate(data) → ReviewResult
- 不写 .review_summary.md 文件，返回文本
- 自然语言转换逻辑与原脚本完全一致，去 chunk_id 编号，6 类异常分组
"""
from __future__ import annotations

import re
from typing import Any

from app.core.validation.types import ReviewResult

# chunk_id 前缀（如 P1-C001: 或 P1-C001-Q3:）
_CHUNK_PREFIX_RE = re.compile(r"^P\d+-C\d+(-Q\d+)?:\s*")
# 置信分数字
_SCORE_RE = re.compile(r"置信分\d+-?")
# 各类【】标记
_BRACKET_RE = re.compile(r"【[^】]*】")
# 从条目提取 chunk_id
_CHUNK_ID_EXTRACT_RE = re.compile(r"(P\d+-C\d+)")
# title 去掉【业务模块】- 前缀
_TITLE_PREFIX_RE = re.compile(r"^【[^】]*】\s*[-–]\s*")


def _remove_chunk_prefix(text: str) -> str:
    """去掉 chunk_id 前缀与各类标记，保留自然语言描述。"""
    cleaned = _CHUNK_PREFIX_RE.sub("", text)
    cleaned = _SCORE_RE.sub("", cleaned)
    cleaned = _BRACKET_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else text


def _find_qa_question(atoms: list[dict], low_conf_item: str) -> str | None:
    """从 low_confidence_qa 条目中查找对应的 QA 问题内容。"""
    match = re.match(r"(P\d+-C\d+)(?:-Q(\d+))?", low_conf_item)
    if not match:
        return None
    chunk_id = match.group(1)
    qa_idx = int(match.group(2)) - 1 if match.group(2) else None

    for atom in atoms:
        if atom.get("chunk_id") == chunk_id:
            qas = atom.get("qa_pairs", [])
            if qa_idx is not None and 0 <= qa_idx < len(qas):
                return qas[qa_idx].get("question", "")
            for qa in qas:
                if qa.get("confidence_score", 100) < 80:
                    return qa.get("question", "")
    return None


def _find_chunk_title(atoms: list[dict], item_text: str) -> str | None:
    """从异常条目提取 chunk_id，查找对应 chunk 标题（去掉【模块】- 前缀）。"""
    match = _CHUNK_ID_EXTRACT_RE.match(item_text)
    if not match:
        return None
    chunk_id = match.group(1)
    for atom in atoms:
        if atom.get("chunk_id") == chunk_id:
            title = atom.get("title", "")
            return _TITLE_PREFIX_RE.sub("", title) or None
    return None


def generate(data: dict[str, Any]) -> ReviewResult:
    """从 JSON 提取异常项，生成自然语言人工复核清单。

    清单不出现 chunk_id 等内部编号，按 6 类异常分组，每类附默认处理建议。
    无异常时返回「全部通过」。
    """
    atoms = data.get("knowledge_atoms", [])
    el = data.get("exception_list", {})
    qs = data.get("quality_summary", {})

    total_chunks = len(atoms)
    total_qa = sum(len(a.get("qa_pairs", [])) for a in atoms)
    exception_count = qs.get("exception_count", 0)

    if exception_count == 0:
        return ReviewResult(
            summary="✅ 本次处理全部通过，无需人工复核，直接交付JSON文件。",
            needs_review=False,
        )

    sections: list[str] = []
    header = f"共处理出 {total_chunks} 个知识片段、{total_qa} 条问答，其中 {exception_count} 项需要你关注"
    sections.append(header)
    sections.append("")
    sections.append("需要关注的内容：")
    sections.append("")

    has_any = False

    # 1. 敏感信息
    sensitive = el.get("sensitive_items", [])
    if sensitive:
        has_any = True
        sections.append("**包含敏感信息的**（需确认脱敏是否正确）：")
        for item in sensitive:
            sections.append(f"- {_remove_chunk_prefix(item)}")
        sections.append("")

    # 2. 答案不确定的（低置信 QA + vague_items）
    low_conf = el.get("low_confidence_qa", [])
    vague = el.get("vague_items", [])
    if low_conf or vague:
        has_any = True
        sections.append("**答案不确定的**（原文信息不够，AI给出的回答可能不准）：")
        for item in low_conf:
            qa_desc = _find_qa_question(atoms, item)
            if qa_desc:
                sections.append(f"- 关于「{qa_desc}」，原文信息不够明确")
            else:
                sections.append(f"- {_remove_chunk_prefix(item)}")
        for item in vague:
            sections.append(f"- {_remove_chunk_prefix(item)}")
        sections.append("")

    # 3. 需要补充的
    missing = el.get("missing_info", [])
    if missing:
        has_any = True
        sections.append("**需要你补充的**（原文缺失，留了空等你填写）：")
        for item in missing:
            sections.append(f"- {_remove_chunk_prefix(item)}")
        sections.append("")

    # 4. 内容冲突
    conflicts = el.get("content_conflicts", [])
    if conflicts:
        has_any = True
        sections.append("**内容有冲突的**（不同地方说法不一致）：")
        for item in conflicts:
            sections.append(f"- {_remove_chunk_prefix(item)}")
        sections.append("")

    # 5. 专业术语待确认
    terms = el.get("terminology_pending", [])
    if terms:
        has_any = True
        sections.append("**专业术语待确认的**（内部叫法可能不标准）：")
        for item in terms:
            sections.append(f"- {_remove_chunk_prefix(item)}")
        sections.append("")

    # 6. 拆分可能不合理（chunk_anomalies + truncated_items）
    anomalies = el.get("chunk_anomalies", [])
    truncated = el.get("truncated_items", [])
    anomaly_items: list[str] = []
    for item in anomalies:
        desc = _remove_chunk_prefix(item)
        title_desc = _find_chunk_title(atoms, item)
        if title_desc and "拆分颗粒度异常" in desc:
            anomaly_items.append(f"「{title_desc}」这段内容{desc.replace('拆分颗粒度异常-', '')}")
        else:
            anomaly_items.append(desc)
    for item in truncated:
        desc = _remove_chunk_prefix(item)
        title_desc = _find_chunk_title(atoms, item)
        if title_desc:
            anomaly_items.append(f"「{title_desc}」可能被截断，内容不完整")
        else:
            anomaly_items.append(desc)

    if anomaly_items:
        has_any = True
        sections.append("**拆分可能不合理的**（知识片段过长或过短）：")
        for item in anomaly_items:
            sections.append(f"- {item}")
        sections.append("")

    # 7. 过期内容
    expired = el.get("expired_items", [])
    if expired:
        has_any = True
        sections.append("**可能已过期的**（内容可能不再适用）：")
        for item in expired:
            sections.append(f"- {_remove_chunk_prefix(item)}")
        sections.append("")

    if not has_any:
        return ReviewResult(
            summary="✅ 本次处理全部通过，无需人工复核，直接交付JSON文件。",
            needs_review=False,
        )

    sections.append("✅ 以上内容确认无误后，我将交付完整的JSON文件；如需修改请直接告诉我。")

    return ReviewResult(summary="\n".join(sections), needs_review=True)
