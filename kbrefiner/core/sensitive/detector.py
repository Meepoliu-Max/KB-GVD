"""敏感数据识别与脱敏引擎。

核心类 SensitiveDetector：
- detect(text)         → 识别所有命中，返回 SensitiveMatch 列表（不修改原文）
- mask(text, ...)      → 将命中替换为【敏感数据】（Stage 3 QA 答案脱敏用）
- flag(text, ...)      → 将命中替换为【敏感数据-禁止向量化入库】（Stage 1 全文标记用）
- scan_to_report(text) → 生成结构化报告（供 Stage 1 sensitive_items 清单登记）

上下文感知：
- 已被【敏感数据】或【敏感数据-禁止向量化入库】标记包裹的内容不重复处理
- 通过 _mask_existing_markers 先把已存在标记替换为占位符，处理完再还原

设计目标：
- 替代 check_consistency.py 中硬编码的 5 条 _SENSITIVE_PATTERNS
- 规则可配置、可扩展（外部加载 YAML/JSON）
- 三种动作分离，对应 Stage 1 / Stage 3 / validation 不同场景
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from kbrefiner.core.sensitive.rules import (
    Action,
    DEFAULT_RULES,
    EXISTING_MARKER_RE,
    FLAG_PLACEHOLDER,
    MASK_PLACEHOLDER,
    SensitiveRule,
    get_default_rules,
)

# 占位符模板（处理已存在标记时用，避免被规则误匹配）
_PLACEHOLDER_TEMPLATE = "\x00SENSITIVE_{}\x00"


@dataclass
class SensitiveMatch:
    """单条敏感数据命中结果。

    Attributes:
        rule_name: 命中的规则名（如 "phone"）
        match_text: 原始命中文本
        masked_text: 脱敏后的文本（按 action 决定占位符）
        start: 在原文中的起始位置
        end: 在原文中的结束位置
        action: 处理动作
        description: 规则描述
    """

    rule_name: str
    match_text: str
    masked_text: str
    start: int
    end: int
    action: Action
    description: str

    def as_report_entry(self) -> str:
        """转为 Stage 1 sensitive_items 清单条目格式。"""
        return f"{self.rule_name}: {self.match_text} → {self.masked_text}"


@dataclass
class SensitiveReport:
    """敏感数据扫描报告。"""

    matches: list[SensitiveMatch]
    total_count: int

    @property
    def has_sensitive(self) -> bool:
        return self.total_count > 0

    def by_rule(self) -> dict[str, list[SensitiveMatch]]:
        """按规则名分组。"""
        grouped: dict[str, list[SensitiveMatch]] = {}
        for m in self.matches:
            grouped.setdefault(m.rule_name, []).append(m)
        return grouped


class SensitiveDetector:
    """敏感数据识别与脱敏引擎。

    用法示例：
        detector = SensitiveDetector()  # 用默认规则集

        # Stage 1：扫描全文，生成 sensitive_items 清单
        report = detector.scan_to_report(text)
        for m in report.matches:
            print(m.as_report_entry())

        # Stage 1：把全文敏感值替换为完整标记
        flagged = detector.flag(text)

        # Stage 3：QA 答案脱敏
        masked, matches = detector.mask_qa_answer(answer)
    """

    def __init__(self, rules: list[SensitiveRule] | None = None):
        """
        Args:
            rules: 规则集。None 用默认规则集 DEFAULT_RULES。
        """
        self._rules = sorted(
            rules if rules is not None else get_default_rules(),
            key=lambda r: r.priority,
        )

    @property
    def rules(self) -> list[SensitiveRule]:
        return list(self._rules)

    # =================================================================
    # 识别（不修改原文）
    # =================================================================

    def detect(self, text: str, action_filter: Action | None = None) -> list[SensitiveMatch]:
        """识别所有命中，返回 SensitiveMatch 列表，不修改原文。

        Args:
            text: 待扫描文本
            action_filter: 仅返回指定动作的命中；None 返回全部

        Note:
            已被【敏感数据】或【敏感数据-禁止向量化入库】标记包裹的内容
            不会被重复识别。
        """
        if not text:
            return []

        # 把已存在的标记替换为占位符，避免内部内容被规则误匹配
        masked_text, restore_map = self._mask_existing_markers(text)

        matches: list[SensitiveMatch] = []
        occupied: list[tuple[int, int]] = []  # 已命中的区间，避免重叠

        for rule in self._rules:
            if action_filter is not None and rule.action != action_filter:
                continue
            for m in rule.compiled.finditer(masked_text):
                start, end = m.start(), m.end()
                # 跳过与已命中区间重叠的
                if any(s <= start < e or s < end <= e or (start <= s and e <= end)
                       for s, e in occupied):
                    continue
                # 跳过占位符区域（已存在标记的位置）
                if self._is_in_placeholder(masked_text, start, end):
                    continue

                match_text = m.group(0)
                placeholder = self._placeholder_for_action(rule.action)
                matches.append(SensitiveMatch(
                    rule_name=rule.name,
                    match_text=match_text,
                    masked_text=placeholder,
                    start=start,
                    end=end,
                    action=rule.action,
                    description=rule.description,
                ))
                occupied.append((start, end))

        # 按位置排序
        matches.sort(key=lambda x: x.start)
        return matches

    # =================================================================
    # 替换（mask / flag）
    # =================================================================

    def mask(
        self,
        text: str,
        placeholder: str = MASK_PLACEHOLDER,
        action_filter: Action | None = Action.MASK,
    ) -> str:
        """将命中的敏感数据替换为指定占位符。

        Args:
            text: 原文
            placeholder: 替换占位符，默认【敏感数据】
            action_filter: 仅处理指定动作的命中；None 处理所有命中

        Returns:
            替换后的文本
        """
        return self._replace(text, placeholder, action_filter)

    def flag(
        self,
        text: str,
        placeholder: str = FLAG_PLACEHOLDER,
        action_filter: Action | None = None,
    ) -> str:
        """将命中的敏感数据替换为完整标记（供 Stage 1 全文标记）。

        与 mask 的区别：默认 placeholder 为【敏感数据-禁止向量化入库】，
        且 action_filter=None 表示所有规则都按 flag 处理（无视规则自身的 action）。

        Args:
            text: 原文
            placeholder: 替换占位符，默认【敏感数据-禁止向量化入库】
            action_filter: 仅处理指定动作的命中；None 强制所有命中都 flag
        """
        return self._replace(text, placeholder, action_filter, force_action=Action.FLAG)

    def mask_qa_answer(
        self,
        answer: str,
        placeholder: str = MASK_PLACEHOLDER,
    ) -> tuple[str, list[SensitiveMatch]]:
        """Stage 3 专用：脱敏 QA 答案，返回脱敏后文本 + 命中清单。

        等价于 mask()，但额外返回命中清单，便于 Stage 3 登记到
        exception_list.sensitive_items。

        Args:
            answer: QA 答案原文
            placeholder: 替换占位符，默认【敏感数据】

        Returns:
            (脱敏后的答案, 命中清单)
        """
        matches = self.detect(answer, action_filter=Action.MASK)
        if not matches:
            return answer, []
        masked = self._apply_replacements(answer, matches, placeholder)
        return masked, matches

    # =================================================================
    # 扫描报告（供 Stage 1 sensitive_items 登记用）
    # =================================================================

    def scan_to_report(self, text: str) -> SensitiveReport:
        """扫描全文，生成结构化报告。

        与 detect() 的区别：返回 SensitiveReport 包装结构，
        含 has_sensitive / by_rule() 等便捷方法。
        """
        matches = self.detect(text)
        return SensitiveReport(matches=matches, total_count=len(matches))

    # =================================================================
    # 内部实现
    # =================================================================

    def _replace(
        self,
        text: str,
        placeholder: str,
        action_filter: Action | None,
        force_action: Action | None = None,
    ) -> str:
        """通用替换实现。

        Args:
            text: 原文
            placeholder: 替换占位符
            action_filter: 仅处理指定动作的命中
            force_action: 强制覆盖所有命中的动作（用于 flag 无视规则自身 action）
        """
        if not text:
            return text

        # 确定要处理的命中
        if force_action is not None:
            # flag 模式：处理所有命中，无视 action_filter
            matches = self.detect(text)
        else:
            matches = self.detect(text, action_filter=action_filter)

        if not matches:
            return text

        # 强制动作时，统一占位符
        if force_action is not None:
            for m in matches:
                m.masked_text = placeholder
                m.action = force_action
        else:
            # 按规则默认动作决定占位符（mask 用 placeholder，detect 不替换）
            for m in matches:
                if m.action == Action.MASK:
                    m.masked_text = placeholder
                # detect 动作不替换，保留原文

        # 仅替换 MASK / FLAG 动作的命中
        to_replace = [m for m in matches if m.action in (Action.MASK, Action.FLAG)]
        return self._apply_replacements(text, to_replace, placeholder)

    def _apply_replacements(
        self, text: str, matches: list[SensitiveMatch], placeholder: str
    ) -> str:
        """把命中的区间替换为占位符（从后往前替换，避免位置偏移）。"""
        # 先把已存在标记替换为占位符
        masked_text, restore_map = self._mask_existing_markers(text)

        # 在 masked_text 上做替换
        # 重新定位命中的位置（因为 _mask_existing_markers 改变了文本）
        # 简化：在 masked_text 上重新 detect 一次
        # 但 matches 的 start/end 是基于原始 text 的，需要重新计算
        # 更简单：直接在 masked_text 上重新 detect
        if force_replacements := self._redetect_for_replacement(masked_text, matches, placeholder):
            # 从后往前替换
            for start, end, repl in sorted(force_replacements, key=lambda x: x[0], reverse=True):
                masked_text = masked_text[:start] + repl + masked_text[end:]

        # 还原已存在标记
        return self._restore_markers(masked_text, restore_map)

    def _redetect_for_replacement(
        self, masked_text: str, original_matches: list[SensitiveMatch], placeholder: str
    ) -> list[tuple[int, int, str]]:
        """在已处理已存在标记的文本上重新匹配，返回 (start, end, replacement) 列表。"""
        replacements: list[tuple[int, int, str]] = []
        occupied: list[tuple[int, int]] = []
        rule_names = {m.rule_name for m in original_matches}

        for rule in self._rules:
            if rule.name not in rule_names:
                continue
            for m in rule.compiled.finditer(masked_text):
                start, end = m.start(), m.end()
                if any(s <= start < e or s < end <= e for s, e in occupied):
                    continue
                if self._is_in_placeholder(masked_text, start, end):
                    continue
                replacements.append((start, end, placeholder))
                occupied.append((start, end))

        return replacements

    def _mask_existing_markers(self, text: str) -> tuple[str, dict[str, str]]:
        """把已存在的【敏感数据】和【敏感数据-禁止向量化入库】标记替换为占位符。

        防止标记内部内容被规则重新匹配。

        Returns:
            (替换后的文本, {占位符: 原始标记}) 用于还原
        """
        restore_map: dict[str, str] = {}
        counter = 0

        def _replace(m: re.Match) -> str:
            nonlocal counter
            original = m.group(0)
            # 检查是否已经是占位符（避免重复处理）
            if original.startswith("\x00SENSITIVE_"):
                return original
            placeholder = _PLACEHOLDER_TEMPLATE.format(counter)
            restore_map[placeholder] = original
            counter += 1
            return placeholder

        masked = EXISTING_MARKER_RE.sub(_replace, text)
        return masked, restore_map

    def _restore_markers(self, text: str, restore_map: dict[str, str]) -> str:
        """还原 _mask_existing_markers 替换的标记。"""
        for placeholder, original in restore_map.items():
            text = text.replace(placeholder, original)
        return text

    def _is_in_placeholder(self, text: str, start: int, end: int) -> bool:
        """判断给定区间是否落在占位符内部。"""
        # 占位符格式：\x00SENSITIVE_{n}\x00
        # 检查 start 之前是否有 \x00SENSITIVE_ 且 end 之后有 \x00
        prefix = text.rfind("\x00SENSITIVE_", 0, start)
        if prefix == -1:
            return False
        # 检查 prefix 之后是否有 \x00 闭合（在 start 之前）
        closing = text.find("\x00", prefix + 1)
        return closing != -1 and closing >= start - 1

    @staticmethod
    def _placeholder_for_action(action: Action) -> str:
        if action == Action.MASK:
            return MASK_PLACEHOLDER
        if action == Action.FLAG:
            return FLAG_PLACEHOLDER
        return ""  # detect 不替换


__all__ = [
    "SensitiveDetector",
    "SensitiveMatch",
    "SensitiveReport",
]
