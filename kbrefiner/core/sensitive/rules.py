"""敏感数据识别规则集。

定义 SensitiveRule 数据结构与默认规则集 DEFAULT_RULES。

设计要点：
- 规则用正则表达式定义，按优先级排序（先匹配的优先）
- 每条规则声明默认动作（detect/mask/flag），调用方可覆盖
- placeholder 由调用方传入，规则只负责匹配
- 公开信息（域名、公开服务器地址）不收录，避免误报
- 默认规则集对齐 SKILL.md 与 check_consistency.py 中的硬编码规则，并扩展

规则覆盖范围：
  手机号 / 身份证 / 邮箱 / 银行卡 / 密码 / 密钥 / Token / API Key / Salt / 内网 IP
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Action(str, Enum):
    """命中敏感数据后的处理动作。"""

    DETECT = "detect"  # 仅识别，不替换（用于扫描清单）
    MASK = "mask"      # 替换为【敏感数据】（用于 Stage 3 QA 答案脱敏）
    FLAG = "flag"      # 替换为【敏感数据-禁止向量化入库】（用于 Stage 1 全文标记）


@dataclass(frozen=True)
class SensitiveRule:
    """单条敏感数据识别规则。

    Attributes:
        name: 规则名（唯一标识，如 "phone"）
        pattern: 正则表达式字符串（编译为 re.Pattern）
        description: 人类可读描述
        action: 默认动作（调用方可覆盖）
        priority: 优先级（数字越小越先匹配，同位置按列表顺序）
        case_sensitive: 是否区分大小写
    """

    name: str
    pattern: str
    description: str
    action: Action = Action.DETECT
    priority: int = 100
    case_sensitive: bool = False

    def __post_init__(self):
        # 编译正则，提前暴露语法错误
        flags = 0 if self.case_sensitive else re.IGNORECASE
        object.__setattr__(self, "_compiled", re.compile(self.pattern, flags))

    @property
    def compiled(self) -> re.Pattern:
        return self._compiled  # type: ignore[attr-defined]


# =====================================================================
# 默认规则集
# =====================================================================

# 标准脱敏占位符（与 SKILL.md / Stage Prompt 对齐）
MASK_PLACEHOLDER = "【敏感数据】"
FLAG_PLACEHOLDER = "【敏感数据-禁止向量化入库】"

# 已存在的标记包裹内容（避免重复处理）
_EXISTING_MARKERS = [
    r"【敏感数据-禁止向量化入库】",
    r"【敏感数据】",
]
EXISTING_MARKER_RE = re.compile("|".join(_EXISTING_MARKERS))


DEFAULT_RULES: list[SensitiveRule] = [
    # ============================================================
    # 凭证类（优先级最高，最先匹配）
    # ============================================================
    SensitiveRule(
        name="password",
        pattern=r"(?:密码|password|passwd|pwd)[：:=\s]+\s*[^\s，。；,;]{4,}",
        description="密码（含中文/英文标签 + 值）",
        action=Action.MASK,
        priority=10,
    ),
    SensitiveRule(
        name="api_key",
        pattern=r"(?:api[_\-\s]?key|API\s*Key)[：:=\s]+\s*[A-Za-z0-9_\-]{8,}",
        description="API Key",
        action=Action.MASK,
        priority=10,
    ),
    SensitiveRule(
        name="secret_key",
        pattern=r"(?:secret[_\-\s]?key|密钥)[：:=\s]+\s*[A-Za-z0-9_\-]{8,}",
        description="密钥",
        action=Action.MASK,
        priority=10,
    ),
    SensitiveRule(
        name="token",
        pattern=r"(?:token|令牌)[：:=\s]+\s*[A-Za-z0-9_\-\.]{8,}",
        description="Token / 令牌",
        action=Action.MASK,
        priority=10,
    ),
    SensitiveRule(
        name="salt",
        pattern=r"salt[：:=\s]+\s*[A-Za-z0-9_\-]{4,}",
        description="Salt 值",
        action=Action.MASK,
        priority=10,
    ),

    # ============================================================
    # 个人身份信息（PII）
    # ============================================================
    SensitiveRule(
        name="phone",
        pattern=r"(?<!\d)1[3-9]\d{9}(?!\d)",
        description="中国大陆手机号",
        action=Action.MASK,
        priority=20,
    ),
    SensitiveRule(
        name="id_card",
        pattern=r"(?<!\d)\d{17}[\dXx](?!\d)",
        description="身份证号（18位）",
        action=Action.MASK,
        priority=20,
    ),
    SensitiveRule(
        name="email",
        pattern=r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        description="邮箱地址",
        action=Action.DETECT,  # 邮箱通常业务联系用，默认仅识别
        priority=30,
    ),
    SensitiveRule(
        name="bank_card",
        pattern=r"(?<!\d)\d{16,19}(?!\d)",
        description="银行卡号（16-19位数字）",
        action=Action.MASK,
        priority=30,
    ),

    # ============================================================
    # 网络信息
    # ============================================================
    SensitiveRule(
        name="internal_ip",
        pattern=r"(?<!\d)(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?!\d)",
        description="内网 IP 地址（10.x / 192.168.x / 172.16-31.x）",
        action=Action.DETECT,  # 内网 IP 是否敏感视场景而定，默认仅识别
        priority=40,
    ),
]


def get_default_rules() -> list[SensitiveRule]:
    """返回默认规则集的深拷贝（避免外部修改影响内置规则）。"""
    return [
        SensitiveRule(
            name=r.name,
            pattern=r.pattern,
            description=r.description,
            action=r.action,
            priority=r.priority,
            case_sensitive=r.case_sensitive,
        )
        for r in DEFAULT_RULES
    ]


__all__ = [
    "Action",
    "SensitiveRule",
    "DEFAULT_RULES",
    "get_default_rules",
    "MASK_PLACEHOLDER",
    "FLAG_PLACEHOLDER",
    "EXISTING_MARKER_RE",
]
