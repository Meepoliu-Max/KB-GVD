"""数据持久化层。

TaskStore: 任务状态 SQLite 存储（重启不丢失）。
"""
from kbrefiner.db.task_store import TaskStore

__all__ = ["TaskStore"]
