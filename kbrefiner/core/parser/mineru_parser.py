"""MinerU 文档解析器封装。

MinerU 是 OpenDataLab 开源的一站式文档解析工具，支持 PDF/DOCX/PPTX/XLSX/图片/网页，
自带表格提取（合并单元格拆解）、OCR、版面还原、公式转 LaTeX。

调用方式优先级：
1. CLI（默认）：`mineru -p <file> -o <output_dir>` —— 部署简单，进程隔离
2. SDK（备选）：`from mineru import cli` —— 进程内调用，省去启动开销

MinerU 输出：
- <output_dir>/<stem>/full.md：完整 Markdown（含 HTML 表格、LaTeX 公式、标题层级）
- <output_dir>/<stem>/content.json：结构化版面信息（可选用于提取更精细的元数据）

本模块从 full.md 提取：
- markdown 原文
- headings：从 `#`/`##`/`###` 标题行提取层级
- tables：从 HTML <table> 块提取位置
- formulas：从 `$...$` / `$$...$$` 提取 LaTeX

边界处理：
- 空文档 → EmptyDocumentError
- 加密 PDF → EncryptedDocumentError（检测 MinerU stderr 含 "encrypted" 关键词）
- 超大文档 → 正常解析，由流水线分片逻辑处理
- MinerU 未安装 → ParseFailedError（提示安装方式）
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from kbrefiner.core.parser.base import (
    BaseParser,
    EmptyDocumentError,
    EncryptedDocumentError,
    FileType,
    Formula,
    Heading,
    ParseFailedError,
    ParsedDocument,
    UnsupportedFileError,
)

# MinerU CLI 命令名
_MINERU_CMD = "mineru"

# Markdown 标题正则：# / ## / ### 等（最多 6 级）
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

# HTML 表格块正则（跨行，非贪婪）
_TABLE_RE = re.compile(r"<table[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)

# 行内公式 $...$（非贪婪，不跨行）
_INLINE_FORMULA_RE = re.compile(r"\$([^$\n]+)\$")
# 块级公式 $$...$$
_BLOCK_FORMULA_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)

# 加密 PDF 检测关键词
_ENCRYPTED_KEYWORDS = ["encrypted", "password", "加密", "密码保护"]

# MinerU 未安装检测
_NOT_INSTALLED_HINT = (
    "MinerU 未安装。请按以下方式之一安装：\n"
    "  pip install mineru          # Python SDK + CLI\n"
    "  或参考 https://github.com/opendatalab/MinerU 使用 Docker 部署"
)


class MineruParser(BaseParser):
    """MinerU 统一解析器，覆盖所有支持的格式。

    默认使用 CLI 调用（进程隔离，崩溃不影响主进程）。
    设置 use_sdk=True 可切换到 SDK 模式（需 mineru 包已安装）。
    """

    def __init__(self, use_sdk: bool = False, timeout: int = 600):
        """
        Args:
            use_sdk: True 用 SDK 进程内调用，False（默认）用 CLI
            timeout: CLI 超时秒数（大文档可调大）
        """
        self._use_sdk = use_sdk
        self._timeout = timeout

    @property
    def supported_types(self) -> set[FileType]:
        """MinerU 支持所有格式。"""
        return {
            FileType.PDF, FileType.DOCX, FileType.PPTX,
            FileType.XLSX, FileType.IMAGE, FileType.WEBPAGE,
        }

    def parse(self, file_path: str | Path) -> ParsedDocument:
        """解析文档，返回 ParsedDocument。

        流程：调用 MinerU → 定位 full.md → 读取 → 提取结构 → 构造 ParsedDocument
        """
        path = Path(file_path)
        file_type = FileType.from_path(path)

        if not self.supports(file_type):
            raise UnsupportedFileError(f"MinerU 不支持此格式: {file_type}")

        if not path.exists() and file_type != FileType.WEBPAGE:
            raise ParseFailedError(f"文件不存在: {path}")

        # 调用 MinerU 获取 Markdown
        markdown = self._run_mineru(path, file_type)

        if not markdown.strip():
            raise EmptyDocumentError(f"解析结果为空文档: {path}")

        # 提取结构化信息
        headings = self._extract_headings(markdown)
        tables = self._extract_tables(markdown)
        formulas = self._extract_formulas(markdown)
        page_count = self._detect_page_count(markdown)

        return ParsedDocument(
            source=str(path),
            file_type=file_type,
            markdown=markdown,
            headings=headings,
            tables=tables,
            formulas=formulas,
            page_count=page_count,
        )

    # =================================================================
    # MinerU 调用
    # =================================================================

    def _run_mineru(self, path: Path, file_type: FileType) -> str:
        """调用 MinerU 解析，返回 Markdown 文本。"""
        if self._use_sdk:
            return self._run_via_sdk(path)
        return self._run_via_cli(path)

    def _run_via_cli(self, path: Path) -> str:
        """通过 CLI 调用 MinerU。"""
        if shutil.which(_MINERU_CMD) is None:
            raise ParseFailedError(_NOT_INSTALLED_HINT)

        with tempfile.TemporaryDirectory(prefix="mineru_") as tmpdir:
            cmd = [_MINERU_CMD, "-p", str(path), "-o", tmpdir]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    encoding="utf-8",
                )
            except subprocess.TimeoutExpired:
                raise ParseFailedError(
                    f"MinerU 解析超时（{self._timeout}s），文档可能过大: {path}"
                )

            # 检测加密 PDF
            stderr_lower = (result.stderr or "").lower()
            if any(kw in stderr_lower for kw in _ENCRYPTED_KEYWORDS):
                raise EncryptedDocumentError(f"加密文档，无法解析: {path}")

            if result.returncode != 0:
                raise ParseFailedError(
                    f"MinerU 解析失败 (exit={result.returncode}): {result.stderr[:500]}"
                )

            # 定位 full.md：MinerU 输出结构为 <output_dir>/<stem>/full.md
            md_path = self._find_markdown(tmpdir, path.stem)
            if md_path is None:
                raise ParseFailedError(
                    f"MinerU 未输出 Markdown 文件，输出目录: {tmpdir}"
                )

            return md_path.read_text(encoding="utf-8")

    def _run_via_sdk(self, path: Path) -> str:
        """通过 SDK 调用 MinerU（进程内）。"""
        try:
            from mineru import cli as mineru_cli
        except ImportError:
            raise ParseFailedError(_NOT_INSTALLED_HINT)

        with tempfile.TemporaryDirectory(prefix="mineru_") as tmpdir:
            try:
                # MinerU SDK API（3.x 版本）
                mineru_cli(path=str(path), output_dir=tmpdir)
            except Exception as e:
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in _ENCRYPTED_KEYWORDS):
                    raise EncryptedDocumentError(f"加密文档: {path}") from e
                raise ParseFailedError(f"MinerU SDK 调用失败: {e}") from e

            md_path = self._find_markdown(tmpdir, path.stem)
            if md_path is None:
                raise ParseFailedError(f"MinerU SDK 未输出 Markdown: {tmpdir}")

            return md_path.read_text(encoding="utf-8")

    def _find_markdown(self, output_dir: str, stem: str) -> Path | None:
        """在 MinerU 输出目录中定位 full.md。

        MinerU 输出结构：<output_dir>/<stem>/full.md 或 <output_dir>/full.md
        """
        # 优先找 <output_dir>/<stem>/full.md
        candidate = Path(output_dir) / stem / "full.md"
        if candidate.exists():
            return candidate
        # 回退：递归查找任意 full.md
        for md in Path(output_dir).rglob("*.md"):
            if md.name in ("full.md", "auto.md"):
                return md
        return None

    # =================================================================
    # 结构化提取
    # =================================================================

    @staticmethod
    def _extract_headings(markdown: str) -> list[Heading]:
        """从 Markdown 提取标题层级。"""
        headings: list[Heading] = []
        for i, line in enumerate(markdown.splitlines()):
            m = _HEADING_RE.match(line)
            if m:
                level = len(m.group(1))
                text = m.group(2).strip()
                headings.append(Heading(level=level, text=text, line_start=i, line_end=i))
        return headings

    @staticmethod
    def _extract_tables(markdown: str) -> list[Table]:
        """提取 HTML 表格块及其位置。"""
        from kbrefiner.core.parser.base import Table

        tables: list[Table] = []
        # 按行定位表格起始位置
        lines = markdown.splitlines()
        flat = markdown
        for m in _TABLE_RE.finditer(flat):
            # 计算表格块在原文中的起始行号
            char_pos = m.start()
            line_start = flat[:char_pos].count("\n")
            # 估算结束行
            block_lines = m.group(0).count("\n")
            tables.append(Table(
                content_html=m.group(0),
                line_start=line_start,
                line_end=line_start + block_lines,
            ))
        return tables

    @staticmethod
    def _extract_formulas(markdown: str) -> list[Formula]:
        """提取 LaTeX 公式（块级 + 行内）。"""
        formulas: list[Formula] = []
        lines = markdown.splitlines()
        flat = markdown

        # 块级公式 $$...$$
        for m in _BLOCK_FORMULA_RE.finditer(flat):
            char_pos = m.start()
            line_start = flat[:char_pos].count("\n")
            formulas.append(Formula(latex=m.group(1).strip(), line_start=line_start))

        # 行内公式 $...$（排除已被块级匹配的部分）
        consumed = {m.span() for m in _BLOCK_FORMULA_RE.finditer(flat)}
        for m in _INLINE_FORMULA_RE.finditer(flat):
            # 跳过与块级公式重叠的
            if any(s <= m.start() < e for s, e in consumed):
                continue
            char_pos = m.start()
            line_start = flat[:char_pos].count("\n")
            formulas.append(Formula(latex=m.group(1).strip(), line_start=line_start))

        return formulas

    @staticmethod
    def _detect_page_count(markdown: str) -> int:
        """尝试从 Markdown 检测页数。

        MinerU 有时在 Markdown 中插入页面分隔标记，如 <!-- page --> 或特定注释。
        无法检测时返回 0。
        """
        # MinerU 常见页面分隔符
        page_markers = [
            r"<!--\s*page\s*-->",
            r"<!--\s*Page\s*\d+\s*-->",
            r"\n---\n",  # 分页线（需谨慎，可能与 YAML front matter 冲突）
        ]
        count = 0
        for pattern in page_markers:
            count = max(count, len(re.findall(pattern, markdown, re.IGNORECASE)))
        # 有分隔符则页数 = 分隔符数 + 1，否则 0
        return count + 1 if count > 0 else 0


# =====================================================================
# 解析器工厂
# =====================================================================


class ParserFactory:
    """解析器工厂，按文件类型和配置分发解析器。

    支持三种后端：
    - auto（默认）：检测 MinerU 是否安装，已安装则用 MinerU，否则用 SimpleParser
    - mineru：强制使用 MinerU（需 pip install mineru）
    - simple：强制使用轻量解析器（pypdf + python-docx）
    """

    _parser: BaseParser | None = None
    _backend: str | None = None

    @classmethod
    def get_parser(
        cls,
        file_type: FileType | None = None,
        backend: str = "auto",
    ) -> BaseParser:
        """获取解析器实例。

        Args:
            file_type: 文件类型（当前解析器均支持全部类型，可忽略）
            backend: 解析后端，auto / mineru / simple
        """
        if cls._parser is not None and cls._backend == backend:
            return cls._parser

        if backend == "simple":
            from kbrefiner.core.parser.simple_parser import SimpleParser
            cls._parser = SimpleParser()
        elif backend == "mineru":
            cls._parser = MineruParser()
        else:
            # auto: 优先 MinerU，回退 SimpleParser
            if cls._is_mineru_available():
                cls._parser = MineruParser()
            else:
                from kbrefiner.core.parser.simple_parser import SimpleParser
                cls._parser = SimpleParser()

        cls._backend = backend
        return cls._parser

    @staticmethod
    def _is_mineru_available() -> bool:
        """检测 MinerU 是否已安装。"""
        import shutil
        return shutil.which("mineru") is not None

    @classmethod
    def _reset(cls):
        """重置单例（测试用）。"""
        cls._parser = None
        cls._backend = None
