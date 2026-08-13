"""敏感数据识别与脱敏引擎公共接口。

用法：
    from kbrefiner.core.sensitive import SensitiveDetector, Action

    detector = SensitiveDetector()

    # Stage 1：扫描全文生成 sensitive_items 清单
    report = detector.scan_to_report(text)
    for m in report.matches:
        print(m.as_report_entry())

    # Stage 1：把全文敏感值替换为完整标记
    flagged = detector.flag(text)

    # Stage 3：QA 答案脱敏 + 命中清单
    masked, matches = detector.mask_qa_answer(answer)
"""
from kbrefiner.core.sensitive.detector import (
    SensitiveDetector,
    SensitiveMatch,
    SensitiveReport,
)
from kbrefiner.core.sensitive.rules import (
    Action,
    DEFAULT_RULES,
    FLAG_PLACEHOLDER,
    MASK_PLACEHOLDER,
    SensitiveRule,
    get_default_rules,
)

__all__ = [
    # 核心类
    "SensitiveDetector",
    "SensitiveMatch",
    "SensitiveReport",
    # 规则
    "Action",
    "SensitiveRule",
    "DEFAULT_RULES",
    "get_default_rules",
    # 常量
    "MASK_PLACEHOLDER",
    "FLAG_PLACEHOLDER",
]
