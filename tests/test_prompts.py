"""Prompt 模板渲染测试。

覆盖：
- 4 个 stage 基础渲染：关键变量已替换、未残留 {{ }} 占位
- system/user 分割正确（含 ===END_SYSTEM=== 在 system 末尾）
- retry_errors/previous_output 注入（stage1/2/3/4 都验证）
- Stage 2 doc_type 差异化分支（4 种类型分别命中正确分支文字）
- Stage 3 sensitive_items 注入（列表/空列表两个分支）
- Stage 4 doc_type 差异化分支（FAQ 的 knowledge_type=FAQ 固定、制度合规禁止永久有效等）

运行：python -m unittest tests.test_prompts -v
"""
from __future__ import annotations

import unittest

from kbrefiner.core.pipeline.prompts import (
    render_stage1,
    render_stage2,
    render_stage3,
    render_stage4,
)


def _assert_no_placeholder(text: str) -> None:
    """确保渲染后无残留 Jinja2 占位符。"""
    assert "{{" not in text, f"残留 Jinja2 占位符 {{: \n{text[:300]}"
    assert "{%" not in text, f"残留 Jinja2 控制块 %: \n{text[:300]}"


class TestRenderStage1(unittest.TestCase):
    """Stage 1 渲染。"""

    def test_basic_render(self):
        system, user = render_stage1(
            markdown="# 测试文档\n正文内容",
            document_source="测试.pdf",
            doc_title="测试标题",
        )
        # 分割标记不进入任何一段（仅作切分用途，不发给 LLM）
        self.assertNotIn("===END_SYSTEM===", system)
        self.assertNotIn("===END_SYSTEM===", user)
        # 关键变量被替换
        self.assertIn("测试.pdf", user)           # document_source
        self.assertIn("测试标题", user)            # doc_title
        self.assertIn("# 测试文档", user)          # markdown
        _assert_no_placeholder(system + user)

    def test_doc_title_fallback(self):
        """doc_title=None 时，应回退显示 document_source。"""
        _, user = render_stage1(
            markdown="# x",
            document_source="fallback.docx",
        )
        self.assertIn("fallback.docx", user)

    def test_retry_errors_injected(self):
        errors = ["错误1：缺少字段", "错误2：doc_type 非法值"]
        _, user = render_stage1(
            markdown="# x",
            document_source="a.pdf",
            retry_errors=errors,
            previous_output='{"bad": true}',
        )
        self.assertIn("上一次输出存在以下错误", user)
        self.assertIn("错误1：缺少字段", user)
        self.assertIn("错误2：doc_type 非法值", user)
        self.assertIn('"bad": true', user)  # previous_output 注入

    def test_no_retry_block_when_none(self):
        _, user = render_stage1(markdown="# x", document_source="a.pdf")
        self.assertNotIn("上一次输出存在以下错误", user)


class TestRenderStage2(unittest.TestCase):
    """Stage 2 渲染。"""

    def test_basic_render_and_partition_prefix(self):
        system, user = render_stage2(
            cleaned_text="# 标题\n正文",
            doc_type="技术运维",
            partition_prefix="P3",
        )
        self.assertNotIn("===END_SYSTEM===", system)
        self.assertNotIn("===END_SYSTEM===", user)
        # partition_prefix 被渲染进 chunk_id 强制格式说明
        self.assertIn("P3-C001", system)
        self.assertIn("P3-C015", system)
        # 用户提示中的分片编号
        self.assertIn("P3", user)
        self.assertIn("技术运维", user)
        _assert_no_placeholder(system + user)

    def test_doc_type_rule_branch_制度合规(self):
        system, _ = render_stage2(
            cleaned_text="x",
            doc_type="制度合规",
            partition_prefix="P1",
        )
        self.assertIn("适用范围/准入条件", system)
        self.assertIn("禁止改写制度原文文字", system)

    def test_doc_type_rule_branch_FAQ(self):
        system, _ = render_stage2(
            cleaned_text="x",
            doc_type="FAQ",
            partition_prefix="P1",
        )
        self.assertIn("一问一答单独成块", system)

    def test_doc_type_rule_branch_产品活动(self):
        system, _ = render_stage2(
            cleaned_text="x",
            doc_type="产品活动",
            partition_prefix="P1",
        )
        self.assertIn("规格售价逐条拆分", system)
        self.assertIn("【已过期-禁止入库】", system)

    def test_doc_type_rule_branch_技术运维(self):
        system, _ = render_stage2(
            cleaned_text="x",
            doc_type="技术运维",
            partition_prefix="P1",
        )
        self.assertIn("故障原因", system)
        self.assertIn("解决方案", system)

    def test_title_context_present(self):
        _, user = render_stage2(
            cleaned_text="x",
            doc_type="制度合规",
            partition_prefix="P1",
            title_context="【账号管理】第三章",
        )
        self.assertIn("【账号管理】第三章", user)

    def test_retry_errors_injected(self):
        errs = ["chunk_id 格式错误"]
        _, user = render_stage2(
            cleaned_text="x",
            doc_type="FAQ",
            partition_prefix="P2",
            retry_errors=errs,
        )
        self.assertIn("chunk_id 格式错误", user)


class TestRenderStage3(unittest.TestCase):
    """Stage 3 渲染。"""

    SAMPLE_CHUNKS = [
        {
            "chunk_id": "P1-C001",
            "title": "【账号管理】- 登录失败处理",
            "content": "连续3次失败锁定30分钟。",
        },
        {
            "chunk_id": "P1-C002",
            "title": "【账号管理】- 解锁流程",
            "content": "联系管理员解锁。",
        },
    ]

    def test_basic_render_and_chunks_injected(self):
        system, user = render_stage3(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="技术运维",
            sensitive_items=[],
            chunk_id_prefix="P1",
        )
        self.assertNotIn("===END_SYSTEM===", system)
        self.assertNotIn("===END_SYSTEM===", user)
        self.assertIn("P1-C001", user)
        self.assertIn("P1-C002", user)
        self.assertIn("登录失败处理", user)
        self.assertIn("解锁流程", user)
        # loop 计数
        self.assertIn("知识块 1/2", user)
        self.assertIn("知识块 2/2", user)
        _assert_no_placeholder(system + user)

    def test_sensitive_items_list_injected_into_system_and_user(self):
        sens = [
            "第1章: 登录密码abc123-禁止向量化入库",
            "第2章: 手机号13800138000-禁止向量化入库",
        ]
        system, user = render_stage3(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="技术运维",
            sensitive_items=sens,
        )
        # system 注入
        self.assertIn("登录密码abc123", system)
        self.assertIn("手机号13800138000", system)
        # user 注入
        self.assertIn("登录密码abc123", user)
        self.assertIn("手机号13800138000", user)

    def test_sensitive_items_empty_branch(self):
        system, user = render_stage3(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="制度合规",
            sensitive_items=[],
        )
        self.assertIn("本份文档无已登记的敏感数据", system)
        self.assertIn("本份文档无已登记敏感数据", user)

    def test_doc_type_制度合规_branch(self):
        system, _ = render_stage3(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="制度合规",
            sensitive_items=[],
        )
        self.assertIn("违规后果、审批红线类问题重点覆盖", system)
        self.assertIn("模糊条款（\"视情况\"、\"酌情\"）一律打低分", system)

    def test_doc_type_FAQ_branch(self):
        system, _ = render_stage3(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="FAQ",
            sensitive_items=[],
        )
        self.assertIn("同义口语提问", system)

    def test_doc_type_产品活动_branch(self):
        system, _ = render_stage3(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="产品活动",
            sensitive_items=[],
        )
        self.assertIn("价格、时效、使用限制、叠加规则", system)
        self.assertIn("本活动已过期", system)

    def test_doc_type_技术运维_branch(self):
        system, _ = render_stage3(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="技术运维",
            sensitive_items=[],
        )
        self.assertIn("故障现象、故障原因、处置步骤分开设问", system)

    def test_retry_errors_injected(self):
        _, user = render_stage3(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="技术运维",
            sensitive_items=[],
            retry_errors=["低分未标【低置信】", "Answer 有建议"],
        )
        self.assertIn("低分未标【低置信】", user)
        self.assertIn("Answer 有建议", user)


class TestRenderStage4(unittest.TestCase):
    """Stage 4 渲染。"""

    SAMPLE_CHUNKS = [
        {
            "chunk_id": "P1-C001",
            "title": "【账号管理】- 登录失败处理",
            "content": "连续3次失败锁定30分钟，2026-01-01生效。",
        },
    ]

    def test_basic_render(self):
        system, user = render_stage4(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="技术运维",
        )
        self.assertNotIn("===END_SYSTEM===", system)
        self.assertNotIn("===END_SYSTEM===", user)
        self.assertIn("P1-C001", user)
        self.assertIn("5 字段必填", system)
        self.assertIn("target_audience", system)
        _assert_no_placeholder(system + user)

    def test_doc_type_制度合规_version_never_default_permanent(self):
        system, _ = render_stage4(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="制度合规",
        )
        self.assertIn("严格以正文文字为准", system)
        self.assertIn("禁止默认填「永久有效」", system)

    def test_doc_type_FAQ_fixed_knowledge_type(self):
        system, _ = render_stage4(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="FAQ",
        )
        self.assertIn("固定为「FAQ」", system)
        self.assertIn("不可自定义", system)

    def test_doc_type_产品活动_version_require_range(self):
        system, _ = render_stage4(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="产品活动",
        )
        self.assertIn("必须标注活动起止时间", system)
        self.assertIn("【已过期-禁止入库】", system)

    def test_doc_type_技术运维_knowledge_type_subdivision(self):
        system, _ = render_stage4(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="技术运维",
        )
        self.assertIn("故障排查", system)
        self.assertIn("操作步骤类", system)
        self.assertIn("版本说明", system)

    def test_retry_errors_injected(self):
        _, user = render_stage4(
            chunks=self.SAMPLE_CHUNKS,
            doc_type="制度合规",
            retry_errors=["summary 超 20 字", "version_timeliness 只写了未知"],
            previous_output='{"broken": 1}',
        )
        self.assertIn("summary 超 20 字", user)
        self.assertIn("version_timeliness 只写了未知", user)
        self.assertIn('"broken": 1', user)


if __name__ == "__main__":
    unittest.main()
