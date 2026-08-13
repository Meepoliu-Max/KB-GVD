"""文档解析层公共接口。

用法：
    from kbrefiner.core.parser import parse_document, ParserFactory, ParsedDocument

    # 推荐：工厂模式（自动检测 MinerU）
    doc = parse_document("report.pdf")

    # 指定后端
    parser = ParserFactory.get_parser(backend="simple")
    doc = parser.parse("report.pdf")
"""
from kbrefiner.core.parser.base import (
    BaseParser,
    EmptyDocumentError,
    EncryptedDocumentError,
    FileType,
    Formula,
    Heading,
    ParseFailedError,
    ParsedDocument,
    ParserError,
    Table,
    UnsupportedFileError,
)
from kbrefiner.core.parser.mineru_parser import MineruParser, ParserFactory
from kbrefiner.core.parser.simple_parser import SimpleParser

__all__ = [
    # 工厂与便捷函数
    "MineruParser",
    "SimpleParser",
    "ParserFactory",
    # 数据结构
    "ParsedDocument",
    "Heading",
    "Table",
    "Formula",
    "FileType",
    # 基类
    "BaseParser",
    # 异常
    "ParserError",
    "UnsupportedFileError",
    "ParseFailedError",
    "EmptyDocumentError",
    "EncryptedDocumentError",
]


def parse_document(file_path: str, backend: str = "auto", **kwargs) -> ParsedDocument:
    """便捷函数：解析文档，自动选择解析器。

    Args:
        file_path: 文件路径
        backend: 解析后端，auto / mineru / simple
        **kwargs: 传递给解析器的参数
    """
    parser = ParserFactory.get_parser(backend=backend)
    return parser.parse(file_path)
