"""一致性校验（迁移自 kb-preprocess/scripts/check_consistency.py）。

变更点：
- 解耦 sys.argv：check(data) → (data, ConsistencyResult)
- 不写文件，返回修正后的 data 与 issues/fixed
- 校验与自动修复逻辑与原脚本完全一致，对齐 SKILL.md 置信分/字符区间/敏感数据规则
"""
from __future__ import annotations

import re
from typing import Any

from kbrefiner.core.validation.types import ConsistencyIssue, ConsistencyResult

# QA 答案中疑似未脱敏的敏感数据模式
_SENSITIVE_PATTERNS = [
    (r"密码[：:]\s*\S+", "密码值未脱敏"),
    (r"密钥[：:]\s*\S+", "密钥值未脱敏"),
    (r"[Kk]ey[：:]\s*\S+", "Key值未脱敏"),
    (r"[Tt]oken[：:]\s*\S+", "Token值未脱敏"),
    (r"salt[：:]\s*\S+", "Salt值未脱敏"),
]


def check(data: dict[str, Any]) -> tuple[dict[str, Any], ConsistencyResult]:
    """检查置信分/字符区间/敏感数据一致性，部分问题自动修复。

    Returns:
        (修正后的 data, ConsistencyResult)
    """
    issues: list[ConsistencyIssue] = []
    atoms = data.get("knowledge_atoms", [])
    el = data.get("exception_list", {})

    # 1. 置信分 vs 低置信标记一致性
    for atom in atoms:
        chunk_id = atom.get("chunk_id", "?")
        for j, qa in enumerate(atom.get("qa_pairs", [])):
            score = qa.get("confidence_score")
            remark = qa.get("remark", "")
            has_low_mark = "低置信" in remark

            if score is not None:
                if score < 80 and not has_low_mark:
                    issues.append(ConsistencyIssue(
                        type="置信分与标记矛盾",
                        location=f"{chunk_id} 第{j+1}条QA",
                        detail=f"confidence_score={score}<80，但remark未标记【低置信-人工复核】",
                        fix="在remark中添加【低置信-人工复核】",
                    ))
                elif score >= 80 and has_low_mark:
                    issues.append(ConsistencyIssue(
                        type="置信分与标记矛盾",
                        location=f"{chunk_id} 第{j+1}条QA",
                        detail=f"confidence_score={score}≥80，但remark标记了【低置信-人工复核】",
                        fix="移除remark中的【低置信-人工复核】，或调整score<80",
                    ))

    # 2. chunk 字符数 vs 【拆分颗粒度异常】标注一致性
    for atom in atoms:
        chunk_id = atom.get("chunk_id", "?")
        content = atom.get("content", "")
        remark = atom.get("remark", "")
        char_count = len(content)

        has_anomaly_mark = "拆分颗粒度异常" in remark
        should_have_mark = char_count < 300 or char_count > 800

        if should_have_mark and not has_anomaly_mark:
            issues.append(ConsistencyIssue(
                type="字符数与标注矛盾",
                location=chunk_id,
                detail=f"内容{char_count}字，偏离300-800区间，但未标注【拆分颗粒度异常】",
                fix="在remark中添加【拆分颗粒度异常】，并在exception_list.chunk_anomalies中登记",
            ))
        elif not should_have_mark and has_anomaly_mark:
            issues.append(ConsistencyIssue(
                type="字符数与标注矛盾",
                location=chunk_id,
                detail=f"内容{char_count}字，在300-800区间内，但标注了【拆分颗粒度异常】",
                fix="移除remark中的【拆分颗粒度异常】标注",
            ))

        # chunk_anomalies 中应有对应记录
        if should_have_mark:
            found = any(chunk_id in item for item in el.get("chunk_anomalies", []))
            if not found:
                issues.append(ConsistencyIssue(
                    type="颗粒度异常未登记",
                    location=chunk_id,
                    detail=f"内容{char_count}字偏离区间，但exception_list.chunk_anomalies中无对应记录",
                    fix=f"在chunk_anomalies中添加: '{chunk_id}: 拆分颗粒度异常-内容{char_count}字'",
                ))

    # 3. 敏感数据标记一致性
    # 3a. content 标记了敏感数据的，应在 sensitive_items 有记录
    for atom in atoms:
        chunk_id = atom.get("chunk_id", "?")
        content = atom.get("content", "")
        if "敏感数据" in content:
            found = any(chunk_id in item for item in el.get("sensitive_items", []))
            if not found:
                issues.append(ConsistencyIssue(
                    type="敏感数据未登记",
                    location=chunk_id,
                    detail="content中标记了敏感数据，但exception_list.sensitive_items中无对应记录",
                    fix="在sensitive_items中添加该chunk的敏感数据记录",
                ))

        # 3b. QA 答案中的敏感数据应已脱敏
        for j, qa in enumerate(atom.get("qa_pairs", [])):
            answer = qa.get("answer", "")
            for pattern, desc in _SENSITIVE_PATTERNS:
                if re.search(pattern, answer) and "【敏感数据】" not in answer:
                    issues.append(ConsistencyIssue(
                        type="QA答案敏感数据未脱敏",
                        location=f"{chunk_id} 第{j+1}条QA",
                        detail=desc,
                        fix="将具体值替换为【敏感数据】，在remark注明已脱敏",
                    ))

    # 4. low_confidence_qa 条目与实际低置信 QA 对应（原脚本此处只做宽松检查，保持一致）
    # 原脚本逻辑：el 中条目提取 chunk_id 前缀后查实际低分 QA，未命中也不报错（假阳性可能）
    # 此处保留同样的宽松行为，不新增 issue

    # 5. 短答案检测：答案过短可能信息不完整（排除「文档未说明」）
    SHORT_ANSWER_THRESHOLD = 20
    for atom in atoms:
        chunk_id = atom.get("chunk_id", "?")
        for j, qa in enumerate(atom.get("qa_pairs", [])):
            answer = qa.get("answer", "").strip()
            if answer and answer != "文档未说明" and len(answer) < SHORT_ANSWER_THRESHOLD:
                issues.append(ConsistencyIssue(
                    type="答案过短",
                    location=f"{chunk_id} 第{j+1}条QA",
                    detail=f"答案仅{len(answer)}字（低于{SHORT_ANSWER_THRESHOLD}字），可能信息不完整",
                    fix="检查原文是否有更多相关信息，补充到答案中",
                ))

    # 自动修复：score<80 但未标记低置信的，添加标记
    fixed: list[str] = []
    for issue in issues:
        if issue.type == "置信分与标记矛盾" and "未标记" in issue.detail:
            loc = issue.location
            parts = loc.split(" 第")
            if len(parts) == 2:
                chunk_id = parts[0]
                try:
                    qa_idx = int(parts[1].replace("条QA", "")) - 1
                except ValueError:
                    continue
                for atom in atoms:
                    if atom.get("chunk_id") == chunk_id:
                        qas = atom.get("qa_pairs", [])
                        if 0 <= qa_idx < len(qas):
                            qa = qas[qa_idx]
                            if "低置信" not in qa.get("remark", ""):
                                qa["remark"] = (qa.get("remark", "") + " 【低置信-人工复核】").strip()
                                fixed.append(f"已修复: {loc} - 添加低置信标记")

    data["knowledge_atoms"] = atoms
    return data, ConsistencyResult(issues=issues, fixed=fixed)
