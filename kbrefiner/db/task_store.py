"""任务状态持久化存储（SQLite）。

替代内存 dict，任务状态重启不丢失。
使用 Python 标准库 sqlite3，零外部依赖，线程安全（check_same_thread=False + 锁）。

表结构：
    tasks(task_id TEXT PRIMARY KEY,
          status TEXT, filename TEXT,
          created_at REAL, updated_at REAL,
          file_size INTEGER, error TEXT, result TEXT)

除常规方法外，实现 Mapping 风格接口（__getitem__/__setitem__/__contains__/items/get），
使路由层可像 dict 一样使用（_task_store[tid] = {...}）。

用法：
    from kbrefiner.db.task_store import TaskStore

    store = TaskStore("./data/tasks.db")
    store.create("abc123", filename="doc.pdf", file_size=1024)
    store.update_status("abc123", "processing")
    info = store.get("abc123")
    tasks = store.list_all()
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional


# 写入白名单：__setitem__ 接受的字段
_FIELDS = ("status", "filename", "created_at", "updated_at", "file_size", "error", "result")


class TaskStore:
    """SQLite 任务状态存储，兼容 dict 式读写。"""

    def __init__(self, db_path: str | Path = "./data/tasks.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_table()

    def _init_table(self) -> None:
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT DEFAULT 'pending',
                    filename TEXT,
                    created_at REAL,
                    updated_at REAL,
                    file_size INTEGER DEFAULT 0,
                    error TEXT,
                    result TEXT
                )
            """)
            # 兼容旧表：缺失列时自动补齐
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
            if "result" not in columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN result TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC)"
            )
            self._conn.commit()

    def create(self, task_id: str, filename: str = "", file_size: int = 0,
               status: str = "pending") -> None:
        """创建任务记录。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(task_id, status, filename, created_at, updated_at, file_size, error, result) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                (task_id, status, filename, now, now, file_size),
            )
            self._conn.commit()

    def update_status(self, task_id: str, status: str,
                      error: Optional[str] = None) -> None:
        """更新任务状态（可选带错误信息）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, error = ? WHERE task_id = ?",
                (status, time.time(), error, task_id),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """行转 dict；result 字段反序列化为对象。"""
        d = dict(row)
        if d.get("result") is not None:
            try:
                d["result"] = json.loads(d["result"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def get(self, task_id: str, default: Any = None) -> Any:
        """查询单个任务，不存在返回 default。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else default

    def list_all(self) -> list[dict[str, Any]]:
        """按创建时间倒序列出全部任务。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ===== Mapping 兼容接口（供路由层 dict 式调用） =====

    def __getitem__(self, task_id: str) -> dict[str, Any]:
        info = self.get(task_id)
        if info is None:
            raise KeyError(task_id)
        return info

    def __setitem__(self, task_id: str, value: dict[str, Any]) -> None:
        """dict 式写入：_task_store[tid] = {"status": ..., "filename": ..., ...}。"""
        if not isinstance(value, dict):
            raise TypeError(f"TaskStore 仅接受 dict 值，收到 {type(value).__name__}")
        now = time.time()
        row = {k: value.get(k) for k in _FIELDS}
        row["updated_at"] = now
        row.setdefault("status", "pending")
        row.setdefault("filename", "")
        row["created_at"] = row.get("created_at") or now
        row["file_size"] = row.get("file_size") or 0
        # result 序列化为 JSON 文本存储
        result = row.get("result")
        row["result"] = json.dumps(result, ensure_ascii=False) if result is not None else None
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(task_id, status, filename, created_at, updated_at, file_size, error, result) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, row["status"], row["filename"], row["created_at"], now,
                 row["file_size"], row.get("error"), row["result"]),
            )
            self._conn.commit()

    def __contains__(self, task_id: object) -> bool:
        if not isinstance(task_id, str):
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row is not None

    def __len__(self) -> int:
        with self._lock:
            (count,) = self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        return int(count)

    def items(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """按创建时间倒序返回 (task_id, info) 迭代器。"""
        for info in self.list_all():
            yield info["task_id"], info

    def delete(self, task_id: str) -> None:
        """删除任务记录。"""
        with self._lock:
            self._conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


__all__ = ["TaskStore"]
