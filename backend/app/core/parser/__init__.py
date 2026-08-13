"""文档解析层公共接口。

用法：
    from app.core.parser import parse_document, MineruParser, ParsedDocument

    # 推荐：工厂模式
    doc = parse_document("report.pdf")

    # 直接使用 MineruParser
    parser = MineruParser()
    doc = parser.parse("report.pdf")
"""
from app.core.parser.base import (
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
from app.core.parser.mineru_parser import MineruParser, ParserFactory

__all__ = [
    # 工厂与便捷函数
    "MineruParser",
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


def parse_document(file_path: str, **kwargs) -> ParsedDocument:
    """便捷函数：解析文档，自动选择解析器。

    Args:
        file_path: 文件路径
        **kwargs: 传递给 MineruParser 的参数（use_sdk, timeout）
    """
    parser = MineruParser(**kwargs) if kwargs else ParserFactory.get_parser()
    return parser.parse(file_path)
