"""流水线编排单元测试。

全部 mock DeepSeekClient.chat_json_async，不发起真实 LLM 调用。

覆盖：
- Stage 1/2/3/4 单独执行（成功）
- 断点校验失败 + 重试成功
- 重试耗尽抛 StageValidationError
- 完整 Pipeline 串联（4 阶段 + merge + 持久化）
- Stage 3/4 并行执行
- 断点续跑（已有中间结果跳过 LLM 调用）
- postprocess merge（chunk_id 不匹配 + exception_list 归集去重）
- quality_summary 重算

运行：python -m unittest tests.test_pipeline -v
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from kbrefiner.core.llm import ChatResult, DeepSeekClient, LLMConfig
from kbrefiner.core.pipeline import (
    Pipeline,
    PipelineConfig,
    StageValidationError,
    merge_exception_lists,
    merge_stages_to_document,
    run_stage1,
    run_stage2,
    run_stage3,
    run_stage4,
)
from kbrefiner.models import (
    DocType,
    ExceptionList,
    Stage1Input,
    Stage2Input,
    Stage3Input,
    Stage4Input,
)


# =====================================================================
# Mock 数据工厂
# =====================================================================


def _make_chat_result(parsed: dict, content: str = "") -> ChatResult:
    """构造 mock ChatResult。"""
    return ChatResult(
        content=content or json.dumps(parsed, ensure_ascii=False),
        parsed=parsed,
        model="deepseek-v4-flash",
        usage={"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        raw=None,
        attempts=1,
    )


def _make_stage1_output_dict() -> dict:
    return {
        "cleaned_text": "# 登录安全制度\n\n连续3次失败锁定30分钟。",
        "doc_type": "制度合规",
        "sensitive_items": ["第1章: 服务器密码abc123-禁止向量化入库"],
        "terminology_pending": [],
    }


def _make_stage2_output_dict() -> dict:
    return {
        "chunks": [
            {
                "chunk_id": "P1-C001",
                "title": "【账号管理】- 登录失败锁定",
                "content": "连续3次输入密码失败，账号锁定30分钟。",
                "remark": "",
            },
            {
                "chunk_id": "P1-C002",
                "title": "【账号管理】- 解锁流程",
                "content": "联系管理员在后台手动解锁账号。",
                "remark": "",
            },
        ],
        "exception_list": {
            "chunk_anomalies": ["P1-C002: 拆分颗粒度异常-内容15字"],
        },
    }


def _make_stage3_output_dict() -> dict:
    return {
        "chunks": [
            {
                "chunk_id": "P1-C001",
                "qa_pairs": [
                    {
                        "question": "密码输错几次会锁？",
                        "answer": "连续3次失败锁定30分钟。",
                        "keywords": ["密码", "锁定", "失败"],
                        "confidence_score": 90,
                        "remark": "",
                    },
                    {
                        "question": "账号被锁了怎么办？",
                        "answer": "联系管理员解锁。",
                        "keywords": ["解锁", "管理员"],
                        "confidence_score": 85,
                        "remark": "",
                    },
                ],
            },
            {
                "chunk_id": "P1-C002",
                "qa_pairs": [
                    {
                        "question": "怎么解锁账号？",
                        "answer": "联系管理员在后台手动解锁。",
                        "keywords": ["解锁", "管理员", "后台"],
                        "confidence_score": 88,
                        "remark": "",
                    },
                ],
            },
        ],
        "exception_list": {},
    }


def _make_stage4_output_dict() -> dict:
    return {
        "chunks": [
            {
                "chunk_id": "P1-C001",
                "doc_type": "制度合规",
                "metadata": {
                    "target_audience": "普通用户",
                    "business_module": "账号管理",
                    "knowledge_type": "制度规则",
                    "version_timeliness": "2026-01-01生效",
                    "summary": "登录失败锁定规则",
                },
                "remark": "",
            },
            {
                "chunk_id": "P1-C002",
                "doc_type": "制度合规",
                "metadata": {
                    "target_audience": "管理员",
                    "business_module": "账号管理",
                    "knowledge_type": "操作指南",
                    "version_timeliness": "2026-01-01生效",
                    "summary": "账号解锁流程",
                },
                "remark": "",
            },
        ],
        "exception_list": {
            "missing_info": ["P1-C002: 缺少version_timeliness细节"],
        },
    }


def _make_mock_llm_client(return_sequence: list[dict] | dict) -> DeepSeekClient:
    """构造 mock LLM 客户端。

    Args:
        return_sequence: 单个 dict（每次返回相同）或 dict 列表（按顺序返回，用于重试测试）
    """
    cfg = LLMConfig(api_key="test-key")
    client = DeepSeekClient(config=cfg)

    if isinstance(return_sequence, dict):
        results = [_make_chat_result(return_sequence)]
    else:
        results = [_make_chat_result(d) for d in return_sequence]

    call_count = [0]

    async def mock_chat_json_async(**kwargs):
        idx = min(call_count[0], len(results) - 1)
        call_count[0] += 1
        return results[idx]

    client.chat_json_async = mock_chat_json_async  # type: ignore
    return client


# =====================================================================
# Stage 1 测试
# =====================================================================


class TestRunStage1(unittest.TestCase):
    """Stage 1 执行器。"""

    def test_success(self):
        client = _make_mock_llm_client(_make_stage1_output_dict())
        stage_input = Stage1Input(markdown="# x", document_source="test.pdf")

        result = asyncio.run(run_stage1(stage_input, client, max_retries=1))

        self.assertEqual(result.doc_type, DocType.COMPLIANCE)
        self.assertEqual(len(result.sensitive_items), 1)
        self.assertIn("密码", result.sensitive_items[0])

    def test_validation_retry_then_success(self):
        """断点校验失败后重试成功。"""
        bad_output = {"cleaned_text": "", "doc_type": "制度合规"}  # cleaned_text 空
        good_output = _make_stage1_output_dict()
        client = _make_mock_llm_client([bad_output, good_output])

        stage_input = Stage1Input(markdown="# x", document_source="test.pdf")
        result = asyncio.run(run_stage1(stage_input, client, max_retries=2))

        self.assertEqual(result.doc_type, DocType.COMPLIANCE)
        self.assertTrue(result.cleaned_text)

    def test_retries_exhausted_raises(self):
        """重试耗尽抛 StageValidationError。"""
        bad_output = {"cleaned_text": "", "doc_type": "制度合规"}
        client = _make_mock_llm_client([bad_output, bad_output, bad_output])

        stage_input = Stage1Input(markdown="# x", document_source="test.pdf")
        with self.assertRaises(StageValidationError):
            asyncio.run(run_stage1(stage_input, client, max_retries=2))


# =====================================================================
# Stage 2 测试
# =====================================================================


class TestRunStage2(unittest.TestCase):
    """Stage 2 执行器。"""

    def test_success(self):
        client = _make_mock_llm_client(_make_stage2_output_dict())
        stage_input = Stage2Input(
            cleaned_text="内容", doc_type=DocType.COMPLIANCE, partition_prefix="P1"
        )

        result = asyncio.run(run_stage2(stage_input, client, max_retries=1))

        self.assertEqual(len(result.chunks), 2)
        self.assertEqual(result.chunks[0].chunk_id, "P1-C001")
        self.assertEqual(len(result.exception_list.chunk_anomalies), 1)

    def test_invalid_chunk_id_retry(self):
        """chunk_id 格式错误 → 重试 → 成功。"""
        bad = {
            "chunks": [{
                "chunk_id": "chunk_001",  # 格式错误
                "title": "x", "content": "y", "remark": "",
            }],
            "exception_list": {},
        }
        good = _make_stage2_output_dict()
        client = _make_mock_llm_client([bad, good])

        stage_input = Stage2Input(
            cleaned_text="x", doc_type=DocType.COMPLIANCE, partition_prefix="P1"
        )
        result = asyncio.run(run_stage2(stage_input, client, max_retries=2))
        self.assertEqual(result.chunks[0].chunk_id, "P1-C001")


# =====================================================================
# Stage 3 测试
# =====================================================================


class TestRunStage3(unittest.TestCase):
    """Stage 3 执行器。"""

    def test_success(self):
        client = _make_mock_llm_client(_make_stage3_output_dict())
        stage_input = Stage3Input(
            chunks=[{"chunk_id": "P1-C001", "title": "x", "content": "y"}],
            doc_type=DocType.COMPLIANCE,
            sensitive_items=[],
        )

        result = asyncio.run(run_stage3(stage_input, client, max_retries=1))

        self.assertEqual(len(result.chunks), 2)
        self.assertEqual(len(result.chunks[0].qa_pairs), 2)
        self.assertEqual(result.chunks[0].qa_pairs[0].confidence_score, 90)


# =====================================================================
# Stage 4 测试
# =====================================================================


class TestRunStage4(unittest.TestCase):
    """Stage 4 执行器。"""

    def test_success(self):
        client = _make_mock_llm_client(_make_stage4_output_dict())
        stage_input = Stage4Input(
            chunks=[{"chunk_id": "P1-C001", "title": "x", "content": "y"}],
            doc_type=DocType.COMPLIANCE,
        )

        result = asyncio.run(run_stage4(stage_input, client, max_retries=1))

        self.assertEqual(len(result.chunks), 2)
        self.assertEqual(result.chunks[0].metadata.target_audience, "普通用户")
        self.assertEqual(result.chunks[0].metadata.summary, "登录失败锁定规则")


# =====================================================================
# Postprocess merge 测试
# =====================================================================


class TestMergeStages(unittest.TestCase):
    """后处理 merge。"""

    def test_merge_success(self):
        from kbrefiner.models import Stage1Output, Stage2Output, Stage3Output, Stage4Output

        s1 = Stage1Output.model_validate(_make_stage1_output_dict())
        s2 = Stage2Output.model_validate(_make_stage2_output_dict())
        s3 = Stage3Output.model_validate(_make_stage3_output_dict())
        s4 = Stage4Output.model_validate(_make_stage4_output_dict())

        doc = merge_stages_to_document(s1, s2, s3, s4, "test.pdf")

        # document_info
        self.assertEqual(doc.document_info.source, "test.pdf")
        self.assertEqual(doc.document_info.doc_type, DocType.COMPLIANCE)
        self.assertEqual(doc.document_info.total_chunks, 2)
        self.assertEqual(doc.document_info.total_qa_pairs, 3)  # 2 + 1

        # knowledge_atoms
        self.assertEqual(len(doc.knowledge_atoms), 2)
        atom0 = doc.knowledge_atoms[0]
        self.assertEqual(atom0.chunk_id, "P1-C001")
        self.assertEqual(atom0.title, "【账号管理】- 登录失败锁定")
        self.assertEqual(len(atom0.qa_pairs), 2)
        self.assertEqual(atom0.metadata.summary, "登录失败锁定规则")

        # exception_list 归集（Stage 1 sensitive + Stage 2 chunk_anomalies + Stage 4 missing_info）
        self.assertGreaterEqual(len(doc.exception_list.sensitive_items), 1)
        self.assertGreaterEqual(len(doc.exception_list.chunk_anomalies), 1)
        self.assertGreaterEqual(len(doc.exception_list.missing_info), 1)

        # quality_summary 已重算
        self.assertGreater(doc.quality_summary.avg_confidence, 0)
        # 3 条 QA 平均分 = (90+85+88)/3 = 87.67
        self.assertAlmostEqual(doc.quality_summary.avg_confidence, 87.7, places=1)
        # 有异常项（sensitive_items/chunk_anomalies/missing_info），needs_review 应为 True
        self.assertTrue(doc.quality_summary.needs_review)
        self.assertGreater(doc.quality_summary.exception_count, 0)

    def test_merge_chunk_id_mismatch(self):
        """Stage 3 缺少某 chunk_id → 登记到 missing_info。"""
        from kbrefiner.models import Stage1Output, Stage2Output, Stage3Output, Stage4Output

        s1 = Stage1Output.model_validate(_make_stage1_output_dict())
        s2 = Stage2Output.model_validate(_make_stage2_output_dict())
        s4 = Stage4Output.model_validate(_make_stage4_output_dict())

        # Stage 3 缺少 P1-C002
        s3_dict = _make_stage3_output_dict()
        s3_dict["chunks"] = [s3_dict["chunks"][0]]  # 只保留 C001
        s3 = Stage3Output.model_validate(s3_dict)

        doc = merge_stages_to_document(s1, s2, s3, s4, "test.pdf")

        # missing_info 应包含 Stage 3 缺失
        missing_text = " ".join(doc.exception_list.missing_info)
        self.assertIn("P1-C002", missing_text)
        self.assertIn("Stage 3", missing_text)


class TestMergeExceptionLists(unittest.TestCase):
    """exception_list 归集去重。"""

    def test_merge_deduplicates(self):
        exc1 = ExceptionList(
            sensitive_items=["item_a", "item_b"],
            missing_info=["info1"],
        )
        exc2 = ExceptionList(
            sensitive_items=["item_b", "item_c"],  # item_b 重复
            chunk_anomalies=["anomaly1"],
        )

        merged = merge_exception_lists(exc1, exc2)

        self.assertEqual(merged.sensitive_items, ["item_a", "item_b", "item_c"])
        self.assertEqual(merged.missing_info, ["info1"])
        self.assertEqual(merged.chunk_anomalies, ["anomaly1"])
        # 未涉及的字段为空
        self.assertEqual(merged.terminology_pending, [])

    def test_merge_empty(self):
        merged = merge_exception_lists()
        for field in ["sensitive_items", "missing_info", "chunk_anomalies"]:
            self.assertEqual(getattr(merged, field), [])


# =====================================================================
# 完整 Pipeline 测试
# =====================================================================


class TestPipelineFullRun(unittest.TestCase):
    """完整流水线串联。"""

    def test_full_pipeline_success(self):
        """4 阶段串联 + merge + 持久化。"""
        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            # mock LLM 按顺序返回 4 个 Stage 的输出
            call_count = [0]
            stage_outputs = [
                _make_stage1_output_dict(),
                _make_stage2_output_dict(),
                _make_stage3_output_dict(),
                _make_stage4_output_dict(),
            ]

            cfg = LLMConfig(api_key="test-key")
            client = DeepSeekClient(config=cfg)

            async def mock_chat_json_async(**kwargs):
                idx = min(call_count[0], len(stage_outputs) - 1)
                call_count[0] += 1
                return _make_chat_result(stage_outputs[idx])

            client.chat_json_async = mock_chat_json_async  # type: ignore

            config = PipelineConfig(
                output_dir=output_dir,
                stage_retries=1,
                enable_checkpoint=True,
            )
            pipeline = Pipeline(client, config)

            doc = asyncio.run(pipeline.run(
                markdown="# 测试文档\n正文",
                document_source="test.pdf",
            ))

            # 验证最终文档
            self.assertEqual(doc.document_info.source, "test.pdf")
            self.assertEqual(len(doc.knowledge_atoms), 2)
            self.assertEqual(doc.document_info.total_qa_pairs, 3)

            # 验证持久化文件
            self.assertTrue((output_dir / "stage1.json").exists())
            self.assertTrue((output_dir / "stage2.json").exists())
            self.assertTrue((output_dir / "stage3.json").exists())
            self.assertTrue((output_dir / "stage4.json").exists())
            self.assertTrue((output_dir / "final.json").exists())

            # 验证 final.json 可解析
            final_data = json.loads((output_dir / "final.json").read_text(encoding="utf-8"))
            self.assertEqual(final_data["document_info"]["source"], "test.pdf")

    def test_progress_callback(self):
        """进度回调被调用。"""
        with TemporaryDirectory() as tmpdir:
            cfg = LLMConfig(api_key="test-key")
            client = DeepSeekClient(config=cfg)

            stage_outputs = [
                _make_stage1_output_dict(),
                _make_stage2_output_dict(),
                _make_stage3_output_dict(),
                _make_stage4_output_dict(),
            ]
            call_count = [0]

            async def mock_chat_json_async(**kwargs):
                idx = min(call_count[0], len(stage_outputs) - 1)
                call_count[0] += 1
                return _make_chat_result(stage_outputs[idx])

            client.chat_json_async = mock_chat_json_async  # type: ignore

            config = PipelineConfig(
                output_dir=Path(tmpdir),
                stage_retries=1,
                enable_checkpoint=False,
            )
            pipeline = Pipeline(client, config)

            completed_stages: list[str] = []

            def on_complete(stage_name, output):
                completed_stages.append(stage_name)

            asyncio.run(pipeline.run(
                markdown="# x",
                document_source="test.pdf",
                on_stage_complete=on_complete,
            ))

            # 应至少有 4 个 Stage + Postprocess
            self.assertGreaterEqual(len(completed_stages), 4)
            self.assertIn("Stage1-Clean", completed_stages)
            self.assertIn("Stage2-Chunk", completed_stages)
            self.assertIn("Stage3-QA", completed_stages)
            self.assertIn("Stage4-Tag", completed_stages)


class TestPipelineCheckpoint(unittest.TestCase):
    """断点续跑。"""

    def test_checkpoint_skips_llm_call(self):
        """已有中间结果时跳过 LLM 调用。"""
        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            # 预先写入 Stage 1/2/3/4 的中间结果
            (output_dir / "stage1.json").write_text(
                json.dumps(_make_stage1_output_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (output_dir / "stage2.json").write_text(
                json.dumps(_make_stage2_output_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (output_dir / "stage3.json").write_text(
                json.dumps(_make_stage3_output_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (output_dir / "stage4.json").write_text(
                json.dumps(_make_stage4_output_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # mock LLM（如果被调用会报错）
            cfg = LLMConfig(api_key="test-key")
            client = DeepSeekClient(config=cfg)

            async def mock_should_not_be_called(**kwargs):
                raise AssertionError("断点续跑不应调用 LLM")

            client.chat_json_async = mock_should_not_be_called  # type: ignore

            config = PipelineConfig(output_dir=output_dir, enable_checkpoint=True)
            pipeline = Pipeline(client, config)

            doc = asyncio.run(pipeline.run(
                markdown="# x", document_source="test.pdf"
            ))

            # 应从断点恢复，不调用 LLM
            self.assertEqual(doc.document_info.source, "test.pdf")
            self.assertEqual(len(doc.knowledge_atoms), 2)

    def test_clear_checkpoints(self):
        """清除中间结果。"""
        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "stage1.json").write_text("{}", encoding="utf-8")
            (output_dir / "final.json").write_text("{}", encoding="utf-8")

            cfg = LLMConfig(api_key="test-key")
            pipeline = Pipeline(DeepSeekClient(config=cfg),
                               PipelineConfig(output_dir=output_dir))
            pipeline.clear_checkpoints()

            self.assertFalse((output_dir / "stage1.json").exists())
            self.assertFalse((output_dir / "final.json").exists())


if __name__ == "__main__":
    unittest.main()
