"""敏感数据引擎单元测试。

覆盖：
- 各规则匹配正确性（手机号/身份证/邮箱/银行卡/密码/密钥/Token/API Key/Salt/内网IP）
- detect 不修改原文
- mask 替换为【敏感数据】
- flag 替换为【敏感数据-禁止向量化入库】
- 上下文感知：已存在的标记内容不重复处理
- mask_qa_answer 返回脱敏后文本 + 命中清单
- scan_to_report 结构化报告
- 边界：空文本/无命中/规则优先级/动作过滤
- 自定义规则集

运行：python -m unittest tests.test_sensitive -v
"""
from __future__ import annotations

import unittest

from kbrefiner.core.sensitive import (
    Action,
    FLAG_PLACEHOLDER,
    MASK_PLACEHOLDER,
    SensitiveDetector,
    SensitiveRule,
    get_default_rules,
)


class TestRuleMatching(unittest.TestCase):
    """各规则匹配正确性。"""

    def setUp(self):
        self.detector = SensitiveDetector()

    def test_phone(self):
        matches = self.detector.detect("联系我 13800138000 谢谢")
        self.assertTrue(any(m.rule_name == "phone" for m in matches))
        phone_match = next(m for m in matches if m.rule_name == "phone")
        self.assertEqual(phone_match.match_text, "13800138000")

    def test_phone_no_false_positive_in_long_digits(self):
        # 12位数字不应误匹配为手机号
        matches = self.detector.detect("订单号 123456789012")
        self.assertFalse(any(m.rule_name == "phone" for m in matches))

    def test_id_card(self):
        matches = self.detector.detect("身份证 110101199001011234")
        self.assertTrue(any(m.rule_name == "id_card" for m in matches))

    def test_id_card_with_x(self):
        matches = self.detector.detect("身份证 11010119900101123X")
        self.assertTrue(any(m.rule_name == "id_card" for m in matches))

    def test_email(self):
        matches = self.detector.detect("邮箱 user@example.com 联系")
        self.assertTrue(any(m.rule_name == "email" for m in matches))

    def test_bank_card(self):
        matches = self.detector.detect("卡号 6222021234567890123")
        self.assertTrue(any(m.rule_name == "bank_card" for m in matches))

    def test_password_chinese(self):
        matches = self.detector.detect("密码：abc123456")
        self.assertTrue(any(m.rule_name == "password" for m in matches))

    def test_password_english(self):
        matches = self.detector.detect("password: secret_pwd_2026")
        self.assertTrue(any(m.rule_name == "password" for m in matches))

    def test_api_key(self):
        matches = self.detector.detect("API Key: ak_1234567890abcdef")
        self.assertTrue(any(m.rule_name == "api_key" for m in matches))

    def test_secret_key(self):
        matches = self.detector.detect("密钥：sk_abcdefgh12345678")
        self.assertTrue(any(m.rule_name == "secret_key" for m in matches))

    def test_token(self):
        matches = self.detector.detect("token: tk_2026.08.04.abcdef")
        self.assertTrue(any(m.rule_name == "token" for m in matches))

    def test_salt(self):
        matches = self.detector.detect("salt: random_salt_value")
        self.assertTrue(any(m.rule_name == "salt" for m in matches))

    def test_internal_ip_192(self):
        matches = self.detector.detect("服务器 192.168.1.100 可访问")
        self.assertTrue(any(m.rule_name == "internal_ip" for m in matches))

    def test_internal_ip_10(self):
        matches = self.detector.detect("内网 10.0.0.1 网关")
        self.assertTrue(any(m.rule_name == "internal_ip" for m in matches))

    def test_internal_ip_172(self):
        matches = self.detector.detect("172.16.5.10 是内网")
        self.assertTrue(any(m.rule_name == "internal_ip" for m in matches))

    def test_public_ip_not_matched(self):
        # 公网 IP 不应匹配 internal_ip 规则
        matches = self.detector.detect("公网 8.8.8.8 是 DNS")
        self.assertFalse(any(m.rule_name == "internal_ip" for m in matches))


class TestDetectDoesNotModify(unittest.TestCase):
    """detect 不修改原文。"""

    def test_detect_preserves_original(self):
        detector = SensitiveDetector()
        original = "电话 13800138000 密码：abc123456"
        self.detector = detector
        matches = detector.detect(original)
        self.assertTrue(len(matches) > 0)
        # 原文未被修改
        self.assertEqual(original, "电话 13800138000 密码：abc123456")


class TestMask(unittest.TestCase):
    """mask 替换为【敏感数据】。"""

    def setUp(self):
        self.detector = SensitiveDetector()

    def test_mask_phone(self):
        result = self.detector.mask("电话 13800138000 联系")
        self.assertIn(MASK_PLACEHOLDER, result)
        self.assertNotIn("13800138000", result)

    def test_mask_multiple_types(self):
        result = self.detector.mask("电话 13800138000，密码：abc123456")
        # 两处都被替换
        self.assertEqual(result.count(MASK_PLACEHOLDER), 2)
        self.assertNotIn("13800138000", result)
        self.assertNotIn("abc123456", result)

    def test_mask_preserves_non_sensitive(self):
        result = self.detector.mask("普通文字 13800138000 普通文字")
        self.assertIn("普通文字", result)
        self.assertIn(MASK_PLACEHOLDER, result)

    def test_mask_custom_placeholder(self):
        result = self.detector.mask("13800138000", placeholder="<HIDDEN>")
        self.assertIn("<HIDDEN>", result)

    def test_mask_no_match_returns_original(self):
        original = "普通文本无敏感信息"
        result = self.detector.mask(original)
        self.assertEqual(result, original)


class TestFlag(unittest.TestCase):
    """flag 替换为【敏感数据-禁止向量化入库】。"""

    def setUp(self):
        self.detector = SensitiveDetector()

    def test_flag_default_placeholder(self):
        result = self.detector.flag("密码：abc123456")
        self.assertIn(FLAG_PLACEHOLDER, result)
        self.assertNotIn("abc123456", result)

    def test_flag_includes_detect_rules(self):
        # flag 默认处理所有规则（含 detect 类型的 email）
        result = self.detector.flag("邮箱 user@example.com")
        self.assertIn(FLAG_PLACEHOLDER, result)
        self.assertNotIn("user@example.com", result)


class TestContextAware(unittest.TestCase):
    """上下文感知：已存在的标记内容不重复处理。"""

    def setUp(self):
        self.detector = SensitiveDetector()

    def test_existing_flag_marker_not_reprocessed(self):
        # 已有完整标记包裹的内容不应再被识别
        text = f"密码：{FLAG_PLACEHOLDER}"
        matches = self.detector.detect(text)
        # 标记本身不应被规则匹配
        self.assertFalse(any("禁止向量化入库" in m.match_text for m in matches))

    def test_existing_mask_marker_not_reprocessed(self):
        text = f"密码：{MASK_PLACEHOLDER}"
        matches = self.detector.detect(text)
        self.assertFalse(any("敏感数据" in m.match_text and "禁止" not in m.match_text
                             for m in matches))

    def test_flag_preserves_existing_markers(self):
        # 已有标记应原样保留，不重复替换
        text = f"已有 {FLAG_PLACEHOLDER} 标记"
        result = self.detector.flag(text)
        # 应仍含原标记，且数量不增加
        self.assertEqual(result.count(FLAG_PLACEHOLDER), 1)

    def test_mixed_existing_and_new(self):
        text = f"旧标记 {FLAG_PLACEHOLDER}，新密码：new123456"
        result = self.detector.flag(text)
        # 旧标记保留，新敏感值被替换
        self.assertEqual(result.count(FLAG_PLACEHOLDER), 2)
        self.assertNotIn("new123456", result)


class TestMaskQaAnswer(unittest.TestCase):
    """Stage 3 专用：mask_qa_answer。"""

    def setUp(self):
        self.detector = SensitiveDetector()

    def test_returns_masked_and_matches(self):
        answer = "密码：abc123456，电话 13800138000"
        masked, matches = self.detector.mask_qa_answer(answer)
        self.assertIn(MASK_PLACEHOLDER, masked)
        self.assertNotIn("abc123456", masked)
        self.assertNotIn("13800138000", masked)
        self.assertEqual(len(matches), 2)
        rule_names = {m.rule_name for m in matches}
        self.assertIn("password", rule_names)
        self.assertIn("phone", rule_names)

    def test_no_match_returns_original(self):
        answer = "普通答案无敏感数据"
        masked, matches = self.detector.mask_qa_answer(answer)
        self.assertEqual(masked, answer)
        self.assertEqual(matches, [])

    def test_match_entry_format(self):
        answer = "密码：abc123456"
        _, matches = self.detector.mask_qa_answer(answer)
        entry = matches[0].as_report_entry()
        self.assertIn("password", entry)
        self.assertIn("abc123456", entry)


class TestScanToReport(unittest.TestCase):
    """扫描报告。"""

    def setUp(self):
        self.detector = SensitiveDetector()

    def test_report_structure(self):
        text = "电话 13800138000，邮箱 a@b.com"
        report = self.detector.scan_to_report(text)
        self.assertTrue(report.has_sensitive)
        self.assertEqual(report.total_count, 2)

    def test_report_by_rule(self):
        text = "13800138000 和 13900139000"
        report = self.detector.scan_to_report(text)
        by_rule = report.by_rule()
        self.assertIn("phone", by_rule)
        self.assertEqual(len(by_rule["phone"]), 2)

    def test_report_no_sensitive(self):
        report = self.detector.scan_to_report("普通文本")
        self.assertFalse(report.has_sensitive)
        self.assertEqual(report.total_count, 0)


class TestActionFilter(unittest.TestCase):
    """动作过滤。"""

    def setUp(self):
        self.detector = SensitiveDetector()

    def test_filter_mask_only(self):
        text = "13800138000 和 a@b.com"  # phone=mask, email=detect
        matches = self.detector.detect(text, action_filter=Action.MASK)
        rule_names = {m.rule_name for m in matches}
        self.assertIn("phone", rule_names)
        self.assertNotIn("email", rule_names)

    def test_filter_detect_only(self):
        text = "13800138000 和 a@b.com"
        matches = self.detector.detect(text, action_filter=Action.DETECT)
        rule_names = {m.rule_name for m in matches}
        self.assertIn("email", rule_names)
        self.assertNotIn("phone", rule_names)


class TestBoundary(unittest.TestCase):
    """边界情况。"""

    def test_empty_text(self):
        detector = SensitiveDetector()
        self.assertEqual(detector.detect(""), [])
        self.assertEqual(detector.mask(""), "")
        self.assertEqual(detector.flag(""), "")

    def test_custom_rules(self):
        # 自定义单条规则
        custom = SensitiveRule(
            name="employee_id",
            pattern=r"EMP\d{6}",
            description="员工工号",
            action=Action.MASK,
        )
        detector = SensitiveDetector(rules=[custom])
        matches = detector.detect("工号 EMP123456 注册")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].rule_name, "employee_id")
        # 默认规则不应生效
        self.assertFalse(any(m.rule_name == "phone" for m in matches))

    def test_no_overlap_between_rules(self):
        # 同一段文本不被两条规则同时命中
        detector = SensitiveDetector()
        # "密码：abc123456" 应只被 password 规则命中，abc123456 不应再被其他规则命中
        matches = detector.detect("密码：abc123456")
        # 应只有一条命中
        self.assertEqual(len(matches), 1)

    def test_get_default_rules_returns_copy(self):
        rules1 = get_default_rules()
        rules2 = get_default_rules()
        self.assertIsNot(rules1, rules2)
        self.assertEqual(len(rules1), len(rules2))


class TestSensitiveRuleDataclass(unittest.TestCase):
    """SensitiveRule 数据类。"""

    def test_compiled_available(self):
        rule = SensitiveRule(name="t", pattern=r"\d+", description="test")
        self.assertIsNotNone(rule.compiled)

    def test_case_insensitive_default(self):
        rule = SensitiveRule(name="t", pattern=r"test", description="t")
        self.assertTrue(rule.compiled.search("TEST"))

    def test_case_sensitive(self):
        rule = SensitiveRule(
            name="t", pattern=r"test", description="t", case_sensitive=True
        )
        self.assertIsNone(rule.compiled.search("TEST"))
        self.assertIsNotNone(rule.compiled.search("test"))


if __name__ == "__main__":
    unittest.main()
