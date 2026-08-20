"""文档解析层单元测试。

覆盖：
- FileType.from_path 扩展名推断
- ParsedDocument 数据结构
- MineruParser 结构化提取（标题/表格/公式/页数）—— 纯函数，不需真实 MinerU
- MineruParser 边界情况（空文档/加密/不支持格式/文件不存在）—— mock _run_mineru
- ParserFactory 单例

运行：python -m unittest tests.test_parser -v
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from kbrefiner.core.parser import (
    EmptyDocumentError,
    EncryptedDocumentError,
    FileType,
    MineruParser,
    ParsedDocument,
    ParserFactory,
    ParseFailedError,
    SimpleParser,
    UnsupportedFileError,
    parse_document,
)
from kbrefiner.core.parser.base import Formula, Heading, Table


class TestFileType(unittest.TestCase):
    """文件类型推断。"""

    def test_pdf(self):
        self.assertEqual(FileType.from_path("doc.pdf"), FileType.PDF)

    def test_docx(self):
        self.assertEqual(FileType.from_path("report.docx"), FileType.DOCX)
        # 旧格式兼容
        self.assertEqual(FileType.from_path("report.doc"), FileType.DOCX)

    def test_pptx(self):
        self.assertEqual(FileType.from_path("slides.pptx"), FileType.PPTX)

    def test_xlsx(self):
        self.assertEqual(FileType.from_path("data.xlsx"), FileType.XLSX)

    def test_images(self):
        for ext in ("png", "jpg", "jpeg", "webp"):
            with self.subTest(ext=ext):
                self.assertEqual(FileType.from_path(f"img.{ext}"), FileType.IMAGE)

    def test_unsupported(self):
        with self.assertRaises(UnsupportedFileError):
            FileType.from_path("video.mp4")

    def test_case_insensitive(self):
        self.assertEqual(FileType.from_path("DOC.PDF"), FileType.PDF)


class TestParsedDocument(unittest.TestCase):
    """解析结果数据结构。"""

    def test_basic_construction(self):
        doc = ParsedDocument(
            source="test.pdf",
            file_type=FileType.PDF,
            markdown="# 标题\n正文内容",
        )
        self.assertEqual(doc.char_count, len("# 标题\n正文内容"))
        self.assertFalse(doc.is_empty)

    def test_empty_detection(self):
        doc = ParsedDocument(
            source="empty.pdf",
            file_type=FileType.PDF,
            markdown="   \n\n  \t  \n",
        )
        self.assertTrue(doc.is_empty)

    def test_default_collections(self):
        doc = ParsedDocument(
            source="x.pdf",
            file_type=FileType.PDF,
            markdown="内容",
        )
        self.assertEqual(doc.headings, [])
        self.assertEqual(doc.tables, [])
        self.assertEqual(doc.formulas, [])
        self.assertEqual(doc.page_count, 0)


class TestMineruParserExtraction(unittest.TestCase):
    """MineruParser 结构化提取（纯函数，mock 掉 MinerU 调用）。

    这些测试验证从 MinerU 输出的 Markdown 中提取标题/表格/公式的逻辑，
    不依赖真实 MinerU 安装。
    """

    def _make_parser_with_markdown(self, markdown: str) -> MineruParser:
        """构造一个 parse() 返回指定 markdown 的 MineruParser。"""
        parser = MineruParser()
        # mock _run_mineru，跳过真实 MinerU 调用
        parser._run_mineru = lambda path, ft: markdown  # type: ignore
        return parser

    def _write_temp_file(self, suffix: str = ".pdf") -> Path:
        """写一个临时文件供 parse() 读取路径。"""
        import tempfile
        fd, path = tempfile.mkstemp(suffix=suffix)
        Path(path).write_text("dummy", encoding="utf-8")
        return Path(path)

    def test_extract_headings(self):
        markdown = "# 一级标题\n正文\n## 二级标题\n更多\n### 三级标题"
        parser = self._make_parser_with_markdown(markdown)
        path = self._write_temp_file()
        try:
            doc = parser.parse(path)
        finally:
            path.unlink()
        self.assertEqual(len(doc.headings), 3)
        self.assertEqual(doc.headings[0].level, 1)
        self.assertEqual(doc.headings[0].text, "一级标题")
        self.assertEqual(doc.headings[0].line_start, 0)
        self.assertEqual(doc.headings[1].level, 2)
        self.assertEqual(doc.headings[1].text, "二级标题")
        self.assertEqual(doc.headings[2].level, 3)

    def test_extract_table(self):
        markdown = (
            "标题前文字\n\n"
            "<table>\n<tr><td>A</td><td>B</td></tr>\n<tr><td>1</td><td>2</td></tr>\n</table>\n\n"
            "表格后文字"
        )
        parser = self._make_parser_with_markdown(markdown)
        path = self._write_temp_file()
        try:
            doc = parser.parse(path)
        finally:
            path.unlink()
        self.assertEqual(len(doc.tables), 1)
        self.assertIn("<table>", doc.tables[0].content_html)
        self.assertIn("</table>", doc.tables[0].content_html)
        # 表格在第 2 行（0-based：空行后）
        self.assertGreaterEqual(doc.tables[0].line_start, 1)

    def test_extract_multiple_tables(self):
        markdown = (
            "<table><tr><td>表1</td></tr></table>\n"
            "中间文字\n"
            "<table><tr><td>表2</td></tr></table>"
        )
        parser = self._make_parser_with_markdown(markdown)
        path = self._write_temp_file()
        try:
            doc = parser.parse(path)
        finally:
            path.unlink()
        self.assertEqual(len(doc.tables), 2)

    def test_extract_block_formula(self):
        markdown = "正文\n\n$$E = mc^2$$\n\n后续"
        parser = self._make_parser_with_markdown(markdown)
        path = self._write_temp_file()
        try:
            doc = parser.parse(path)
        finally:
            path.unlink()
        self.assertTrue(len(doc.formulas) >= 1)
        block_formulas = [f for f in doc.formulas if "E = mc^2" in f.latex]
        self.assertTrue(len(block_formulas) >= 1)

    def test_extract_inline_formula(self):
        markdown = "公式 $a^2 + b^2 = c^2$ 在文中"
        parser = self._make_parser_with_markdown(markdown)
        path = self._write_temp_file()
        try:
            doc = parser.parse(path)
        finally:
            path.unlink()
        inline_formulas = [f for f in doc.formulas if "a^2" in f.latex]
        self.assertTrue(len(inline_formulas) >= 1)

    def test_no_formula_in_plain_text(self):
        markdown = "纯文本没有公式，$符号也不算"
        parser = self._make_parser_with_markdown(markdown)
        path = self._write_temp_file()
        try:
            doc = parser.parse(path)
        finally:
            path.unlink()
        # $符号单独出现不算公式（行内公式需 $...$ 配对）
        # 注意：如果 markdown 恰好有配对，则可能被误提取，这里测试典型纯文本
        # "纯文本没有公式，$符号也不算" 只有一个 $，不配对，应无公式
        self.assertEqual(len(doc.formulas), 0)

    def test_page_count_with_markers(self):
        markdown = "第1页内容\n<!-- page -->\n第2页内容\n<!-- page -->\n第3页"
        parser = self._make_parser_with_markdown(markdown)
        path = self._write_temp_file()
        try:
            doc = parser.parse(path)
        finally:
            path.unlink()
        self.assertEqual(doc.page_count, 3)

    def test_page_count_without_markers(self):
        markdown = "无分页标记的普通文档"
        parser = self._make_parser_with_markdown(markdown)
        path = self._write_temp_file()
        try:
            doc = parser.parse(path)
        finally:
            path.unlink()
        self.assertEqual(doc.page_count, 0)

    def test_full_structure_extraction(self):
        """综合：标题 + 表格 + 公式 + 分页。"""
        markdown = (
            "# 账号管理规范\n"
            "## 登录规则\n"
            "用户连续3次登录失败将锁定。\n\n"
            "<table>\n<tr><th>失败次数</th><th>锁定时长</th></tr>\n"
            "<tr><td>3</td><td>30分钟</td></tr>\n</table>\n\n"
            "计算公式 $$T = n \\times 10$$ 分钟\n\n"
            "<!-- page -->\n"
            "## 解锁流程\n"
            "联系管理员解锁。"
        )
        parser = self._make_parser_with_markdown(markdown)
        path = self._write_temp_file()
        try:
            doc = parser.parse(path)
        finally:
            path.unlink()

        self.assertEqual(len(doc.headings), 3)
        self.assertEqual(doc.headings[0].text, "账号管理规范")
        self.assertEqual(doc.headings[1].text, "登录规则")
        self.assertEqual(doc.headings[2].text, "解锁流程")
        self.assertEqual(len(doc.tables), 1)
        self.assertTrue(len(doc.formulas) >= 1)
        self.assertEqual(doc.page_count, 2)
        self.assertFalse(doc.is_empty)


class TestMineruParserBoundary(unittest.TestCase):
    """MineruParser 边界情况。"""

    def test_empty_document_raises(self):
        parser = MineruParser()
        parser._run_mineru = lambda path, ft: "   \n\n  "  # type: ignore
        import tempfile
        path = Path(tempfile.mkstemp(suffix=".pdf")[1])
        path.write_text("dummy")
        try:
            with self.assertRaises(EmptyDocumentError):
                parser.parse(path)
        finally:
            path.unlink()

    def test_unsupported_format(self):
        parser = MineruParser()
        import tempfile
        path = Path(tempfile.mkstemp(suffix=".mp4")[1])
        path.write_text("dummy")
        try:
            with self.assertRaises(UnsupportedFileError):
                parser.parse(path)
        finally:
            path.unlink()

    def test_file_not_found(self):
        parser = MineruParser()
        with self.assertRaises(ParseFailedError):
            parser.parse("/nonexistent/file.pdf")

    def test_supported_types(self):
        parser = MineruParser()
        # MARKDOWN 由 SimpleParser 直读，不走 MinerU
        expected = set(FileType) - {FileType.MARKDOWN}
        self.assertEqual(parser.supported_types, expected)

    def test_encrypted_detection(self):
        """模拟加密 PDF：mock _run_mineru 抛出 EncryptedDocumentError。"""
        parser = MineruParser()
        parser._run_mineru = lambda path, ft: (_ for _ in ()).throw(  # type: ignore
            EncryptedDocumentError("encrypted")
        )
        import tempfile
        path = Path(tempfile.mkstemp(suffix=".pdf")[1])
        path.write_text("dummy")
        try:
            with self.assertRaises(EncryptedDocumentError):
                parser.parse(path)
        finally:
            path.unlink()

    def test_mineru_not_installed(self):
        """CLI 模式下 MinerU 未安装应提示安装方式。"""
        parser = MineruParser(use_sdk=False)
        import tempfile
        path = Path(tempfile.mkstemp(suffix=".pdf")[1])
        path.write_text("dummy")
        try:
            # mock shutil.which 返回 None
            with patch("kbrefiner.core.parser.mineru_parser.shutil.which", return_value=None):
                with self.assertRaises(ParseFailedError) as ctx:
                    parser.parse(path)
            self.assertIn("未安装", str(ctx.exception))
        finally:
            path.unlink()


class TestParserFactory(unittest.TestCase):
    """解析器工厂。"""

    def test_singleton(self):
        ParserFactory._reset()
        p1 = ParserFactory.get_parser()
        p2 = ParserFactory.get_parser()
        self.assertIs(p1, p2)

    def test_returns_mineru_parser(self):
        """auto 模式下返回可用的解析器（MinerU 或 SimpleParser）。"""
        ParserFactory._reset()
        p = ParserFactory.get_parser()
        # auto 模式：MinerU 已安装则返回 MineruParser，否则返回 SimpleParser
        self.assertIsInstance(p, (MineruParser, SimpleParser))

    def test_reset(self):
        ParserFactory._reset()
        p1 = ParserFactory.get_parser()
        ParserFactory._reset()
        p2 = ParserFactory.get_parser()
        self.assertIsNot(p1, p2)


class TestParseDocumentHelper(unittest.TestCase):
    """便捷函数 parse_document。"""

    def test_parse_document_with_mock(self):
        """mock MinerU 调用，验证 parse_document 便捷函数。"""
        markdown = "# 测试文档\n正文内容"
        import tempfile
        path = Path(tempfile.mkstemp(suffix=".pdf")[1])
        path.write_text("dummy")
        try:
            with patch.object(MineruParser, "_run_mineru", return_value=markdown):
                doc = parse_document(str(path), backend="mineru")
            self.assertEqual(doc.markdown, markdown)
            self.assertEqual(doc.file_type, FileType.PDF)
            self.assertEqual(len(doc.headings), 1)
            self.assertEqual(doc.headings[0].text, "测试文档")
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
