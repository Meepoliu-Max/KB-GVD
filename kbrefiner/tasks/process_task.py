"""Celery 异步文档处理任务。

在 Celery worker 进程中独立执行：
1. 从 upload_dir 读取文件
2. 用 MinerU 解析
3. 运行 4 阶 Pipeline
4. 保存结果到 output_dir
5. 更新 Redis 任务状态

Worker 启动：
    celery -A app.tasks.celery_app worker --loglevel=info
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from kbrefiner.config import Settings
from kbrefiner.core.llm import DeepSeekClient, LLMConfig
from kbrefiner.core.pipeline import Pipeline, PipelineConfig
from kbrefiner.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="process_document", max_retries=3, default_retry_delay=10)
def process_document_task(self, file_id: str, filename: str | None = None) -> dict:
    """异步处理文档的 Celery 任务。

    Args:
        file_id: 上传时返回的文件 ID
        filename: 可选，原始文件名

    Returns:
        {"task_id": "...", "status": "completed", "result": {...}}

    Raises:
        self.retry(exc=exc) 在临时失败时重试
    """
    settings = Settings()
    logger.info("Celery 任务开始: file_id=%s", file_id)

    # 1. 查找文件
    upload_dir = Path(settings.upload_dir)
    candidates = list(upload_dir.glob(f"{file_id}.*"))
    if not candidates:
        raise FileNotFoundError(f"文件 {file_id} 不存在")

    file_path = candidates[0]
    doc_name = filename or file_path.name

    # 2. MinerU 解析
    from kbrefiner.core.parser import ParserFactory, FileType

    file_type = FileType.from_path(file_path)
    parser = ParserFactory.get_parser(file_type)
    try:
        parsed = parser.parse(str(file_path))
    except Exception as e:
        logger.error("MinerU 解析失败: %s", e)
        raise

    markdown = parsed.markdown

    # 3. 运行 Pipeline
    output_dir = Path(settings.output_dir) / file_id
    output_dir.mkdir(parents=True, exist_ok=True)

    llm_client = DeepSeekClient(
        config=LLMConfig(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
    )

    pipeline = Pipeline(
        llm_client=llm_client,
        config=PipelineConfig(
            output_dir=output_dir,
            stage_retries=2,
            enable_checkpoint=True,
            partition_prefix="P1",
        ),
    )

    import asyncio

    try:
        doc = asyncio.run(
            pipeline.run(
                markdown=markdown,
                document_source=doc_name,
            )
        )
        result = json.loads(doc.model_dump_json(ensure_ascii=False))
        logger.info("Celery 任务完成: file_id=%s", file_id)
        return {"task_id": file_id, "status": "completed", "result": result}
    except Exception as e:
        logger.error("Pipeline 执行失败: %s", e)
        # 可重试：临时错误
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        raise


__all__ = ["process_document_task"]