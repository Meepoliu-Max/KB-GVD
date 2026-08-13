"""Stage Schema 断点校验测试。

覆盖：
- Stage 1/2/3/4 输出合法数据通过校验
- Stage 1：cleaned_text 空 / doc_type 非法枚举
- Stage 2：chunk_id 格式 / content 空 / chunks 空 / exception_list 9 字段默认空数组
- Stage 3：chunk_id 格式 / qa_pairs 空 / confidence_score 越界 / keywords 缺失
- Stage 4：chunk_id 格式 / metadata 5 字段缺失 / chunks 空
- extra='ignore' 容错（LLM 输出多余字段自动忽略）
- stage2_to_stage3_input / stage2_to_stage4_input 数据流转

运行：python -m unittest tests.test_stage_schemas -v
"""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from kbrefiner.models import (
    DocType,
    Stage1Output,
    Stage2Output,
    Stage3Output,
    Stage4Output,
    stage2_to_stage3_input,
    stage2_to_stage4_input,
)


# =====================================================================
# Stage 1 测试
# =====================================================================


class TestStage1Output(unittest.TestCase):
    """Stage 1 输出校验。"""

    def test_valid_output(self):
        out = Stage1Output.model_validate({
            "cleaned_text": "# 标题\n正文内容",
            "doc_type": "制度合规",
            "sensitive_items": ["第1章: 密码abc123-禁止向量化入库"],
            "terminology_pending": ["红宝识 / 红宝石 可能指同一实体"],
        })
        self.assertEqual(out.doc_type, DocType.COMPLIANCE)
        self.assertEqual(len(out.sensitive_items), 1)
        self.assertEqual(len(out.terminology_pending), 1)

    def test_empty_sensitive_and_terminology(self):
        """无敏感数据无术语问题时，两字段应为空数组（默认值）。"""
        out = Stage1Output.model_validate({
            "cleaned_text": "纯净文本",
            "doc_type": "FAQ",
        })
        self.assertEqual(out.sensitive_items, [])
        self.assertEqual(out.terminology_pending, [])

    def test_empty_cleaned_text_rejected(self):
        with self.assertRaises(ValidationError):
            Stage1Output.model_validate({
                "cleaned_text": "",
                "doc_type": "FAQ",
            })

    def test_invalid_doc_type_rejected(self):
        with self.assertRaises(ValidationError):
            Stage1Output.model_validate({
                "cleaned_text": "x",
                "doc_type": "未知类型",
            })

    def test_extra_fields_ignored(self):
        out = Stage1Output.model_validate({
            "cleaned_text": "x",
            "doc_type": "技术运维",
            "extra_field": "should be ignored",
        })
        self.assertFalse(hasattr(out, "extra_field"))


# =====================================================================
# Stage 2 测试
# =====================================================================


class TestStage2Output(unittest.TestCase):
    """Stage 2 输出校验。"""

    VALID_OUTPUT = {
        "chunks": [
            {
                "chunk_id": "P1-C001",
                "title": "【账号管理】- 登录规则",
                "content": "连续3次失败锁定30分钟。",
                "remark": "",
            },
            {
                "chunk_id": "P1-C002",
                "title": "【账号管理】- 解锁流程",
                "content": "联系管理员解锁。",
                "remark": "",
            },
        ],
        "exception_list": {
            "content_conflicts": [],
            "missing_info": [],
            "vague_items": [],
            "expired_items": [],
            "chunk_anomalies": ["P1-C002: 拆分颗粒度异常-内容10字"],
            "truncated_items": [],
            "sensitive_items": [],
            "low_confidence_qa": [],
            "terminology_pending": [],
        },
    }

    def test_valid_output(self):
        out = Stage2Output.model_validate(self.VALID_OUTPUT)
        self.assertEqual(len(out.chunks), 2)
        self.assertEqual(out.chunks[0].chunk_id, "P1-C001")
        self.assertEqual(len(out.exception_list.chunk_anomalies), 1)

    def test_invalid_chunk_id_format(self):
        data = {
            "chunks": [{
                "chunk_id": "chunk_001",
                "title": "x",
                "content": "y",
                "remark": "",
            }],
            "exception_list": {},
        }
        with self.assertRaises(ValidationError):
            Stage2Output.model_validate(data)

    def test_empty_content_rejected(self):
        data = {
            "chunks": [{
                "chunk_id": "P1-C001",
                "title": "x",
                "content": "",
                "remark": "",
            }],
            "exception_list": {},
        }
        with self.assertRaises(ValidationError):
            Stage2Output.model_validate(data)

    def test_empty_chunks_rejected(self):
        with self.assertRaises(ValidationError):
            Stage2Output.model_validate({"chunks": [], "exception_list": {}})

    def test_exception_list_defaults_to_empty(self):
        """exception_list 缺失时默认 9 字段全为空数组。"""
        out = Stage2Output.model_validate({
            "chunks": [{
                "chunk_id": "P1-C001",
                "title": "x",
                "content": "y",
                "remark": "",
            }],
        })
        self.assertEqual(out.exception_list.chunk_anomalies, [])
        self.assertEqual(out.exception_list.sensitive_items, [])

    def test_remark_defaults_to_empty(self):
        out = Stage2Output.model_validate({
            "chunks": [{
                "chunk_id": "P1-C001",
                "title": "x",
                "content": "y",
            }],
        })
        self.assertEqual(out.chunks[0].remark, "")


# =====================================================================
# Stage 3 测试
# =====================================================================


class TestStage3Output(unittest.TestCase):
    """Stage 3 输出校验。"""

    VALID_OUTPUT = {
        "chunks": [
            {
                "chunk_id": "P1-C001",
                "qa_pairs": [
                    {
                        "question": "密码忘了怎么办？",
                        "answer": "点击登录页「忘记密码」重置。",
                        "keywords": ["密码", "忘记", "重置"],
                        "confidence_score": 90,
                        "remark": "",
                    },
                    {
                        "question": "登不上咋办？",
                        "answer": "文档未说明",
                        "keywords": ["登录", "失败"],
                        "confidence_score": 60,
                        "remark": "【低置信-人工复核】",
                    },
                ],
            },
        ],
        "exception_list": {
            "low_confidence_qa": ["P1-C001-Q2: 置信分60-原文未说明"],
        },
    }

    def test_valid_output(self):
        out = Stage3Output.model_validate(self.VALID_OUTPUT)
        self.assertEqual(len(out.chunks), 1)
        self.assertEqual(len(out.chunks[0].qa_pairs), 2)
        self.assertEqual(out.chunks[0].qa_pairs[0].confidence_score, 90)

    def test_invalid_chunk_id(self):
        data = {
            "chunks": [{
                "chunk_id": "bad_id",
                "qa_pairs": [{
                    "question": "q",
                    "answer": "a",
                    "keywords": ["k"],
                    "confidence_score": 80,
                    "remark": "",
                }],
            }],
        }
        with self.assertRaises(ValidationError):
            Stage3Output.model_validate(data)

    def test_empty_qa_pairs_rejected(self):
        data = {
            "chunks": [{
                "chunk_id": "P1-C001",
                "qa_pairs": [],
            }],
        }
        with self.assertRaises(ValidationError):
            Stage3Output.model_validate(data)

    def test_confidence_score_out_of_range(self):
        data = {
            "chunks": [{
                "chunk_id": "P1-C001",
                "qa_pairs": [{
                    "question": "q",
                    "answer": "a",
                    "keywords": ["k"],
                    "confidence_score": 150,
                    "remark": "",
                }],
            }],
        }
        with self.assertRaises(ValidationError):
            Stage3Output.model_validate(data)

    def test_missing_keywords_rejected(self):
        """keywords 是必填字段。"""
        data = {
            "chunks": [{
                "chunk_id": "P1-C001",
                "qa_pairs": [{
                    "question": "q",
                    "answer": "a",
                    "confidence_score": 80,
                    "remark": "",
                }],
            }],
        }
        with self.assertRaises(ValidationError):
            Stage3Output.model_validate(data)

    def test_remark_defaults_to_empty(self):
        out = Stage3Output.model_validate({
            "chunks": [{
                "chunk_id": "P1-C001",
                "qa_pairs": [{
                    "question": "q",
                    "answer": "a",
                    "keywords": ["k"],
                    "confidence_score": 80,
                }],
            }],
        })
        self.assertEqual(out.chunks[0].qa_pairs[0].remark, "")


# =====================================================================
# Stage 4 测试
# =====================================================================


class TestStage4Output(unittest.TestCase):
    """Stage 4 输出校验。"""

    VALID_OUTPUT = {
        "chunks": [
            {
                "chunk_id": "P1-C001",
                "doc_type": "技术运维",
                "metadata": {
                    "target_audience": "管理员",
                    "business_module": "账号管理",
                    "knowledge_type": "操作指南",
                    "version_timeliness": "2026-01-01生效",
                    "summary": "登录失败锁定规则",
                },
                "remark": "",
            },
        ],
        "exception_list": {
            "missing_info": ["P1-C001: 缺少version_timeliness"],
        },
    }

    def test_valid_output(self):
        out = Stage4Output.model_validate(self.VALID_OUTPUT)
        self.assertEqual(len(out.chunks), 1)
        self.assertEqual(out.chunks[0].metadata.target_audience, "管理员")
        self.assertEqual(out.chunks[0].metadata.summary, "登录失败锁定规则")

    def test_invalid_chunk_id(self):
        data = {
            "chunks": [{
                "chunk_id": "x",
                "doc_type": "技术运维",
                "metadata": {
                    "target_audience": "a",
                    "business_module": "b",
                    "knowledge_type": "c",
                    "version_timeliness": "d",
                    "summary": "e",
                },
            }],
        }
        with self.assertRaises(ValidationError):
            Stage4Output.model_validate(data)

    def test_missing_metadata_field_rejected(self):
        """metadata 5 字段缺一不可。"""
        data = {
            "chunks": [{
                "chunk_id": "P1-C001",
                "doc_type": "技术运维",
                "metadata": {
                    "target_audience": "a",
                    "business_module": "b",
                    "knowledge_type": "c",
                    # 缺少 version_timeliness 和 summary
                },
            }],
        }
        with self.assertRaises(ValidationError):
            Stage4Output.model_validate(data)

    def test_invalid_doc_type_rejected(self):
        data = {
            "chunks": [{
                "chunk_id": "P1-C001",
                "doc_type": "不存在",
                "metadata": {
                    "target_audience": "a",
                    "business_module": "b",
                    "knowledge_type": "c",
                    "version_timeliness": "d",
                    "summary": "e",
                },
            }],
        }
        with self.assertRaises(ValidationError):
            Stage4Output.model_validate(data)

    def test_empty_chunks_rejected(self):
        with self.assertRaises(ValidationError):
            Stage4Output.model_validate({"chunks": []})

    def test_remark_defaults_to_empty(self):
        out = Stage4Output.model_validate({
            "chunks": [{
                "chunk_id": "P1-C001",
                "doc_type": "FAQ",
                "metadata": {
                    "target_audience": "a",
                    "business_module": "b",
                    "knowledge_type": "c",
                    "version_timeliness": "d",
                    "summary": "e",
                },
            }],
        })
        self.assertEqual(out.chunks[0].remark, "")


# =====================================================================
# Stage 间数据流转测试
# =====================================================================


class TestStageTransition(unittest.TestCase):
    """Stage 2 → Stage 3/4 数据流转。"""

    def _make_stage2_output(self) -> Stage2Output:
        return Stage2Output.model_validate({
            "chunks": [
                {
                    "chunk_id": "P3-C001",
                    "title": "【账号】- 登录",
                    "content": "内容1",
                    "remark": "",
                },
                {
                    "chunk_id": "P3-C002",
                    "title": "【账号】- 解锁",
                    "content": "内容2",
                    "remark": "",
                },
            ],
        })

    def test_stage2_to_stage3_input(self):
        stage2_out = self._make_stage2_output()
        stage3_in = stage2_to_stage3_input(
            stage2_out, DocType.OPS, ["密码abc-禁止向量化入库"]
        )
        self.assertEqual(len(stage3_in.chunks), 2)
        self.assertEqual(stage3_in.chunks[0]["chunk_id"], "P3-C001")
        self.assertEqual(stage3_in.chunks[0]["title"], "【账号】- 登录")
        self.assertEqual(stage3_in.chunks[0]["content"], "内容1")
        self.assertEqual(stage3_in.doc_type, DocType.OPS)
        self.assertEqual(stage3_in.sensitive_items, ["密码abc-禁止向量化入库"])
        # partition_prefix 从 chunk_id 提取
        self.assertEqual(stage3_in.chunk_id_prefix, "P3")

    def test_stage2_to_stage4_input(self):
        stage2_out = self._make_stage2_output()
        stage4_in = stage2_to_stage4_input(stage2_out, DocType.OPS)
        self.assertEqual(len(stage4_in.chunks), 2)
        self.assertEqual(stage4_in.chunks[1]["chunk_id"], "P3-C002")
        self.assertEqual(stage4_in.doc_type, DocType.OPS)

    def test_stage3_and_stage4_receive_same_chunks(self):
        """Stage 3 和 Stage 4 输入应相同（可并行）。"""
        stage2_out = self._make_stage2_output()
        stage3_in = stage2_to_stage3_input(stage2_out, DocType.OPS, [])
        stage4_in = stage2_to_stage4_input(stage2_out, DocType.OPS)
        # chunks 内容相同
        self.assertEqual(stage3_in.chunks, stage4_in.chunks)


if __name__ == "__main__":
    unittest.main()
