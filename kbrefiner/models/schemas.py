"""知序 KBRefiner 权威 JSON Schema 定义。

字段结构与约束严格对齐：
- kb-preprocess/SKILL.md「输出格式」章节（权威来源）
- 5 个校验脚本（validate_json.py / calc_metrics.py / check_consistency.py）的字段约束

注意：business-framework.md 中的 JSON 结构已过时（qa_list/meta/exception_list 数组等），
以本文件为唯一权威定义。

设计原则：
- Schema 层只强制「硬性格式规则」（chunk_id 正则、confidence_score 范围、必填字段）。
- 「软性规则」（title 带【】、summary ≤20字、字符区间 300-800、置信分与标记一致性）
  不在 Schema 层抛错，留给 validation 模块按 warning/error 分级处理，
  避免 LLM 输出因软性规则频繁解析失败。
- extra='ignore'：LLM 输出多余字段自动忽略，提升容错。
"""
from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# =====================================================================
# 业务常量
# =====================================================================

# Stage 2 语义分块字符区间（偏离需备注【拆分颗粒度异常】）
CHUNK_MIN_CHARS = 300
CHUNK_MAX_CHARS = 800

# 置信分低于此阈值视为低置信（需标记【低置信-人工复核】）
LOW_CONFIDENCE_THRESHOLD = 80

# metadata.summary 最大字数
SUMMARY_MAX_CHARS = 20

# chunk_id 格式：P{分片号}-C{序号}，如 P1-C001、P2-C003
CHUNK_ID_PATTERN = re.compile(r"^P\d+-C\d+$")

# 敏感数据在 content 中的完整标记（不可省略后缀）
SENSITIVE_MARKER = "【敏感数据-禁止向量化入库】"
# QA 答案中敏感数据脱敏后的占位符
SENSITIVE_MASK = "【敏感数据】"


class DocType(str, Enum):
    """文档/知识原子类型。

    不同类型在 Stage 2 分块、Stage 4 打标时有差异化策略。
    教学知识为泛教育场景核心类型：概念/公式/例题/考点按知识点边界分块。
    """

    COMPLIANCE = "制度合规"
    FAQ = "FAQ"
    PRODUCT = "产品活动"
    OPS = "技术运维"
    TEACHING = "教学知识"


# =====================================================================
# document_info
# =====================================================================


class DocumentInfo(BaseModel):
    """文档级元信息。"""

    model_config = ConfigDict(extra="ignore")

    source: str = Field(description="原始文件名或来源描述")
    doc_type: DocType = Field(description="文档类型")
    total_chunks: int = Field(description="知识原子总数（= len(knowledge_atoms)）")
    total_qa_pairs: int = Field(description="QA 对总数（= 全部 qa_pairs 之和）")
    processing_date: str = Field(
        description="处理日期，格式 YYYY-MM-DD",
        default_factory=lambda: date.today().isoformat(),
    )


# =====================================================================
# knowledge_atoms
# =====================================================================


class Metadata(BaseModel):
    """知识原子多维元数据，Stage 4 打标产出。5 个字段缺一不可。"""

    model_config = ConfigDict(extra="ignore")

    target_audience: str = Field(description="目标受众，如「内部员工」「商家」")
    business_module: str = Field(description="业务模块，如「账号管理」「支付结算」")
    knowledge_type: str = Field(description="知识类型，如「操作指南」「政策条款」")
    version_timeliness: str = Field(
        description="版本时效，缺失时标注【人工补全项】，不要只写「未知」"
    )
    summary: str = Field(description="≤20 字概括")


class QaPair(BaseModel):
    """单条口语化 QA 对，Stage 3 产出。"""

    model_config = ConfigDict(extra="ignore")

    question: str = Field(description="口语化问题")
    answer: str = Field(
        description="严格取自原文的答案；原文没写填「文档未说明」；"
        "敏感数据必须脱敏为【敏感数据】"
    )
    keywords: list[str] = Field(description="关键词列表，建议 3 个")
    confidence_score: int = Field(
        description="置信分 0-100；<80 必须在 remark 标记【低置信-人工复核】，"
        "≥80 禁止标记低置信（两者互为充要条件）",
        ge=0,
        le=100,
    )
    remark: str = Field(default="", description="备注，低置信时含【低置信-人工复核】")

    @field_validator("confidence_score")
    @classmethod
    def _check_score_range(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError(f"confidence_score 超出范围 [0,100]: {v}")
        return v


class KnowledgeAtom(BaseModel):
    """单个知识原子（语义分块 + QA + 元数据），Stage 2/3/4 产出。"""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(description="分块 ID，格式 P{分片号}-C{序号}，如 P1-C001")
    title: str = Field(description="标题，格式【业务模块】- 具体内容")
    content: str = Field(description="纯净原子化内容")
    doc_type: DocType = Field(description="知识原子类型")
    metadata: Metadata
    qa_pairs: list[QaPair]
    remark: str = Field(default="", description="备注，如【拆分颗粒度异常】")

    @field_validator("chunk_id")
    @classmethod
    def _check_chunk_id_format(cls, v: str) -> str:
        if not CHUNK_ID_PATTERN.match(v):
            raise ValueError(
                f"chunk_id 格式错误: '{v}'，应为 P{{N}}-C{{NNN}}（如 P1-C001）"
            )
        return v


# =====================================================================
# exception_list（9 个字段，均为字符串数组）
# =====================================================================


class ExceptionList(BaseModel):
    """异常清单，9 个字段缺一不可，无异常填空数组 []。

    每条异常用一段简明文字描述「位置 + 问题」，
    禁止用对象结构 [{"key":"value"}]。
    """

    model_config = ConfigDict(extra="ignore")

    content_conflicts: list[str] = Field(
        default_factory=list, description="内容冲突项（分片内冲突）"
    )
    missing_info: list[str] = Field(
        default_factory=list, description="缺失信息，如「P1-C002: 缺少生效时间」"
    )
    vague_items: list[str] = Field(
        default_factory=list, description="模糊表述，如「P1-C005: 视情况而定无细则」"
    )
    expired_items: list[str] = Field(
        default_factory=list, description="过期内容说明"
    )
    chunk_anomalies: list[str] = Field(
        default_factory=list,
        description="拆分颗粒度异常，如「P1-C006: 内容不足300字」",
    )
    truncated_items: list[str] = Field(
        default_factory=list, description="分片截断位置"
    )
    sensitive_items: list[str] = Field(
        default_factory=list,
        description="敏感数据记录，如「P1-C001: 服务器登录密码-禁止向量化入库」；"
        "公开信息（域名、公开服务器地址）不得列入",
    )
    low_confidence_qa: list[str] = Field(
        default_factory=list,
        description="低置信 QA（score<80），如「P1-C004-Q3: 置信分78-原文未说明」",
    )
    terminology_pending: list[str] = Field(
        default_factory=list,
        description="自定义术语待确认，如「红宝识/红宝石 同一实体不同称谓」",
    )


# =====================================================================
# quality_summary
# =====================================================================


class QualitySummary(BaseModel):
    """质量汇总，5 个字段。数值由 calc_metrics 精确重算，不可信 LLM 输出。"""

    model_config = ConfigDict(extra="ignore")

    avg_confidence: float = Field(description="所有 QA 的 confidence_score 平均值，保留1位小数")
    low_confidence_count: int = Field(
        description="置信分 <80 的 QA 数量",
    )
    exception_count: int = Field(
        description="异常总数（exception_list 9 个数组的元素总数）",
    )
    coverage_rate: float = Field(
        default=0.0,
        description="知识覆盖率（0.0-1.0），衡量知识原子被 QA 有效覆盖的比例",
    )
    needs_review: bool = Field(
        description="是否有需人工复核项（exception_count > 0 即为 true）",
    )


# =====================================================================
# 顶层文档结构
# =====================================================================


class KbDocument(BaseModel):
    """知识库预处理最终产出文档，4 阶流水线 + 后处理的输出结构。

    用法：
        # 从 LLM 输出的 dict 解析（自动忽略多余字段）
        doc = KbDocument.model_validate(llm_output_dict)
        # 序列化为输出 JSON（字段顺序与定义顺序一致）
        doc.model_dump_json(indent=2, ensure_ascii_str=False)
    """

    model_config = ConfigDict(extra="ignore")

    document_info: DocumentInfo
    knowledge_atoms: list[KnowledgeAtom]
    exception_list: ExceptionList
    quality_summary: QualitySummary

    def total_qa_count(self) -> int:
        """统计实际 QA 总数（用于与 document_info.total_qa_pairs 对比）。"""
        return sum(len(a.qa_pairs) for a in self.knowledge_atoms)

    def total_exception_count(self) -> int:
        """统计异常总数（exception_list 9 个数组的元素总数）。"""
        return sum(
            len(getattr(self.exception_list, f))
            for f in ExceptionList.model_fields
        )
