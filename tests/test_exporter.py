"""导出器模块测试。

覆盖：
- 6 种导出格式基本输出（表头、行数、字段正确性）
- 扣子 QA 格式：问题带 chunk 标题前缀、答案脱敏保留
- Dify JSONL：metadata 含 chunk_id + 5 维打标字段
- get_exporter 错误格式名抛 ValueError
- export_all 返回全部 6 种格式
- SDK export / export_to_files 集成
- API /api/export 端点（格式校验 + 文件下载）

运行：python -m pytest tests/test_exporter.py -v
"""
from __future__ import annotations

import csv
import io
import json
import unittest

from kbrefiner.core.exporter import (
    SUPPORTED_FORMATS,
    export_all,
    get_exporter,
)
from kbrefiner.models import (
    DocType,
    DocumentInfo,
    ExceptionList,
    KbDocument,
    KnowledgeAtom,
    Metadata,
    QaPair,
    QualitySummary,
)


def _make_doc() -> KbDocument:
    """构造最小可用的 KbDocument 测试数据。"""
    atom = KnowledgeAtom(
        chunk_id="P1-C001",
        title="【账号管理】- 登录失败处理",
        content="连续3次密码错误将锁定账户30分钟。",
        doc_type=DocType.OPS,
        metadata=Metadata(
            target_audience="普通用户",
            business_module="账号管理",
            knowledge_type="操作指南",
            version_timeliness="永久有效",
            summary="登录失败锁定30分钟",
        ),
        qa_pairs=[
            QaPair(
                question="密码输错了被锁了怎么办？",
                answer="连续3次密码错误将锁定账户30分钟，请稍后再试。",
                keywords=["密码", "锁定", "30分钟"],
                confidence_score=90,
            ),
            QaPair(
                question="账户能锁多久？",
                answer="30分钟。",
                keywords=["锁定时长"],
                confidence_score=85,
            ),
        ],
    )
    return KbDocument(
        document_info=DocumentInfo(
            source="test.md",
            doc_type=DocType.OPS,
            total_chunks=1,
            total_qa_pairs=2,
        ),
        knowledge_atoms=[atom],
        exception_list=ExceptionList(),
        quality_summary=QualitySummary(
            avg_confidence=87.5,
            low_confidence_count=0,
            exception_count=0,
            needs_review=False,
        ),
    )


def _parse_csv(content: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(content))
    return list(reader)


class TestCozeExporters(unittest.TestCase):
    """扣子导出器。"""

    def setUp(self):
        self.doc = _make_doc()

    def test_coze_qa_header_and_rows(self):
        rows = _parse_csv(get_exporter("coze_qa").export(self.doc))
        self.assertEqual(rows[0], ["问题", "答案"])
        # 2 条 QA = 2 行数据
        self.assertEqual(len(rows), 3)
        # 问题不含章节前缀（T-10 修复：前缀会污染向量检索）
        self.assertNotIn("【账号管理】", rows[1][0])
        self.assertIn("密码输错了被锁了怎么办", rows[1][0])
        self.assertIn("锁定账户30分钟", rows[1][1])

    def test_coze_qa_sensitive_mask_preserved(self):
        """敏感脱敏标记【敏感数据】在答案中保留。"""
        doc = _make_doc()
        doc.knowledge_atoms[0].qa_pairs[0].answer = "请联系管理员，密码为【敏感数据】。"
        rows = _parse_csv(get_exporter("coze_qa").export(doc))
        self.assertIn("【敏感数据】", rows[1][1])

    def test_coze_text_header_and_rows(self):
        rows = _parse_csv(get_exporter("coze_text").export(self.doc))
        self.assertEqual(rows[0], ["标题", "正文"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], "【账号管理】- 登录失败处理")
        self.assertIn("锁定账户30分钟", rows[1][1])


class TestDifyExporters(unittest.TestCase):
    """Dify 导出器。"""

    def setUp(self):
        self.doc = _make_doc()

    def test_dify_qa_header_and_rows(self):
        rows = _parse_csv(get_exporter("dify_qa").export(self.doc))
        self.assertEqual(rows[0], ["question", "answer"])
        self.assertEqual(len(rows), 3)
        # 问题不带 chunk 标题前缀（Dify Q&A 模式纯问答）
        self.assertEqual(rows[1][0], "密码输错了被锁了怎么办？")

    def test_dify_text_header_and_rows(self):
        rows = _parse_csv(get_exporter("dify_text").export(self.doc))
        self.assertEqual(rows[0], ["content", "segment"])
        self.assertEqual(len(rows), 2)

    def test_dify_jsonl_structure(self):
        lines = get_exporter("dify_jsonl").export(self.doc).strip().splitlines()
        self.assertEqual(len(lines), 1)
        obj = json.loads(lines[0])
        # name + text + metadata 三字段
        self.assertEqual(obj["name"], "【账号管理】- 登录失败处理")
        self.assertIn("锁定账户30分钟", obj["text"])
        meta = obj["metadata"]
        # metadata 含 chunk_id + doc_type + 5 维打标 + qa_count
        self.assertEqual(meta["chunk_id"], "P1-C001")
        self.assertEqual(meta["doc_type"], "技术运维")
        self.assertEqual(meta["knowledge_type"], "操作指南")
        self.assertEqual(meta["qa_count"], 2)

    def test_dify_jsonl_chinese_not_escaped(self):
        """ensure_ascii=False：中文直接输出。"""
        content = get_exporter("dify_jsonl").export(self.doc)
        self.assertIn("【账号管理】", content)


class TestJsonExporter(unittest.TestCase):
    """内部 JSON 导出器（兼容性）。"""

    def test_json_output_is_valid_kbdocument(self):
        content = get_exporter("json").export(_make_doc())
        doc = KbDocument.model_validate_json(content)
        self.assertEqual(doc.document_info.total_chunks, 1)


class TestExporterRegistry(unittest.TestCase):
    """注册表与入口。"""

    def test_get_exporter_unknown_format_raises(self):
        with self.assertRaises(ValueError):
            get_exporter("not_a_format")

    def test_supported_formats_complete(self):
        self.assertEqual(
            set(SUPPORTED_FORMATS),
            {"coze_qa", "coze_text", "dify_qa", "dify_text", "dify_jsonl", "json"},
        )

    def test_export_all_returns_all_formats(self):
        results = export_all(_make_doc())
        self.assertEqual(set(results.keys()), set(SUPPORTED_FORMATS))
        # 每种格式输出非空
        for fmt, content in results.items():
            self.assertTrue(content.strip(), f"格式 {fmt} 输出为空")


class TestSdkExport(unittest.TestCase):
    """SDK export 集成。"""

    def test_export_via_sdk(self):
        from kbrefiner.sdk import KBRefiner

        kb = KBRefiner(api_key="sk-test", output_dir="/tmp/kbrefiner-test")
        doc = _make_doc()
        content = kb.export(doc, "coze_qa")
        rows = _parse_csv(content)
        self.assertEqual(rows[0], ["问题", "答案"])

    def test_export_to_files(self):
        import tempfile
        from pathlib import Path

        from kbrefiner.sdk import KBRefiner

        kb = KBRefiner(api_key="sk-test", output_dir="/tmp/kbrefiner-test")
        with tempfile.TemporaryDirectory() as tmp:
            paths = kb.export_to_files(_make_doc(), tmp, formats=["coze_qa", "dify_jsonl"])
            self.assertEqual(len(paths), 2)
            self.assertTrue(Path(paths[0]).exists())
            self.assertTrue(paths[0].name.endswith(".csv"))
            self.assertTrue(paths[1].name.endswith(".jsonl"))


class TestApiExportEndpoint(unittest.TestCase):
    """API /api/export 端点。"""

    def _make_app(self, tmpdir: str):
        import os

        os.environ["OUTPUT_DIR"] = tmpdir
        from fastapi.testclient import TestClient
        from kbrefiner.api.routes import router
        from kbrefiner.main import app

        client = TestClient(app)
        return client

    def test_export_invalid_format_400(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = self._make_app(tmp)
            resp = client.get("/api/export/faketask?format=bad_format")
            self.assertEqual(resp.status_code, 400)

    def test_export_not_found_404(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = self._make_app(tmp)
            resp = client.get("/api/export/faketask?format=coze_qa")
            self.assertEqual(resp.status_code, 404)

    def test_export_csv_download(self):
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient
        from kbrefiner.config import Settings
        from kbrefiner.config import get_settings as get_settings_dep
        from kbrefiner.main import app

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "task1"
            out.mkdir()
            (out / "final.json").write_text(
                _make_doc().model_dump_json(indent=2), encoding="utf-8"
            )

            # 覆盖依赖注入的 settings，避免 lru_cache 环境变量问题
            test_settings = Settings(output_dir=tmp)
            app.dependency_overrides[get_settings_dep] = lambda: test_settings
            try:
                client = TestClient(app)
                resp = client.get("/api/export/task1?format=coze_qa")
                self.assertEqual(resp.status_code, 200)
                self.assertIn("attachment", resp.headers["content-disposition"])
                self.assertIn("问题", resp.text)
            finally:
                app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
