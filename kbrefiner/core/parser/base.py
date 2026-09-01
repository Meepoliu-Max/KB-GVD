"""文档解析层抽象基类。

定义所有文档解析器（MinerU 及未来其他解析器）的统一接口。
Stage 1 及后续流水线只依赖 ParsedDocument，不关心具体解析器实现。

设计要点：
- 解析器输出 Markdown + 结构化元数据（标题层级、表格位置、公式位置）
- 支持的格式由各解析器声明，ParserFactory 按文件类型分发
- 异常分三级：不支持、解析失败、内容异常（空文档/加密/乱码）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class FileType(str, Enum):
    """支持的输入文件类型。

    MVP 1.0 范围：PDF / Word / TXT / Markdown。
    不支持图片、视频、音频、网页（参见产品方向定义）。
    """

    PDF = "pdf"
    DOCX = "docx"
    MARKDOWN = "markdown"  # md/markdown/txt 直读（无需解析）

    @classmethod
    def from_path(cls, path: str | Path) -> "FileType":
        """根据文件扩展名推断类型。"""
        ext = Path(path).suffix.lower().lstrip(".")
        mapping = {
            "pdf": cls.PDF,
            "docx": cls.DOCX,
            "doc": cls.DOCX,
            "md": cls.MARKDOWN,
            "markdown": cls.MARKDOWN,
            "txt": cls.MARKDOWN,
        }
        if ext not in mapping:
            raise UnsupportedFileError(f"不支持的文件格式: .{ext}")
        return mapping[ext]


@dataclass
class Heading:
    """单个标题节点，保留层级结构。"""

    level: int  # 1=H1, 2=H2, ...
    text: str
    line_start: int  # 在 markdown 中的起始行号（0-based）
    line_end: int = 0  # 结束行号


@dataclass
class Table:
    """单个表格的结构化信息。

    content_html 为 MinerU 输出的 HTML 表格，合并单元格已拆解。
    """

    content_html: str
    line_start: int
    line_end: int = 0


@dataclass
class Formula:
    """单个公式信息。"""

    latex: str
    line_start: int


@dataclass
class ParsedDocument:
    """文档解析结果，流水线的输入。

    MinerU 已完成：去页眉页脚、表格规整、合并单元格拆解、OCR、版面还原。
    本结构保留 Markdown 原文 + 结构化索引，供 Stage 1 轻校验使用。
    """

    source: str  # 原始文件名或 URL
    file_type: FileType
    markdown: str  # MinerU 输出的完整 Markdown
    headings: list[Heading] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    formulas: list[Formula] = field(default_factory=list)
    page_count: int = 0  # PDF 页数，非 PDF 为 0
    char_count: int = 0  # markdown 字符数

    def __post_init__(self):
        if not self.char_count:
            self.char_count = len(self.markdown)

    @property
    def is_empty(self) -> bool:
        """文档是否为空（仅空白字符）。"""
        return len(self.markdown.strip()) == 0


# =====================================================================
# 异常体系
# =====================================================================


class ParserError(Exception):
    """解析器基础异常。"""


class UnsupportedFileError(ParserError):
    """文件类型不支持。"""


class ParseFailedError(ParserError):
    """解析过程失败（MinerU 报错、超时等）。"""


class EmptyDocumentError(ParserError):
    """解析结果为空文档。"""


class EncryptedDocumentError(ParserError):
    """加密 PDF，无法解析。"""


# =====================================================================
# 抽象基类
# =====================================================================


class BaseParser(ABC):
    """文档解析器抽象基类。

    所有解析器实现 parse 方法，返回 ParsedDocument。
    流水线通过 ParserFactory.get_parser(file_type) 获取具体实现，
    不直接依赖具体解析器类。
    """

    @property
    @abstractmethod
    def supported_types(self) -> set[FileType]:
        """该解析器支持的文件类型集合。"""

    @abstractmethod
    def parse(self, file_path: str | Path) -> ParsedDocument:
        """解析文档，返回 ParsedDocument。

        Args:
            file_path: 文件路径（或 URL，对 webpage 类型）

        Raises:
            UnsupportedFileError: 文件类型不在 supported_types 中
            ParseFailedError: 解析过程失败
            EmptyDocumentError: 解析结果为空
            EncryptedDocumentError: 加密文档
        """
        ...

    def supports(self, file_type: FileType) -> bool:
        """是否支持指定文件类型。"""
        return file_type in self.supported_types
