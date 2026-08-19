"""KBRefiner Python SDK。

封装 Parser + Pipeline，提供一行代码处理文档的编程接口。

用法：
    from kbrefiner import KBRefiner

    # 从 .env / 环境变量自动读取配置
    kb = KBRefiner()

    # 处理文件（自动解析 + 四阶流水线）
    result = kb.process("policy.pdf")

    # 处理已有 Markdown 文本
    result = kb.process_text(markdown_text, source="manual.md")

    # result 是 KbDocument 对象
    print(result.model_dump_json(indent=2))

    # 指定输出目录
    kb = KBRefiner(output_dir="./output")
    result = kb.process("document.pdf")

    # 自定义 LLM 配置
    kb = KBRefiner(
        api_key="sk-xxx",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
    )
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Literal

from kbrefiner.config import get_settings
from kbrefiner.core.llm import DeepSeekClient, LLMConfig
from kbrefiner.core.parser import ParserFactory, FileType
from kbrefiner.core.pipeline import Pipeline, PipelineConfig

logger = logging.getLogger(__name__)


class KBRefiner:
    """KBRefiner SDK 主类。

    封装文档解析 + 四阶流水线，提供同步和异步接口。

    Attributes:
        llm_client: LLM 客户端实例
        pipeline: 流水线实例
        parser_backend: 解析后端 (auto/mineru/simple)
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        model_pro: str | None = None,
        output_dir: str | Path = "./output",
        parser_backend: Literal["auto", "mineru", "simple"] = "auto",
        enable_checkpoint: bool = True,
    ):
        """初始化 KBRefiner。

        Args:
            api_key: LLM API Key（None 则从环境变量读取）
            base_url: LLM API Base URL（None 则从环境变量读取）
            model: 默认模型名（None 则从环境变量读取）
            model_pro: Pro 模型名（None 则从环境变量读取）
            output_dir: 输出目录
            parser_backend: 解析后端
            enable_checkpoint: 是否启用断点续跑
        """
        settings = get_settings()

        # 模型名：显式参数 > 环境变量 > 默认枚举
        effective_model = model or settings.llm_model
        llm_config = LLMConfig(
            api_key=api_key or settings.llm_api_key,
            base_url=base_url or settings.llm_base_url,
        )
        llm_config.default_model = effective_model  # type: ignore

        self.llm_client = DeepSeekClient(config=llm_config)
        self.parser_backend = parser_backend
        self._model_pro = model_pro or settings.llm_model_pro

        self.pipeline = Pipeline(
            llm_client=self.llm_client,
            config=PipelineConfig(
                output_dir=Path(output_dir),
                enable_checkpoint=enable_checkpoint,
                stage3_model=self._model_pro,
            ),
        )

    def process(
        self,
        file_path: str | Path,
        doc_title: str | None = None,
    ):
        """处理文档文件（解析 + 四阶流水线）。

        Args:
            file_path: 文件路径（PDF / DOCX 等）
            doc_title: 文档标题（可选，默认用文件名）

        Returns:
            KbDocument: 处理结果

        Raises:
            FileNotFoundError: 文件不存在
            ParseFailedError: 解析失败
            各种 Pipeline 异常
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {path}")

        logger.info("开始处理文档: %s", path)

        # 解析文档
        file_type = FileType.from_path(path)
        parser = ParserFactory.get_parser(file_type, backend=self.parser_backend)
        parsed = parser.parse(str(path))

        logger.info("文档解析完成: %s (markdown %d 字符)", path, len(parsed.markdown))

        # 运行流水线
        return self.process_text(
            parsed.markdown,
            source=str(path),
            doc_title=doc_title or path.stem,
        )

    def process_text(
        self,
        markdown: str,
        source: str = "input.md",
        doc_title: str | None = None,
    ):
        """处理 Markdown 文本（跳过解析，直接运行流水线）。

        Args:
            markdown: Markdown 文本
            source: 文档来源标识
            doc_title: 文档标题

        Returns:
            KbDocument: 处理结果
        """
        return asyncio.run(
            self.pipeline.run(
                markdown=markdown,
                document_source=source,
                doc_title=doc_title,
            )
        )

    def close(self):
        """关闭 LLM 客户端连接。"""
        self.llm_client.close()


__all__ = ["KBRefiner"]
