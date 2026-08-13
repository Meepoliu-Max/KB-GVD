"""校验模块公共接口。

迁移自 kb-preprocess/scripts/ 下的 5 个校验脚本，解耦为可导入函数。

用法：
    from app.core.validation import run, validate, calc, check, generate_review

    # 一键校验（推荐）
    report = run(data)
    print(report.passed, report.format.errors, report.review.summary)

    # 分步调用
    fmt = validate(data)
    data, metrics = calc(data)
    data, consistency = check(data)
    review = generate_review(data)
"""
from app.core.validation.calc_metrics import calc
from app.core.validation.check_consistency import check
from app.core.validation.format_review import generate as generate_review
from app.core.validation.runner import run
from app.core.validation.types import (
    ConsistencyIssue,
    ConsistencyResult,
    MetricsResult,
    ReviewResult,
    ValidationReport,
    ValidationResult,
)
from app.core.validation.validate_json import validate

__all__ = [
    # 一键编排
    "run",
    # 分步校验
    "validate",
    "calc",
    "check",
    "generate_review",
    # 结果类型
    "ValidationReport",
    "ValidationResult",
    "MetricsResult",
    "ConsistencyResult",
    "ConsistencyIssue",
    "ReviewResult",
]
