"""Celery 异步任务模块。

提供文档处理的 Celery 任务，在独立 worker 进程中执行。
与 WebSocket 进度推送通过文件系统中间结果桥接。
"""
from __future__ import annotations

from kbrefiner.tasks.celery_app import celery_app
from kbrefiner.tasks.process_task import process_document_task

__all__ = [
    "celery_app",
    "process_document_task",
]