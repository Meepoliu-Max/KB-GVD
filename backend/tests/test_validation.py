"""校验模块全链路测试。

迁移自 kb-preprocess/scripts/ 的 5 个校验脚本，验证解耦后的 core/validation 模块。
覆盖：合法文档全链路 + 5 类错误注入 + 数值修正 + 一致性自动修复 + 复核清单 + 中文引号 + run() 聚合。

运行：python -m unittest tests.test_validation -v
"""
from __future__ import annotations

import copy
import unittest

from app.core.validation import calc, check, generate_review, run, validate


def make_valid_data() -> dict:
    """构造一份合法基准文档。"""
    return {
        "document_info": {
            "source": "账号管理规范.pdf",
            "doc_type": "技术运维",
            "total_chunks": 1,
            "total_qa_pairs": 2,
            "processing_date": "2026-08-04",
        },
        "knowledge_atoms": [
            {
                "chunk_id": "P1-C001",
                "title": "【账号管理】- 登录失败处理",
                "content": "用户登录失败连续3次将锁定账号30分钟，锁定期间无法再次登录。" * 20,
                "doc_type": "技术运维",
                "metadata": {
                    "target_audience": "内部员工",
                    "business_module": "账号管理",
                    "knowledge_type": "操作指南",
                    "version_timeliness": "永久有效",
                    "summary": "登录失败锁定规则",
                },
                "qa_pairs": [
                    {
                        "question": "登录失败几次会锁定？",
                        "answer": "连续3次失败将锁定账号30分钟。",
                        "keywords": ["登录", "锁定", "账号"],
                        "confidence_score": 95,
                        "remark": "",
                    },
                    {
                        "question": "锁定多久？",
                        "answer": "30分钟。",
                        "keywords": ["锁定", "30分钟"],
                        "confidence_score": 72,
                        "remark": "【低置信-人工复核】",
                    },
                ],
                "remark": "",
            },
        ],
        "exception_list": {
            "content_conflicts": [],
            "missing_info": [],
            "vague_items": [],
            "expired_items": [],
            "chunk_anomalies": [],
            "truncated_items": [],
            "sensitive_items": [],
            "low_confidence_qa": ["P1-C001-Q2: 置信分72-原文表述模糊"],
            "terminology_pending": [],
        },
        "quality_summary": {
            "avg_confidence": 83.5,
            "low_confidence_count": 1,
            "exception_count": 1,
            "needs_review": True,
        },
    }


class TestValidDocument(unittest.TestCase):
    """合法文档全链路。"""

    def test_full_pipeline_passes(self):
        data = make_valid_data()
        report = run(data)
        self.assertTrue(report.format.valid, f"errors: {report.format.errors}")
        self.assertEqual(report.format.error_count, 0)
        self.assertEqual(len(report.metrics.fixes), 0, f"fixes: {report.metrics.fixes}")
        self.assertEqual(report.consistency.issue_count, 0,
                         f"issues: {[i.detail for i in report.consistency.issues]}")
        self.assertTrue(report.review.needs_review)
        self.assertIn("答案不确定", report.review.summary)
        # 复核清单去掉了 chunk_id 编号
        self.assertNotIn("P1-C001", report.review.summary)
        self.assertTrue(report.passed)


class TestValidateJson(unittest.TestCase):
    """格式校验错误注入。"""

    def test_chunk_id_format_error(self):
        data = make_valid_data()
        data["knowledge_atoms"][0]["chunk_id"] = "chunk_001"
        r = validate(data)
        self.assertFalse(r.valid)
        self.assertTrue(any("chunk_id格式错误" in e for e in r.errors))

    def test_total_chunks_mismatch(self):
        data = make_valid_data()
        data["document_info"]["total_chunks"] = 99
        r = validate(data)
        self.assertFalse(r.valid)
        self.assertTrue(any("total_chunks" in e for e in r.errors))

    def test_confidence_score_out_of_range(self):
        data = make_valid_data()
        data["knowledge_atoms"][0]["qa_pairs"][0]["confidence_score"] = 150
        r = validate(data)
        self.assertFalse(r.valid)
        self.assertTrue(any("confidence_score超出范围" in e for e in r.errors))

    def test_total_qa_pairs_mismatch(self):
        data = make_valid_data()
        data["document_info"]["total_qa_pairs"] = 99
        r = validate(data)
        self.assertFalse(r.valid)
        self.assertTrue(any("total_qa_pairs" in e for e in r.errors))

    def test_exception_list_missing_field(self):
        data = make_valid_data()
        del data["exception_list"]["sensitive_items"]
        r = validate(data)
        self.assertFalse(r.valid)
        self.assertTrue(any("exception_list缺少字段: sensitive_items" in e for e in r.errors))

    def test_needs_review_not_bool(self):
        data = make_valid_data()
        data["quality_summary"]["needs_review"] = "yes"
        r = validate(data)
        self.assertFalse(r.valid)
        self.assertTrue(any("needs_review 不是布尔值" in e for e in r.errors))

    def test_cn_quote_warning_with_raw_text(self):
        data = make_valid_data()
        # 用 chr 避免源码中出现中文引号字面量
        raw = '{"a": "b"}'.replace('"', chr(0x201C), 1)
        r = validate(data, raw_text=raw)
        self.assertTrue(any("中文引号" in w for w in r.warnings))

    def test_no_cn_quote_check_without_raw_text(self):
        data = make_valid_data()
        r = validate(data)
        self.assertFalse(any("中文引号" in w for w in r.warnings))

    def test_title_warning(self):
        data = make_valid_data()
        data["knowledge_atoms"][0]["title"] = "裸标题无归属"
        r = validate(data)
        self.assertTrue(any("title缺少【上下文归属】" in w for w in r.warnings))

    def test_summary_too_long_warning(self):
        data = make_valid_data()
        data["knowledge_atoms"][0]["metadata"]["summary"] = "这是一个超过二十个字的摘要所以会触发警告哦"
        r = validate(data)
        self.assertTrue(any("summary超过20字" in w for w in r.warnings))


class TestCalcMetrics(unittest.TestCase):
    """数值计算与修正。"""

    def test_fix_avg_confidence(self):
        data = make_valid_data()
        data["quality_summary"]["avg_confidence"] = 99.9
        data, metrics = calc(data)
        self.assertTrue(any("avg_confidence" in f for f in metrics.fixes))
        self.assertEqual(data["quality_summary"]["avg_confidence"], 83.5)

    def test_fix_exception_count(self):
        data = make_valid_data()
        data["quality_summary"]["exception_count"] = 99
        data, metrics = calc(data)
        self.assertTrue(any("exception_count" in f for f in metrics.fixes))
        self.assertEqual(data["quality_summary"]["exception_count"], 1)

    def test_fix_needs_review(self):
        data = make_valid_data()
        data["quality_summary"]["needs_review"] = False
        data, metrics = calc(data)
        self.assertTrue(any("needs_review" in f for f in metrics.fixes))
        self.assertTrue(data["quality_summary"]["needs_review"])

    def test_fix_total_qa_pairs(self):
        data = make_valid_data()
        data["document_info"]["total_qa_pairs"] = 99
        data, metrics = calc(data)
        self.assertTrue(any("total_qa_pairs" in f for f in metrics.fixes))
        self.assertEqual(data["document_info"]["total_qa_pairs"], 2)

    def test_fix_total_chunks(self):
        data = make_valid_data()
        data["document_info"]["total_chunks"] = 99
        data, metrics = calc(data)
        self.assertTrue(any("total_chunks" in f for f in metrics.fixes))
        self.assertEqual(data["document_info"]["total_chunks"], 1)

    def test_no_fix_when_correct(self):
        data = make_valid_data()
        _, metrics = calc(data)
        self.assertEqual(len(metrics.fixes), 0)


class TestCheckConsistency(unittest.TestCase):
    """一致性校验与自动修复。"""

    def test_low_confidence_unmarked_auto_fix(self):
        data = make_valid_data()
        data["knowledge_atoms"][0]["qa_pairs"][1]["remark"] = ""  # 72分但去标记
        data, cons = check(data)
        self.assertTrue(cons.issue_count > 0)
        self.assertEqual(cons.auto_fixed_count, 1)
        self.assertIn("低置信", data["knowledge_atoms"][0]["qa_pairs"][1]["remark"])

    def test_high_score_with_low_mark(self):
        data = make_valid_data()
        # 95分但标了低置信
        data["knowledge_atoms"][0]["qa_pairs"][0]["remark"] = "【低置信-人工复核】"
        data, cons = check(data)
        self.assertTrue(any(i.type == "置信分与标记矛盾" and "≥80" in i.detail for i in cons.issues))

    def test_chunk_too_short_unmarked(self):
        data = make_valid_data()
        data["knowledge_atoms"][0]["content"] = "只有几个字"
        data, cons = check(data)
        self.assertTrue(any(i.type == "字符数与标注矛盾" for i in cons.issues))
        self.assertTrue(any(i.type == "颗粒度异常未登记" for i in cons.issues))

    def test_sensitive_not_registered(self):
        data = make_valid_data()
        data["knowledge_atoms"][0]["content"] += "服务器登录密码【敏感数据-禁止向量化入库】"
        data["exception_list"]["sensitive_items"] = []
        data, cons = check(data)
        self.assertTrue(any(i.type == "敏感数据未登记" for i in cons.issues))

    def test_qa_answer_sensitive_not_masked(self):
        data = make_valid_data()
        data["knowledge_atoms"][0]["qa_pairs"][0]["answer"] = "密码：abc123"
        data, cons = check(data)
        self.assertTrue(any(i.type == "QA答案敏感数据未脱敏" for i in cons.issues))


class TestFormatReview(unittest.TestCase):
    """复核清单生成。"""

    def test_no_exception_all_pass(self):
        data = make_valid_data()
        data["exception_list"]["low_confidence_qa"] = []
        data["quality_summary"]["exception_count"] = 0
        data["quality_summary"]["needs_review"] = False
        data["knowledge_atoms"][0]["qa_pairs"][1]["confidence_score"] = 85
        data["knowledge_atoms"][0]["qa_pairs"][1]["remark"] = ""
        review = generate_review(data)
        self.assertFalse(review.needs_review)
        self.assertIn("全部通过", review.summary)

    def test_review_strips_chunk_id(self):
        data = make_valid_data()
        review = generate_review(data)
        self.assertNotIn("P1-C001-Q2", review.summary)
        self.assertNotIn("P1-C001:", review.summary)

    def test_review_includes_qa_question(self):
        data = make_valid_data()
        review = generate_review(data)
        # 低置信 QA 应该用问题内容描述，而非编号
        self.assertIn("锁定多久", review.summary)


class TestRunner(unittest.TestCase):
    """一键校验聚合。"""

    def test_aggregates_all_steps(self):
        data = make_valid_data()
        # 注入数值错误 + 一致性问题
        data["quality_summary"]["avg_confidence"] = 99.9
        data["knowledge_atoms"][0]["qa_pairs"][1]["remark"] = ""
        report = run(data)
        # 格式本身没错
        self.assertTrue(report.format.valid)
        # 数值被修正
        self.assertTrue(any("avg_confidence" in f for f in report.metrics.fixes))
        # 一致性自动修复
        self.assertEqual(report.consistency.auto_fixed_count, 1)
        # 修正后的 data 含低置信标记
        self.assertIn("低置信", report.data["knowledge_atoms"][0]["qa_pairs"][1]["remark"])
        # 修正后 avg_confidence 正确
        self.assertEqual(report.data["quality_summary"]["avg_confidence"], 83.5)

    def test_passed_false_when_format_error(self):
        data = make_valid_data()
        data["knowledge_atoms"][0]["chunk_id"] = "bad"
        report = run(data)
        self.assertFalse(report.passed)


if __name__ == "__main__":
    unittest.main()
