"""一键校验编排器（迁移自 kb-preprocess/scripts/run_validation.py）。

变更点：
- subprocess 串联 → 进程内函数调用
- 不读写中间结果文件，结果聚合为 ValidationReport
- 执行顺序与原脚本一致：格式校验 → 数值修正 → 一致性校验 → 复核清单
- 即使格式校验有错误，仍继续数值修正与一致性校验（与原脚本行为一致）
"""
from __future__ import annotations

from typing import Any

from app.core.validation.calc_metrics import calc
from app.core.validation.check_consistency import check
from app.core.validation.format_review import generate as generate_review
from app.core.validation.types import ValidationReport
from app.core.validation.validate_json import validate


def run(data: dict[str, Any], raw_text: str | None = None) -> ValidationReport:
    """一键执行完整校验流程。

    依次执行：
      1. 格式校验（validate）—— 不修改 data，只报告
      2. 数值修正（calc）—— 修正 quality_summary 与 document_info 的数值
      3. 一致性校验（check）—— 部分问题自动修复
      4. 复核清单生成（generate）—— 基于修正后的 data 生成自然语言摘要

    Args:
        data: LLM 输出的 JSON dict（会被就地修改并返回）
        raw_text: 原始 JSON 文本（可选，供中文引号检测）

    Returns:
        ValidationReport: 含修正后 data + 各步原始结果
    """
    # Step 1: 格式校验（不修改 data）
    fmt_result = validate(data, raw_text=raw_text)

    # Step 2: 数值修正（即使格式有问题也尝试修正）
    data, metrics_result = calc(data)

    # Step 3: 一致性校验 + 部分修复
    data, consistency_result = check(data)

    # Step 4: 复核清单（基于修正后的 data）
    review_result = generate_review(data)

    return ValidationReport(
        data=data,
        format=fmt_result,
        metrics=metrics_result,
        consistency=consistency_result,
        review=review_result,
    )
