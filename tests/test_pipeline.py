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
    Stage1Output,
    Stage2Input,
    Stage2Output,
    Stage3Input,
    Stage3Output,
    Stage4Input,
    Stage4Output,
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


# =====================================================================
# Stage 3/4 分批并行测试
# =====================================================================


def _make_chunks_dict(n: int) -> list[dict]:
    """构造 n 个 Stage 2 风格的 chunks 输入。"""
    return [
        {"chunk_id": f"P1-C{i:03d}", "title": f"标题{i}", "content": f"内容{i}", "remark": ""}
        for i in range(1, n + 1)
    ]


class TestPipelineBatchParallel(unittest.TestCase):
    """Stage 3/4 分批并行：拆批有序、chunks 顺序合并、exception_list 归并、信号量限流。"""

    @staticmethod
    def _make_pipeline(tmpdir: str, **overrides) -> Pipeline:
        config = PipelineConfig(
            output_dir=Path(tmpdir),
            stage_retries=1,
            enable_checkpoint=False,
            **overrides,
        )
        return Pipeline(DeepSeekClient(config=LLMConfig(api_key="test-key")), config)

    @staticmethod
    def _stage3_output_for(chunks: list[dict]) -> Stage3Output:
        """为一批 chunks 构造 Stage 3 输出（每 chunk 1 条 QA + 本批异常）。"""
        return Stage3Output.model_validate({
            "chunks": [
                {
                    "chunk_id": c["chunk_id"],
                    "qa_pairs": [{
                        "question": f"问题-{c['chunk_id']}",
                        "answer": f"答案-{c['chunk_id']}",
                        "keywords": ["关键词"],
                        "confidence_score": 90,
                        "remark": "",
                    }],
                }
                for c in chunks
            ],
            "exception_list": {
                "content_conflicts": [f"{c['chunk_id']}: S3批次异常" for c in chunks],
            },
        })

    @staticmethod
    def _stage4_output_for(chunks: list[dict]) -> Stage4Output:
        """为一批 chunks 构造 Stage 4 输出（每 chunk 1 份 metadata + 本批异常）。"""
        return Stage4Output.model_validate({
            "chunks": [
                {
                    "chunk_id": c["chunk_id"],
                    "doc_type": "制度合规",
                    "metadata": {
                        "target_audience": "普通用户",
                        "business_module": "账号管理",
                        "knowledge_type": "制度规则",
                        "version_timeliness": "2026-01-01生效",
                        "summary": f"摘要-{c['chunk_id']}",
                    },
                }
                for c in chunks
            ],
            "exception_list": {
                "missing_info": [f"{c['chunk_id']}: S4批次缺失" for c in chunks],
            },
        })

    def test_split_chunks_ordered(self):
        """按 batch_size 有序切分，最后一批允许不满；size<=1 或不超限不分批。"""
        with TemporaryDirectory() as tmpdir:
            pipeline = self._make_pipeline(tmpdir, stage34_batch_size=3)

            chunks = [f"c{i}" for i in range(7)]
            self.assertEqual(
                pipeline._split_chunks(chunks),
                [["c0", "c1", "c2"], ["c3", "c4", "c5"], ["c6"]],
            )
            # 数量不超过 batch_size → 单批
            self.assertEqual(pipeline._split_chunks(["a", "b"]), [["a", "b"]])
            # batch_size <= 1 → 不分批
            pipeline._config.stage34_batch_size = 1
            self.assertEqual(pipeline._split_chunks(["a", "b"]), [["a", "b"]])

    def test_merge_exception_lists_all_fields(self):
        """_merge_exception_lists 覆盖全部 9 个字段且按批顺序拼接。"""
        all_fields = [
            "content_conflicts", "missing_info", "vague_items", "expired_items",
            "chunk_anomalies", "truncated_items", "sensitive_items",
            "low_confidence_qa", "terminology_pending",
        ]
        e1 = ExceptionList(**{f: [f"{f}-b1"] for f in all_fields})
        e2 = ExceptionList(**{f: [f"{f}-b2"] for f in all_fields})

        merged = Pipeline._merge_exception_lists([e1, e2])

        for f in all_fields:
            self.assertEqual(getattr(merged, f), [f"{f}-b1", f"{f}-b2"], f)

        # 空输入 → 全空
        empty = Pipeline._merge_exception_lists([])
        for f in all_fields:
            self.assertEqual(getattr(empty, f), [])

    def test_stage34_batched_merge(self):
        """6 chunks / batch_size=2 → 每阶段 3 批；合并后 chunks 保序、exception_list 归并。"""
        with TemporaryDirectory() as tmpdir:
            chunks = _make_chunks_dict(6)
            stage2_out = {"chunks": chunks, "exception_list": {}}

            # Stage 1/2 走真实 runner（顺序 mock LLM），Stage 3/4 打桩按批返回
            pipeline = self._make_pipeline(tmpdir, stage34_batch_size=2, stage34_concurrency=3)

            stage_outputs: dict[str, object] = {}

            def on_complete(name, output):
                stage_outputs[name] = output

            llm_sequence = [_make_stage1_output_dict(), stage2_out]
            llm_calls = [0]

            async def mock_llm(**kwargs):
                if llm_calls[0] >= len(llm_sequence):
                    raise AssertionError("Stage 3/4 已打桩，LLM 不应被再次调用")
                result = _make_chat_result(llm_sequence[llm_calls[0]])
                llm_calls[0] += 1
                return result

            pipeline._client.chat_json_async = mock_llm  # type: ignore

            s3_batches: list[list[str]] = []
            s4_batches: list[list[str]] = []

            async def mock_run_stage3(stage_input, client, **kwargs):
                s3_batches.append([c["chunk_id"] for c in stage_input.chunks])
                return self._stage3_output_for(stage_input.chunks)

            async def mock_run_stage4(stage_input, client, **kwargs):
                s4_batches.append([c["chunk_id"] for c in stage_input.chunks])
                return self._stage4_output_for(stage_input.chunks)

            with patch("kbrefiner.core.pipeline.orchestrator.run_stage3", mock_run_stage3), \
                 patch("kbrefiner.core.pipeline.orchestrator.run_stage4", mock_run_stage4):
                doc = asyncio.run(pipeline.run(
                    markdown="# x",
                    document_source="test.pdf",
                    on_stage_complete=on_complete,
                ))

            expected_ids = [c["chunk_id"] for c in chunks]

            # 每批恰好 2 个 chunk，批内连续，全部批覆盖 6 个 chunk
            self.assertEqual(len(s3_batches), 3)
            self.assertEqual(len(s4_batches), 3)
            for batches in (s3_batches, s4_batches):
                flat = [cid for batch in batches for cid in batch]
                self.assertEqual(sorted(flat), sorted(expected_ids))

            # 合并结果保持原始顺序（gather 按任务序返回）
            s3_out = stage_outputs["Stage3-QA"]
            self.assertEqual([c.chunk_id for c in s3_out.chunks], expected_ids)
            self.assertEqual(
                s3_out.exception_list.content_conflicts,
                [f"{cid}: S3批次异常" for cid in expected_ids],
            )

            s4_out = stage_outputs["Stage4-Tag"]
            self.assertEqual([c.chunk_id for c in s4_out.chunks], expected_ids)
            self.assertEqual(
                s4_out.exception_list.missing_info,
                [f"{cid}: S4批次缺失" for cid in expected_ids],
            )

            # 最终文档 6 个原子，顺序一致，QA 与 metadata 均已合入
            self.assertEqual(len(doc.knowledge_atoms), 6)
            self.assertEqual(
                [a.chunk_id for a in doc.knowledge_atoms], expected_ids
            )
            for atom in doc.knowledge_atoms:
                self.assertEqual(len(atom.qa_pairs), 1)
                self.assertEqual(atom.qa_pairs[0].question, f"问题-{atom.chunk_id}")
                self.assertEqual(atom.metadata.summary, f"摘要-{atom.chunk_id}")

    def test_stage34_semaphore_limits_concurrency(self):
        """批间并发受 stage34_concurrency 信号量约束。"""
        with TemporaryDirectory() as tmpdir:
            # 6 chunks / batch_size=2 → 3 批，并发上限 2（batch_size<=1 表示不分批）
            chunks = _make_chunks_dict(6)
            stage2_out = {"chunks": chunks, "exception_list": {}}

            pipeline = self._make_pipeline(
                tmpdir, stage34_batch_size=2, stage34_concurrency=2
            )

            llm_sequence = [_make_stage1_output_dict(), stage2_out]
            llm_calls = [0]

            async def mock_llm(**kwargs):
                result = _make_chat_result(llm_sequence[min(llm_calls[0], 1)])
                llm_calls[0] += 1
                return result

            pipeline._client.chat_json_async = mock_llm  # type: ignore

            active = {"s3": 0, "s4": 0}
            peak = {"s3": 0, "s4": 0}

            async def mock_run_stage3(stage_input, client, **kwargs):
                active["s3"] += 1
                peak["s3"] = max(peak["s3"], active["s3"])
                await asyncio.sleep(0.1)
                active["s3"] -= 1
                return self._stage3_output_for(stage_input.chunks)

            async def mock_run_stage4(stage_input, client, **kwargs):
                active["s4"] += 1
                peak["s4"] = max(peak["s4"], active["s4"])
                await asyncio.sleep(0.1)
                active["s4"] -= 1
                return self._stage4_output_for(stage_input.chunks)

            with patch("kbrefiner.core.pipeline.orchestrator.run_stage3", mock_run_stage3), \
                 patch("kbrefiner.core.pipeline.orchestrator.run_stage4", mock_run_stage4):
                asyncio.run(pipeline.run(markdown="# x", document_source="test.pdf"))

            # 3 批 / 并发上限 2 → 峰值恰好为 2（sleep 保证并发窗口存在）
            self.assertEqual(peak["s3"], 2)
            self.assertEqual(peak["s4"], 2)


# =====================================================================
# Stage 1 分段并行测试
# =====================================================================


class TestStage1Segmented(unittest.TestCase):
    """Stage 1 分段并行：段落边界切分、多数表决合并、清单去重、流水线串联。"""

    @staticmethod
    def _make_pipeline(tmpdir: str, **overrides) -> Pipeline:
        config = PipelineConfig(
            output_dir=Path(tmpdir),
            stage_retries=1,
            enable_checkpoint=False,
            **overrides,
        )
        return Pipeline(DeepSeekClient(config=LLMConfig(api_key="test-key")), config)

    def test_split_markdown_paragraph_boundaries(self):
        """按段落边界累积切段：不超限不分段、单段超限独立成段、顺序保持。"""
        with TemporaryDirectory() as tmpdir:
            pipeline = self._make_pipeline(tmpdir, stage1_segment_chars=100)

            # 不超限 → 单段
            self.assertEqual(pipeline._split_markdown("短文本"), ["短文本"])
            # 禁用（0）→ 单段
            pipeline._config.stage1_segment_chars = 0
            long_text = "x" * 500
            self.assertEqual(pipeline._split_markdown(long_text), [long_text])
            pipeline._config.stage1_segment_chars = 100

            # p1(59) + p2(20) ≤ 100 同段；追加 p3(59) 超限 → 切成 2 段
            p1 = "P1_" + "a" * 56  # 59 chars
            p2 = "P2_" + "b" * 17  # 20 chars
            p3 = "P3_" + "c" * 56  # 59 chars
            segments = pipeline._split_markdown("\n\n".join([p1, p2, p3]))
            self.assertEqual(len(segments), 2)
            self.assertEqual(segments[0], f"{p1}\n\n{p2}")
            self.assertEqual(segments[1], p3)
            # 拼回原文无损
            self.assertEqual("\n\n".join(segments), "\n\n".join([p1, p2, p3]))

            # 单段本身超限 → 独立成段不硬切
            big = "B" * 300
            segments = pipeline._split_markdown(f"{big}\n\n{big}")
            self.assertEqual(segments, [big, big])

    def test_merge_stage1_outputs(self):
        """合并：doc_type 多数表决、cleaned_text 按序拼接、清单拼接去重。"""
        def _part(idx: int, doc_type: DocType, sens: list[str], terms: list[str]) -> Stage1Output:
            return Stage1Output(
                cleaned_text=f"\n\n第{idx}段清洗结果\n\n",
                doc_type=doc_type,
                sensitive_items=sens,
                terminology_pending=terms,
            )

        parts = [
            _part(1, DocType.COMPLIANCE, ["s1"], ["红宝识 / 红宝石"]),
            _part(2, DocType.FAQ, ["s2a", "s2b"], ["红宝识 / 红宝石", "水机 / 饮水机"]),
            _part(3, DocType.COMPLIANCE, ["s1"], []),
        ]

        merged = Pipeline._merge_stage1_outputs(parts)

        # 多数表决：COMPLIANCE 2 票 vs FAQ 1 票
        self.assertEqual(merged.doc_type, DocType.COMPLIANCE)
        # cleaned_text 按段序拼接（strip 后以空行连接）
        self.assertEqual(merged.cleaned_text, "第1段清洗结果\n\n第2段清洗结果\n\n第3段清洗结果")
        # sensitive_items 去重（s1 重复报告）
        self.assertEqual(merged.sensitive_items, ["s1", "s2a", "s2b"])
        # terminology_pending 去重（跨段同一称谓对）
        self.assertEqual(merged.terminology_pending, ["红宝识 / 红宝石", "水机 / 饮水机"])

    def test_stage1_segmented_pipeline(self):
        """markdown 超限时分段并行执行，合并结果进入 Stage 2。"""
        with TemporaryDirectory() as tmpdir:
            # 构造 3 段 markdown（每章为单段落 ~66 chars，阈值 100 → 3 个分段）
            paras = [f"# 第{i}章 " + "内容" * 30 for i in range(1, 4)]
            markdown = "\n\n".join(paras)

            pipeline = self._make_pipeline(
                tmpdir, stage1_segment_chars=100, stage1_concurrency=2
            )

            received_segments: list[str] = []
            stage_outputs: dict[str, object] = {}

            def on_complete(name, output):
                stage_outputs[name] = output

            async def mock_run_stage1(stage_input, client, **kwargs):
                received_segments.append(stage_input.markdown)
                idx = len(received_segments)
                return Stage1Output(
                    cleaned_text=f"[cleaned-{idx}]",
                    doc_type=DocType.COMPLIANCE if idx != 2 else DocType.FAQ,
                    sensitive_items=[f"s{idx}"],
                    terminology_pending=[],
                )

            # Stage 2 走真实 runner（mock LLM），Stage 3/4 打桩
            stage2_out = {
                "chunks": [
                    {"chunk_id": "P1-C001", "title": "t", "content": "c", "remark": ""},
                ],
                "exception_list": {},
            }
            llm_calls = [0]

            async def mock_llm(**kwargs):
                result = _make_chat_result(stage2_out)
                llm_calls[0] += 1
                return result

            pipeline._client.chat_json_async = mock_llm  # type: ignore

            async def mock_run_stage3(stage_input, client, **kwargs):
                return Stage3Output.model_validate({
                    "chunks": [{"chunk_id": "P1-C001", "qa_pairs": [{
                        "question": "q", "answer": "a", "keywords": ["k"],
                        "confidence_score": 90, "remark": "",
                    }]}],
                    "exception_list": {},
                })

            async def mock_run_stage4(stage_input, client, **kwargs):
                return Stage4Output.model_validate({
                    "chunks": [{"chunk_id": "P1-C001", "doc_type": "制度合规", "metadata": {
                        "target_audience": "普通用户", "business_module": "账号管理",
                        "knowledge_type": "制度规则", "version_timeliness": "2026-01-01生效",
                        "summary": "s",
                    }}],
                    "exception_list": {},
                })

            with patch("kbrefiner.core.pipeline.orchestrator.run_stage1", mock_run_stage1), \
                 patch("kbrefiner.core.pipeline.orchestrator.run_stage3", mock_run_stage3), \
                 patch("kbrefiner.core.pipeline.orchestrator.run_stage4", mock_run_stage4):
                doc = asyncio.run(pipeline.run(
                    markdown=markdown,
                    document_source="seg_test.pdf",
                    on_stage_complete=on_complete,
                ))

            # 分段覆盖全部原文且顺序保持
            self.assertEqual(received_segments, paras)

            # 合并结果：cleaned_text 按段序拼接、doc_type 多数表决
            s1 = stage_outputs["Stage1-Clean"]
            self.assertEqual(s1.cleaned_text, "[cleaned-1]\n\n[cleaned-2]\n\n[cleaned-3]")
            self.assertEqual(s1.doc_type, DocType.COMPLIANCE)
            self.assertEqual(s1.sensitive_items, ["s1", "s2", "s3"])

            # 流水线完整走通
            self.assertEqual(doc.document_info.source, "seg_test.pdf")
            self.assertEqual(len(doc.knowledge_atoms), 1)

    def test_stage1_small_markdown_single_call(self):
        """markdown 未超限时只调用一次 run_stage1（不分段）。"""
        with TemporaryDirectory() as tmpdir:
            pipeline = self._make_pipeline(tmpdir, stage1_segment_chars=10000)

            calls = [0]

            async def mock_run_stage1(stage_input, client, **kwargs):
                calls[0] += 1
                return Stage1Output.model_validate(_make_stage1_output_dict())

            async def mock_run_stage2(stage_input, client, **kwargs):
                return Stage2Output.model_validate(_make_stage2_output_dict())

            async def mock_run_stage3(stage_input, client, **kwargs):
                return Stage3Output.model_validate(_make_stage3_output_dict())

            async def mock_run_stage4(stage_input, client, **kwargs):
                return Stage4Output.model_validate(_make_stage4_output_dict())

            with patch("kbrefiner.core.pipeline.orchestrator.run_stage1", mock_run_stage1), \
                 patch("kbrefiner.core.pipeline.orchestrator.run_stage2", mock_run_stage2), \
                 patch("kbrefiner.core.pipeline.orchestrator.run_stage3", mock_run_stage3), \
                 patch("kbrefiner.core.pipeline.orchestrator.run_stage4", mock_run_stage4):
                asyncio.run(pipeline.run(markdown="# 短文档", document_source="small.pdf"))

            self.assertEqual(calls[0], 1)


if __name__ == "__main__":
    unittest.main()
