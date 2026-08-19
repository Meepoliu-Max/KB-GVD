"""导出器：将 KbDocument 转换为 Agent 平台可直接导入的格式。

支持的目标格式：
- coze_qa_csv:    扣子「表格类型」知识库，问题/答案两列（QA 检索场景）
- coze_text_csv:  扣子「表格类型」知识库，标题/正文两列（内容检索场景）
- dify_qa_csv:    Dify「Q&A 模式」知识库，question/answer 列
- dify_text_csv:  Dify「通用模式」知识库，分段文本列
- dify_jsonl:     Dify「API 批量导入」格式，name + text + metadata（含打标元数据）
- json:           KBRefiner 内部完整 JSON（保持兼容）

用法：
    from kbrefiner.core.exporter import get_exporter

    exporter = get_exporter("coze_qa")
    content, ext = exporter.export(kb_document)

格式规范来源（2026-08 调研）：
- 扣子：表格知识库支持 .csv 上传，需列名+数据；QA 场景定义「问题」「答案」列，
  并在导入时将「问题」列设为索引列
- Dify：通用模式/Q&A 模式支持 CSV 模板导入；API 批量导入用
  name + text + metadata JSON 结构，metadata 字段可在工作流检索后读取
"""
from __future__ import annotations

import csv
import io
import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kbrefiner.models.schemas import KbDocument


# =====================================================================
# 基类
# =====================================================================


class BaseExporter(ABC):
    """导出器抽象基类。"""

    #: 导出格式名（如 coze_qa）
    name: str = ""
    #: 输出文件扩展名（如 .csv）
    extension: str = ""

    @abstractmethod
    def export(self, doc: "KbDocument") -> str:
        """将 KbDocument 转换为目标格式字符串。

        Args:
            doc: 流水线最终产出文档

        Returns:
            目标格式的文件内容字符串
        """

    def export_to_file(self, doc: "KbDocument", path) -> None:
        """导出并写入文件。"""
        content = self.export(doc)
        with open(path, "w", encoding="utf-8-sig" if self.extension == ".csv" else "utf-8", newline="") as f:
            f.write(content)


def _csv_dump(header: list[str], rows: list[list[str]]) -> str:
    """生成 UTF-8 CSV 字符串（带 BOM，Excel 打开中文不乱码）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


# =====================================================================
# 扣子（Coze）导出器
# =====================================================================


class CozeQaExporter(BaseExporter):
    """扣子表格知识库 QA 格式：「问题」「答案」两列。

    导入步骤（扣子端）：
    1. 创建知识库 → 表格类型 → 本地文档 → 上传本 CSV
    2. 表结构确认后，将「问题」列指定为语义匹配字段（索引列）
    3. 「答案」列作为大模型回答参考
    """

    name = "coze_qa"
    extension = ".csv"

    def export(self, doc: "KbDocument") -> str:
        rows: list[list[str]] = []
        for atom in doc.knowledge_atoms:
            for qa in atom.qa_pairs:
                # 问题前拼上 chunk 标题，增强检索上下文（扣子按行独立检索）
                q = f"[{atom.title}] {qa.question}"
                rows.append([q, qa.answer])
        return _csv_dump(["问题", "答案"], rows)


class CozeTextExporter(BaseExporter):
    """扣子表格知识库文本格式：「标题」「正文」两列（知识块导入）。"""

    name = "coze_text"
    extension = ".csv"

    def export(self, doc: "KbDocument") -> str:
        rows = [[atom.title, atom.content] for atom in doc.knowledge_atoms]
        return _csv_dump(["标题", "正文"], rows)


# =====================================================================
# Dify 导出器
# =====================================================================


class DifyQaExporter(BaseExporter):
    """Dify「Q&A 模式」知识库 CSV：question/answer 列。

    导入步骤（Dify 端）：
    1. 创建知识库 → 上传本 CSV → 分段模式选择「Q&A 模式」
    2. 系统按 question 列生成向量，answer 列作为召回答案
    """

    name = "dify_qa"
    extension = ".csv"

    def export(self, doc: "KbDocument") -> str:
        rows: list[list[str]] = []
        for atom in doc.knowledge_atoms:
            for qa in atom.qa_pairs:
                rows.append([qa.question, qa.answer])
        return _csv_dump(["question", "answer"], rows)


class DifyTextExporter(BaseExporter):
    """Dify「通用模式」知识库 CSV：content/segment 列（知识块导入）。"""

    name = "dify_text"
    extension = ".csv"

    def export(self, doc: "KbDocument") -> str:
        rows = [[atom.title, atom.content] for atom in doc.knowledge_atoms]
        return _csv_dump(["content", "segment"], rows)


class DifyJsonlExporter(BaseExporter):
    """Dify「API 批量导入」JSONL 格式。

    每行一个 JSON 对象：
    {
      "name": "文档名（chunk 标题）",
      "text": "分段内容",
      "metadata": { "chunk_id": ..., "doc_type": ..., 5 维打标字段, ... }
    }

    使用方式（Dify 端）：
    POST /v1/datasets/{dataset_id}/documents
    body: {"data": [ <本文件每行的对象> ]}
    metadata 中的字段可在工作流检索节点通过
    {{retrieval.results[0].metadata.xxx}} 读取
    """

    name = "dify_jsonl"
    extension = ".jsonl"

    def export(self, doc: "KbDocument") -> str:
        lines: list[str] = []
        for atom in doc.knowledge_atoms:
            obj = {
                "name": atom.title,
                "text": atom.content,
                "metadata": {
                    "chunk_id": atom.chunk_id,
                    "doc_type": atom.doc_type.value,
                    **atom.metadata.model_dump(),
                    "qa_count": len(atom.qa_pairs),
                },
            }
            lines.append(json.dumps(obj, ensure_ascii=False))
        return "\n".join(lines) + "\n"


# =====================================================================
# 内部 JSON（保持兼容）
# =====================================================================


class JsonExporter(BaseExporter):
    """KBRefiner 内部完整 JSON（默认输出，保持兼容）。"""

    name = "json"
    extension = ".json"

    def export(self, doc: "KbDocument") -> str:
        return doc.model_dump_json(indent=2, ensure_ascii=False)


# =====================================================================
# 注册表与入口
# =====================================================================


_EXPORTERS: dict[str, type[BaseExporter]] = {
    "coze_qa": CozeQaExporter,
    "coze_text": CozeTextExporter,
    "dify_qa": DifyQaExporter,
    "dify_text": DifyTextExporter,
    "dify_jsonl": DifyJsonlExporter,
    "json": JsonExporter,
}

# 支持的格式名列表（CLI/SDK/API 用于校验与提示）
SUPPORTED_FORMATS = list(_EXPORTERS.keys())


def get_exporter(fmt: str) -> BaseExporter:
    """按格式名获取导出器实例。

    Args:
        fmt: 格式名（coze_qa / coze_text / dify_qa / dify_text / dify_jsonl / json）

    Returns:
        导出器实例

    Raises:
        ValueError: 格式名不支持
    """
    cls = _EXPORTERS.get(fmt)
    if cls is None:
        raise ValueError(
            f"不支持的导出格式: '{fmt}'，支持: {', '.join(SUPPORTED_FORMATS)}"
        )
    return cls()


def export_all(doc: "KbDocument") -> dict[str, str]:
    """导出全部格式。

    Returns:
        {格式名: 文件内容字符串}
    """
    return {fmt: get_exporter(fmt).export(doc) for fmt in _EXPORTERS}


__all__ = [
    "BaseExporter",
    "CozeQaExporter",
    "CozeTextExporter",
    "DifyQaExporter",
    "DifyTextExporter",
    "DifyJsonlExporter",
    "JsonExporter",
    "SUPPORTED_FORMATS",
    "get_exporter",
    "export_all",
]
