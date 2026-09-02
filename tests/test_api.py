"""API 路由层单元测试。

mock 掉 MinerU 解析和 Pipeline 调用，只验证路由逻辑和请求处理。

覆盖：
- POST /api/upload 上传文件（成功/不支持类型/文件过大）
- POST /api/process 处理文档（成功/文件不存在）
- GET /api/status/{task_id} 查询状态（存在/不存在）
- GET /api/result/{task_id} 获取结果（存在/不存在）

运行：python -m pytest tests/test_api.py -v
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from kbrefiner.main import app
from kbrefiner.api import deps, routes
from kbrefiner.api.ws_manager import manager
from kbrefiner.config import Settings, get_settings
from kbrefiner.core.llm import DeepSeekClient, LLMConfig
from kbrefiner.core.pipeline import Pipeline, PipelineConfig
from kbrefiner.db import AuditStore, PasswordResetStore, SettingsStore, TaskStore, UserStore
from kbrefiner.models import KbDocument, DocType, Stage1Output, Stage2Output, Stage3Output, Stage4Output
from kbrefiner import auth as _auth


def _make_mock_pipeline_output() -> KbDocument:
    """构造一个模拟的 Pipeline 输出。"""
    from kbrefiner.models import (
        DocumentInfo,
        ExceptionList,
        KnowledgeAtom,
        Metadata,
        QaPair,
        QualitySummary,
    )

    return KbDocument(
        document_info=DocumentInfo(
            source="test.pdf",
            doc_type=DocType.COMPLIANCE,
            total_chunks=2,
            total_qa_pairs=3,
            processing_date="2026-08-10",
        ),
        knowledge_atoms=[
            KnowledgeAtom(
                chunk_id="P1-C001",
                title="【账号管理】- 登录规则",
                content="连续3次失败锁定30分钟。",
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
                ],
                remark="",
            ),
        ],
        exception_list=ExceptionList(),
        quality_summary=QualitySummary(
            avg_confidence=90.0,
            low_confidence_count=0,
            exception_count=0,
            needs_review=False,
        ),
    )


# 真实任务库（测试结束后恢复）
_original_task_store = routes._task_store


def _create_test_client(temp_dir: str) -> TestClient:
    """创建测试客户端，使用临时目录作为 storage。"""
    settings = Settings(
        _env_file=None,
        upload_dir=str(Path(temp_dir) / "uploads"),
        output_dir=str(Path(temp_dir) / "outputs"),
        llm_api_key="test-key",
    )

    def override_settings():
        return settings

    # 隔离任务库：routes 模块级 _task_store 指向真实 ./data/tasks.db，
    # 不替换的话测试任务会写入真实任务列表
    routes._task_store = TaskStore(str(Path(temp_dir) / "tasks.db"))

    app.dependency_overrides[get_settings] = override_settings
    return TestClient(app)


def _restore_task_store() -> None:
    """恢复真实任务库并释放临时库连接（配合 _create_test_client 的替换）。"""
    routes._task_store.close()
    routes._task_store = _original_task_store


class TestUpload(unittest.TestCase):
    """上传文件测试。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.client = _create_test_client(self.temp_dir.name)

    def tearDown(self):
        _restore_task_store()
        self.temp_dir.cleanup()
        app.dependency_overrides.clear()

    def test_upload_pdf_success(self):
        """上传 PDF 文件成功。"""
        response = self.client.post(
            "/api/upload",
            files={"file": ("test.pdf", b"%PDF-1.4 test content", "application/pdf")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("file_id", data)
        self.assertEqual(data["filename"], "test.pdf")
        self.assertGreater(data["size"], 0)
        # 文件应存在磁盘
        upload_dir = Path(self.temp_dir.name) / "uploads"
        self.assertTrue(list(upload_dir.glob(f"{data['file_id']}.*")))

    def test_upload_invalid_extension(self):
        """不支持的文件类型应返回 400。"""
        response = self.client.post(
            "/api/upload",
            files={"file": ("test.exe", b"hello", "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不支持的文件类型", response.json()["detail"])

    def test_upload_large_file(self):
        """超过大小限制应返回 400。"""
        large_content = b"x" * (51 * 1024 * 1024)  # 51MB
        response = self.client.post(
            "/api/upload",
            files={"file": ("large.pdf", large_content, "application/pdf")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("文件大小超过限制", response.json()["detail"])


class TestProcess(unittest.TestCase):
    """处理文档测试。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.client = _create_test_client(self.temp_dir.name)

        # 准备上传文件
        upload_dir = Path(self.temp_dir.name) / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "abc123.pdf").write_bytes(b"%PDF-1.4 test content")

    def tearDown(self):
        _restore_task_store()
        self.temp_dir.cleanup()
        app.dependency_overrides.clear()

    @patch("kbrefiner.core.parser.ParserFactory.get_parser")
    @patch("kbrefiner.core.pipeline.Pipeline.run")
    def test_process_success(self, mock_pipeline_run, mock_get_parser):
        """处理文档成功。"""
        # 模拟 MinerU 解析
        mock_parser = MagicMock()
        mock_parser.parse.return_value = MagicMock(markdown="# 测试文档\n正文")
        mock_get_parser.return_value = mock_parser

        # 模拟 Pipeline 输出
        mock_pipeline_run.return_value = _make_mock_pipeline_output()

        response = self.client.post(
            "/api/process",
            params={"file_id": "abc123"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "completed")
        self.assertIn("result", data)
        self.assertEqual(data["result"]["document_info"]["source"], "test.pdf")

    def test_process_file_not_found(self):
        """文件不存在应返回 404。"""
        response = self.client.post(
            "/api/process",
            params={"file_id": "nonexistent"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("不存在", response.json()["detail"])


class TestStatus(unittest.TestCase):
    """状态查询测试。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.client = _create_test_client(self.temp_dir.name)

    def tearDown(self):
        _restore_task_store()
        self.temp_dir.cleanup()
        app.dependency_overrides.clear()

    def test_status_not_found(self):
        response = self.client.get("/api/status/nonexistent")
        self.assertEqual(response.status_code, 404)

    def test_status_pending(self):
        """任务存在但尚未开始处理。"""
        output_dir = Path(self.temp_dir.name) / "outputs" / "test123"
        output_dir.mkdir(parents=True, exist_ok=True)
        # 不创建任何 stage 文件 → pending

        response = self.client.get("/api/status/test123")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["progress"], 0.0)

    def test_status_processing(self):
        output_dir = Path(self.temp_dir.name) / "outputs" / "test456"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "stage1.json").write_text("{}", encoding="utf-8")
        (output_dir / "stage2.json").write_text("{}", encoding="utf-8")

        response = self.client.get("/api/status/test456")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "processing")
        self.assertAlmostEqual(data["progress"], 0.4, places=1)

    def test_status_completed(self):
        output_dir = Path(self.temp_dir.name) / "outputs" / "test789"
        output_dir.mkdir(parents=True, exist_ok=True)
        for s in ["stage1", "stage2", "stage3", "stage4", "final"]:
            (output_dir / f"{s}.json").write_text("{}", encoding="utf-8")

        response = self.client.get("/api/status/test789")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["progress"], 1.0)


class TestResult(unittest.TestCase):
    """结果查询测试。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.client = _create_test_client(self.temp_dir.name)

    def tearDown(self):
        _restore_task_store()
        self.temp_dir.cleanup()
        app.dependency_overrides.clear()

    def test_result_not_found(self):
        response = self.client.get("/api/result/nonexistent")
        self.assertEqual(response.status_code, 404)

    def test_result_success(self):
        output_dir = Path(self.temp_dir.name) / "outputs" / "test_result"
        output_dir.mkdir(parents=True, exist_ok=True)
        result = {"document_info": {"source": "test.pdf", "total_chunks": 1}}
        (output_dir / "final.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        response = self.client.get("/api/result/test_result")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["document_info"]["source"], "test.pdf")


class TestWebSocket(unittest.TestCase):
    """WebSocket 实时进度推送测试。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.client = _create_test_client(self.temp_dir.name)

    def tearDown(self):
        _restore_task_store()
        self.temp_dir.cleanup()
        app.dependency_overrides.clear()

    def test_websocket_connect_and_receive_pong(self):
        """连接 WebSocket 并发送 ping 应收到 pong。"""
        with self.client.websocket_connect("/api/ws/test_task") as ws:
            ws.send_text("ping")
            response = ws.receive_text()
            data = json.loads(response)
            self.assertEqual(data["type"], "pong")

    def test_websocket_receive_progress(self):
        """通过 push_progress 推送进度应被 WS 客户端收到。"""
        import asyncio

        from kbrefiner.api.ws_manager import push_progress

        with self.client.websocket_connect("/api/ws/progress_test") as ws:
            # 通过 manager 推送进度
            asyncio.run(push_progress("progress_test", stage="Stage1-Clean", progress=0.2, status="processing"))
            response = ws.receive_text()
            data = json.loads(response)
            self.assertEqual(data["type"], "progress")
            self.assertEqual(data["stage"], "Stage1-Clean")
            self.assertEqual(data["progress"], 0.2)
            self.assertEqual(data["status"], "processing")

    def test_websocket_receive_result(self):
        """通过 push_result 推送结果应被 WS 客户端收到。"""
        import asyncio

        from kbrefiner.api.ws_manager import push_result

        with self.client.websocket_connect("/api/ws/result_test") as ws:
            asyncio.run(push_result("result_test", {"document_info": {"source": "test.pdf"}}))
            response = ws.receive_text()
            data = json.loads(response)
            self.assertEqual(data["type"], "result")
            self.assertEqual(data["result"]["document_info"]["source"], "test.pdf")

    def test_websocket_receive_error(self):
        """通过 push_error 推送错误应被 WS 客户端收到。"""
        import asyncio

        from kbrefiner.api.ws_manager import push_error

        with self.client.websocket_connect("/api/ws/error_test") as ws:
            asyncio.run(push_error("error_test", "something went wrong"))
            response = ws.receive_text()
            data = json.loads(response)
            self.assertEqual(data["type"], "error")
            self.assertIn("something went wrong", data["error"])

    def test_websocket_disconnect_cleanup(self):
        """断开连接后，manager 中应移除该连接。"""
        task_id = "cleanup_test"
        with self.client.websocket_connect(f"/api/ws/{task_id}") as ws:
            self.assertTrue(manager.is_connected(task_id))
        # 退出 with 块后连接已断开
        self.assertFalse(manager.is_connected(task_id))


class TestCancel(unittest.TestCase):
    """任务取消测试。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.client = _create_test_client(self.temp_dir.name)

    def tearDown(self):
        _restore_task_store()
        self.temp_dir.cleanup()
        app.dependency_overrides.clear()

    def test_cancel_persists_status(self):
        """取消后状态应持久化为 cancelled（此前只改内存副本导致状态丢失）。"""
        routes._task_store["task_cancel_1"] = {
            "status": "processing",
            "filename": "doc.pdf",
            "file_size": 1024,
        }

        resp = self.client.post("/api/task/task_cancel_1/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "cancelled")

        # /status 应读到持久化后的 cancelled
        status_resp = self.client.get("/api/status/task_cancel_1")
        self.assertEqual(status_resp.status_code, 200)
        data = status_resp.json()
        self.assertEqual(data["status"], "cancelled")
        self.assertIn("取消", data["error"])

    def test_cancel_not_found(self):
        """取消不存在的任务应返回 404。"""
        resp = self.client.post("/api/task/nonexistent/cancel")
        self.assertEqual(resp.status_code, 404)

    def test_cancel_completed_rejected(self):
        """已完成的任务不可取消。"""
        routes._task_store["task_done"] = {
            "status": "completed",
            "filename": "doc.pdf",
            "file_size": 1024,
        }
        resp = self.client.post("/api/task/task_done/cancel")
        self.assertEqual(resp.status_code, 400)


class TestListTasks(unittest.TestCase):
    """任务列表测试。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.client = _create_test_client(self.temp_dir.name)
        self.output_base = Path(self.temp_dir.name) / "outputs"

    def tearDown(self):
        _restore_task_store()
        self.temp_dir.cleanup()
        app.dependency_overrides.clear()

    def test_list_progress_from_stage_files(self):
        """processing 任务的进度应按落盘阶段文件估算（此前硬编码 0.5）。"""
        routes._task_store["task_p"] = {
            "status": "processing",
            "filename": "a.pdf",
            "file_size": 10,
        }
        task_dir = self.output_base / "task_p"
        task_dir.mkdir(parents=True)
        (task_dir / "stage1.json").write_text("{}", encoding="utf-8")
        (task_dir / "stage2.json").write_text("{}", encoding="utf-8")

        resp = self.client.get("/api/tasks")
        self.assertEqual(resp.status_code, 200)
        tasks = {t["task_id"]: t for t in resp.json()["tasks"]}
        self.assertAlmostEqual(tasks["task_p"]["progress"], 0.4, places=2)

    def test_list_completed_progress_one(self):
        """completed 任务进度为 1.0。"""
        routes._task_store["task_c"] = {
            "status": "completed",
            "filename": "b.pdf",
            "file_size": 10,
        }

        resp = self.client.get("/api/tasks")
        tasks = {t["task_id"]: t for t in resp.json()["tasks"]}
        self.assertEqual(tasks["task_c"]["progress"], 1.0)

    def test_list_includes_history_task_from_output_dir(self):
        """输出目录中存在、任务库中不存在的历史任务也应出现在列表。"""
        task_dir = self.output_base / "task_hist"
        task_dir.mkdir(parents=True)
        final = {"document_info": {"source": "history.pdf"}}
        (task_dir / "final.json").write_text(
            json.dumps(final, ensure_ascii=False), encoding="utf-8"
        )

        resp = self.client.get("/api/tasks")
        self.assertEqual(resp.status_code, 200)
        tasks = {t["task_id"]: t for t in resp.json()["tasks"]}
        self.assertIn("task_hist", tasks)
        self.assertEqual(tasks["task_hist"]["filename"], "history.pdf")
        self.assertEqual(tasks["task_hist"]["status"], "completed")


class TestRecoverInterrupted(unittest.TestCase):
    """服务重启后中断任务恢复测试。"""

    def test_processing_marked_failed(self):
        """processing 任务在启动恢复时应被标记为 failed（避免僵尸状态）。"""
        with TemporaryDirectory() as tmpdir:
            store = TaskStore(str(Path(tmpdir) / "tasks.db"))
            store["t1"] = {"status": "processing", "filename": "a.pdf"}
            store["t2"] = {"status": "completed", "filename": "b.pdf"}
            store["t3"] = {"status": "pending", "filename": "c.pdf"}

            routes._task_store = store
            try:
                routes.recover_interrupted_tasks()

                self.assertEqual(store.get("t1")["status"], "failed")
                self.assertIn("中断", store.get("t1")["error"])
                # 其他状态不受影响
                self.assertEqual(store.get("t2")["status"], "completed")
                self.assertEqual(store.get("t3")["status"], "pending")
            finally:
                routes._task_store = _original_task_store
                store.close()


class TestExportFilename(unittest.TestCase):
    """导出文件名跟随用户上传原文件名测试。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.client = _create_test_client(self.temp_dir.name)

    def tearDown(self):
        _restore_task_store()
        self.temp_dir.cleanup()
        app.dependency_overrides.clear()

    def _write_final(self, task_id: str):
        task_dir = Path(self.temp_dir.name) / "outputs" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "final.json").write_text(
            _make_mock_pipeline_output().model_dump_json(indent=2), encoding="utf-8"
        )

    def test_export_uses_original_filename(self):
        """下载名 = 原文件名去扩展名 + 格式后缀（RFC 5987 编码中文）。"""
        from urllib.parse import quote

        self._write_final("task_exp1")
        routes._task_store["task_exp1"] = {
            "status": "completed",
            "filename": "产品手册.pdf",
        }

        resp = self.client.get("/api/export/task_exp1?format=coze_qa")
        self.assertEqual(resp.status_code, 200)
        disposition = resp.headers["content-disposition"]
        # UTF-8 主值：原文件名 + 格式后缀（产品手册_coze_qa.csv）
        self.assertIn(f"filename*=UTF-8''{quote('产品手册_coze_qa.csv')}", disposition)
        # ASCII 兜底：任务 ID 命名（旧客户端可读）
        self.assertIn('filename="kbrefiner_task_exp1_coze_qa.csv"', disposition)

    def test_export_fallback_when_no_record(self):
        """任务库无记录（历史任务）时回退任务 ID 命名。"""
        self._write_final("task_exp2")

        resp = self.client.get("/api/export/task_exp2?format=json")
        self.assertEqual(resp.status_code, 200)
        disposition = resp.headers["content-disposition"]
        self.assertIn("filename*=UTF-8''kbrefiner_task_exp2_json.json", disposition)
        self.assertIn('filename="kbrefiner_task_exp2_json.json"', disposition)


class TestReportPage(unittest.TestCase):
    """质检报告独立页 /report 测试。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.client = _create_test_client(self.temp_dir.name)

    def tearDown(self):
        _restore_task_store()
        self.temp_dir.cleanup()
        app.dependency_overrides.clear()

    def _write_final(self, task_id: str):
        task_dir = Path(self.temp_dir.name) / "outputs" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "final.json").write_text(
            _make_mock_pipeline_output().model_dump_json(indent=2), encoding="utf-8"
        )

    def test_report_page_renders(self):
        """/report 路由返回报告页 HTML。"""
        resp = self.client.get("/report")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("质检报告", resp.text)
        self.assertIn("renderPipelineResult", resp.text)

    def test_report_data_via_result_api(self):
        """报告页依赖 /api/result/{task_id}，结果须可正常返回。"""
        self._write_final("task_rep1")
        routes._task_store["task_rep1"] = {
            "status": "completed",
            "filename": "手册.pdf",
        }

        resp = self.client.get("/api/result/task_rep1")
        self.assertEqual(resp.status_code, 200)
        result = resp.json()
        self.assertEqual(result["document_info"]["total_chunks"], 2)
        self.assertEqual(result["document_info"]["total_qa_pairs"], 3)
        self.assertEqual(len(result["knowledge_atoms"]), 1)

    def test_report_metadata_from_status_and_tasks(self):
        """报告页侧栏元数据来源：/api/status 与 /api/tasks 均可读。"""
        self._write_final("task_rep2")
        routes._task_store["task_rep2"] = {
            "status": "completed",
            "filename": "手册.pdf",
            "token_consumed": 1234,
            "created_at": 1700000000,
            "updated_at": 1700000100,
        }

        status = self.client.get("/api/status/task_rep2").json()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["filename"], "手册.pdf")

        tasks = self.client.get("/api/tasks").json()
        t = next(x for x in tasks["tasks"] if x["task_id"] == "task_rep2")
        self.assertEqual(t["token_consumed"], 1234)


# =====================================================================
# FR-5: POST /api/tasks/batch 批量操作（cancel/delete/retry，仅管理员）
# =====================================================================

class TestFR5BatchTasks(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        cls._orig_secret = os.environ.get("AUTH_SECRET")
        os.environ["AUTH_SECRET"] = "batch-test-secret"
        _auth._reset_secret_cache()
        cls._orig_deps_user = deps._user_store
        cls._orig_deps_task = deps._task_store
        cls._orig_deps_settings = deps._settings_store
        cls._orig_deps_audit = deps._audit_store
        cls._orig_deps_reset = deps._password_reset_store
        cls._orig_routes_task = routes._task_store

    @classmethod
    def tearDownClass(cls):
        import os
        if cls._orig_secret is None:
            os.environ.pop("AUTH_SECRET", None)
        else:
            os.environ["AUTH_SECRET"] = cls._orig_secret
        _auth._reset_secret_cache()
        deps._user_store = cls._orig_deps_user
        deps._task_store = cls._orig_deps_task
        deps._settings_store = cls._orig_deps_settings
        deps._audit_store = cls._orig_deps_audit
        deps._password_reset_store = cls._orig_deps_reset
        routes._task_store = cls._orig_routes_task

    def setUp(self):
        self.temp = TemporaryDirectory()
        db = str(Path(self.temp.name) / "t.db")
        self.task_store = TaskStore(db)
        self.user_store = UserStore(db)
        self.settings_store = SettingsStore(db)
        self.audit_store = AuditStore(db)
        self.pw_reset_store = PasswordResetStore(db)
        # 全部注入
        deps._task_store = self.task_store
        routes._task_store = self.task_store
        deps._user_store = self.user_store
        deps._settings_store = self.settings_store
        deps._audit_store = self.audit_store
        deps._password_reset_store = self.pw_reset_store

        # 创建超级管理员 + 普通用户
        self.super_admin = self.user_store.create(
            "s@x.com", "s", "pass1234", role="super_admin")
        self.user = self.user_store.create(
            "u@x.com", "u", "pass1234", role="user")

        s = Settings(
            _env_file=None,
            upload_dir=str(Path(self.temp.name) / "uploads"),
            output_dir=str(Path(self.temp.name) / "outputs"),
            llm_api_key="test-key",
        )
        app.dependency_overrides[get_settings] = lambda: s
        self.client = TestClient(app)
        # 登录超级管理员
        r = self.client.post("/api/auth/login", json={"email": "s@x.com", "password": "pass1234"})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        app.dependency_overrides.clear()
        for store in (self.task_store, self.user_store, self.settings_store,
                      self.audit_store, self.pw_reset_store):
            store.close()
        self.temp.cleanup()

    # ===== 辅助 =====

    def _post(self, action, ids):
        return self.client.post("/api/tasks/batch", json={"action": action, "ids": ids})

    def test_batch_requires_admin(self):
        # 未登录客户端
        nc = TestClient(app)
        resp = nc.post("/api/tasks/batch", json={"action": "delete", "ids": ["x"]})
        self.assertEqual(resp.status_code, 401)

    def test_batch_invalid_action_400(self):
        resp = self._post("rename", ["t1"])
        self.assertEqual(resp.status_code, 400)

    def test_batch_empty_ids_422(self):
        resp = self.client.post("/api/tasks/batch", json={"action": "delete", "ids": []})
        self.assertEqual(resp.status_code, 422)

    def test_batch_delete_existing_and_missing(self):
        self.task_store["d1"] = {"status": "completed", "filename": "a.pdf", "file_size": 10}
        self.task_store["d2"] = {"status": "failed", "filename": "b.pdf", "file_size": 10}
        self.assertIsNotNone(self.task_store.get("d1"))
        resp = self._post("delete", ["d1", "d2", "nope"])
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["success"], 2)
        self.assertEqual(data["skipped"], 1)  # nope 不存在
        self.assertIsNone(self.task_store.get("d1"))
        self.assertIsNone(self.task_store.get("d2"))

    def test_batch_cancel_pending_and_processing(self):
        self.task_store["cp"] = {"status": "pending", "filename": "a.pdf", "file_size": 10}
        self.task_store["cr"] = {"status": "processing", "filename": "b.pdf", "file_size": 10}
        self.task_store["cd"] = {"status": "completed", "filename": "c.pdf", "file_size": 10}
        resp = self._post("cancel", ["cp", "cr", "cd"])
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["success"], 2)
        self.assertEqual(data["skipped"], 1)  # completed 跳过
        self.assertEqual(self.task_store.get("cp")["status"], "cancelled")
        self.assertEqual(self.task_store.get("cr")["status"], "cancelled")

    def test_batch_retry_failed_and_cancelled(self):
        self.task_store["rf"] = {"status": "failed", "filename": "a.pdf", "file_size": 10, "error": "x"}
        self.task_store["rc"] = {"status": "cancelled", "filename": "b.pdf", "file_size": 10}
        self.task_store["rp"] = {"status": "pending", "filename": "c.pdf", "file_size": 10}
        resp = self._post("retry", ["rf", "rc", "rp"])
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["success"], 2)
        self.assertEqual(data["skipped"], 1)
        self.assertEqual(self.task_store.get("rf")["status"], "pending")
        self.assertIsNone(self.task_store.get("rf").get("error"))
        self.assertEqual(self.task_store.get("rc")["status"], "pending")

    def test_batch_cancel_not_found_recorded(self):
        resp = self._post("cancel", ["missing1", "missing2"])
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["errors"]), 2)
        self.assertEqual(data["errors"][0]["error"], "not_found")

    def test_batch_over_200_400(self):
        ids = [f"t{i}" for i in range(201)]
        resp = self._post("delete", ids)
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()