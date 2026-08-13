"""Celery 应用配置。

Worker 启动命令：
    celery -A app.tasks.celery_app worker --loglevel=info

使用 Redis 作为 broker 和 result backend（配置见 Settings）。
"""
from __future__ import annotations

from celery import Celery

from app.config import Settings

_settings = Settings()

celery_app = Celery(
    "kbrefiner",
    broker=_settings.celery_broker_url,
    backend=_settings.celery_result_backend,
    include=["app.tasks.process_task"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=600,  # 单个任务软限制 10 分钟
    task_time_limit=900,  # 硬限制 15 分钟
)


__all__ = ["celery_app"]