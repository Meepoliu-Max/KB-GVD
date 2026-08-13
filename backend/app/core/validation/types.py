"""校验模块的统一结果数据类型。

设计原则：
- 每个校验步骤返回结构化结果对象，不写文件、不打印。
- runner 聚合各步结果为 ValidationReport，由调用方决定如何呈现。
- 用 dataclass 保持轻量，不引入 Pydantic 校验开销（校验模块本身不该被校验）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationResult:
    """格式校验结果（对应 validate_json.py）。"""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)


@dataclass
class ConsistencyIssue:
    """单条一致性问题（对应 check_consistency.py 的 issue 结构）。"""

    type: str
    location: str
    detail: str
    fix: str


@dataclass
class MetricsResult:
    """数值计算与修正结果（对应 calc_metrics.py）。"""

    calculated: dict[str, Any]
    fixes: list[str] = field(default_factory=list)


@dataclass
class ConsistencyResult:
    """一致性校验结果（对应 check_consistency.py）。"""

    issues: list[ConsistencyIssue] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    @property
    def auto_fixed_count(self) -> int:
        return len(self.fixed)

    @property
    def unfixable_count(self) -> int:
        return len(self.issues) - len(self.fixed)


@dataclass
class ReviewResult:
    """人工复核摘要（对应 format_review.py）。"""

    summary: str
    needs_review: bool


@dataclass
class ValidationReport:
    """一键校验聚合报告（对应 run_validation.py 的汇总）。

    持有修正后的 data 与各步原始结果，调用方可按需序列化或展示。
    """

    data: dict[str, Any]
    format: ValidationResult
    metrics: MetricsResult
    consistency: ConsistencyResult
    review: ReviewResult

    @property
    def passed(self) -> bool:
        """格式校验通过且无不可修复的一致性问题."""
        return self.format.valid and self.consistency.unfixable_count == 0
