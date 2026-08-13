"""端到端集成测试。

模拟完整的用户操作流程：上传 → 处理 → 查状态 → 看结果 → WebSocket 进度。
mock 掉 MinerU 解析和 LLM 调用，只验证 HTTP 层面的端到端流程。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.config import Settings, get_settings
from app.core.llm import DeepSeekClient, LLMConfig
from app.core.pipeline import Pipeline, PipelineConfig
from app.models import KbDocument, DocType


def _make_mock_pipeline_output() -> KbDocument:
    """构造一个完整的 Pipeline 模拟输出。"""
    from app.models import (
        DocumentInfo,
        ExceptionList,
        KnowledgeAtom,
        Metadata,
        QaPair,
        QualitySummary,
    )

    return KbDocument(
        document_info=DocumentInfo(
            source="e2e_test.pdf",
            doc_type=DocType.COMPLIANCE,
            total_chunks=2,
            total_qa_pairs=3,
            processing_date="2026-08-10",
        ),
        knowledge_atoms=[
            KnowledgeAtom(
                chunk_id="P1-C001",
                title="【账号管理】- 登录规则",
                content="用户连续3次输入错误密码，账号将被锁定30分钟。锁定期间无法登录。",
                doc_type=DocType.COMPLIANCE,
                metadata=Metadata(
                    target_audience="普通用户",
                    business_module="账号管理",
                    knowledge_type="制度规则",
                    version_timeliness="2026-01-01生效",
                    summary="登录失败锁定规则",
                ),
                qa_pairs=[
                    QaPair(
                        question="密码输错几次会锁？",
                        answer="3次",
                        keywords=["密码", "锁定"],
                        confidence_score=90,
                        remark="",
                    ),
                    QaPair(
                        question="锁多久？",
                        answer="30分钟",
                        keywords=["锁定", "时长"],
                        confidence_score=85,
                        remark="",
                    ),
                ],
                remark="",
            ),
            KnowledgeAtom(
                chunk_id="P1-C002",
                title="【密码管理】- 复杂度要求",
                content="密码必须包含大小写字母、数字和特殊字符，长度不少于8位。",
                doc_type=DocType.COMPLIANCE,
                metadata=Metadata(
                    target_audience="所有用户",
                    business_module="密码管理",
                    knowledge_type="制度规则",
                    version_timeliness="2026-01-01生效",
                    summary="密码复杂度要求",
                ),
                qa_pairs=[
                    QaPair(
                        question="密码最少几位？",
                        answer="8位",
                        keywords=["密码", "长度"],
                        confidence_score=95,
                        remark="",
                    ),
                ],
                remark="",
            ),
        ],
        exception_list=ExceptionList(
            content_conflicts=["P1-C001: 锁定时长在正文中写的是30分钟，但QA中锁多久回答为30分钟，前后一致"],
        ),
        quality_summary=QualitySummary(
            avg_confidence=90.0,
            low_confidence_count=1,
            exception_count=1,
            needs_review=True,
        ),
    )


def _create_test_client(temp_dir: str) -> TestClient:
    """创建测试客户端，使用临时目录。"""
    settings = Settings(
        _env_file=None,
        upload_dir=str(Path(temp_dir) / "uploads"),
        output_dir=str(Path(temp_dir) / "outputs"),
        deepseek_api_key="test-key",
        app_env="test",
    )

    def override_settings():
        return settings

    app.dependency_overrides[get_settings] = override_settings
    return TestClient(app)


class TestE2E(unittest.TestCase):
    """端到端集成测试：上传 → 处理 → 状态 → 结果 。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.client = _create_test_client(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()
        app.dependency_overrides.clear()

    @patch("app.core.parser.ParserFactory.get_parser")
    @patch("app.core.pipeline.Pipeline.run")
    def test_full_flow_sync(self, mock_pipeline_run, mock_get_parser):
        """完整流程：上传 → 同步处理 → 查状态 → 看结果。"""
        # ---- Step 1: Upload ----
        resp = self.client.post(
            "/api/upload",
            files={"file": ("policy.pdf", b"%PDF-1.4 mock content", "application/pdf")},
        )
        self.assertEqual(resp.status_code, 200)
        upload_data = resp.json()
        file_id = upload_data["file_id"]
        self.assertIsNotNone(file_id)
        self.assertEqual(upload_data["filename"], "policy.pdf")

        # ---- Step 2: Process (sync) ----
        # Mock MinerU parser
        mock_parser = MagicMock()
        mock_parser.parse.return_value = MagicMock(markdown="# 模拟文档\n正文内容")
        mock_get_parser.return_value = mock_parser
        # Mock Pipeline output
        mock_pipeline_run.return_value = _make_mock_pipeline_output()

        resp = self.client.post("/api/process", params={"file_id": file_id})
        self.assertEqual(resp.status_code, 200)
        process_data = resp.json()
        self.assertEqual(process_data["status"], "completed")
        self.assertIn("result", process_data)
        result = process_data["result"]
        self.assertEqual(result["document_info"]["source"], "e2e_test.pdf")
        self.assertEqual(result["document_info"]["total_chunks"], 2)
        self.assertEqual(result["document_info"]["total_qa_pairs"], 3)

        # ---- Step 3: Check status ----
        task_id = process_data["task_id"]
        resp = self.client.get(f"/api/status/{task_id}")
        self.assertEqual(resp.status_code, 200)
        status_data = resp.json()
        self.assertEqual(status_data["status"], "completed")
        self.assertEqual(status_data["progress"], 1.0)

        # ---- Step 4: Get result ----
        resp = self.client.get(f"/api/result/{task_id}")
        self.assertEqual(resp.status_code, 200)
        result_data = resp.json()
        self.assertEqual(result_data["document_info"]["source"], "e2e_test.pdf")
        self.assertEqual(len(result_data["knowledge_atoms"]), 2)
        self.assertEqual(len(result_data["knowledge_atoms"][0]["qa_pairs"]), 2)
        self.assertEqual(len(result_data["knowledge_atoms"][1]["qa_pairs"]), 1)
        self.assertEqual(len(result_data["exception_list"]["content_conflicts"]), 1)
        self.assertTrue(result_data["quality_summary"]["needs_review"])

    @patch("app.core.parser.ParserFactory.get_parser")
    @patch("app.core.pipeline.Pipeline.run")
    def test_full_flow_async_with_websocket(self, mock_pipeline_run, mock_get_parser):
        """完整流程：上传 → 异步处理 → WebSocket 连通。"""
        # ---- Step 1: Upload ----
        resp = self.client.post(
            "/api/upload",
            files={"file": ("policy.pdf", b"%PDF-1.4 mock content", "application/pdf")},
        )
        self.assertEqual(resp.status_code, 200)
        file_id = resp.json()["file_id"]

        # Mock MinerU parser
        mock_parser = MagicMock()
        mock_parser.parse.return_value = MagicMock(markdown="# 模拟文档\n正文内容")
        mock_get_parser.return_value = mock_parser

        # ---- Step 2: Trigger async process ----
        resp = self.client.post(
            "/api/process",
            params={"file_id": file_id, "async_mode": True},
        )
        self.assertEqual(resp.status_code, 200)
        process_data = resp.json()
        self.assertEqual(process_data["status"], "processing")
        self.assertEqual(process_data["task_id"], file_id)

        # ---- Step 3: Verify WebSocket endpoint is accessible ----
        with self.client.websocket_connect(f"/api/ws/{file_id}") as ws:
            ws.send_text("ping")
            pong = ws.receive_text()
            self.assertEqual(json.loads(pong)["type"], "pong")

    def test_upload_invalid_type_returns_400(self):
        """上传不支持的文件类型返回 400。"""
        resp = self.client.post(
            "/api/upload",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )
        self.assertEqual(resp.status_code, 400)

    def test_process_nonexistent_file_returns_404(self):
        """处理不存在的文件返回 404。"""
        resp = self.client.post("/api/process", params={"file_id": "nonexistent"})
        self.assertEqual(resp.status_code, 404)

    def test_health_endpoint(self):
        """健康检查端点应返回 ok。"""
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("env", data)
        self.assertIn("deepseek_model", data)


if __name__ == "__main__":
    unittest.main()