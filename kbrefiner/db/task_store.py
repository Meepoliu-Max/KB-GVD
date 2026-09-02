"""任务状态持久化存储（SQLite）。

替代内存 dict，任务状态重启不丢失。
使用 Python 标准库 sqlite3，零外部依赖，线程安全（check_same_thread=False + 锁）。

表结构：
    tasks(task_id TEXT PRIMARY KEY,
          status TEXT, filename TEXT, user_id TEXT,
          created_at REAL, updated_at REAL,
          file_size INTEGER, token_consumed INTEGER DEFAULT 0,
          error TEXT, result TEXT)

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
_FIELDS = (
    "status", "filename", "user_id", "created_at", "updated_at",
    "file_size", "token_consumed", "error", "result",
)


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
                    user_id TEXT,
                    created_at REAL,
                    updated_at REAL,
                    file_size INTEGER DEFAULT 0,
                    token_consumed INTEGER DEFAULT 0,
                    error TEXT,
                    result TEXT
                )
            """)
            # 兼容旧表：缺失列时自动补齐
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
            if "result" not in columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN result TEXT")
            if "user_id" not in columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN user_id TEXT")
            if "token_consumed" not in columns:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN token_consumed INTEGER DEFAULT 0"
                )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id)"
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

    def list_all(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """按创建时间倒序列出任务；指定 user_id 时只列该用户的任务。"""
        sql = "SELECT * FROM tasks"
        params: tuple = ()
        if user_id is not None:
            sql += " WHERE user_id = ?"
            params = (user_id,)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def add_tokens(self, task_id: str, tokens: int) -> None:
        """累加任务的 Token 消耗（LLM 调用后回写）。"""
        if tokens <= 0:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET token_consumed = COALESCE(token_consumed, 0) + ?, "
                "updated_at = ? WHERE task_id = ?",
                (int(tokens), time.time(), task_id),
            )
            self._conn.commit()

    def stats_by_user(self) -> dict[str, dict[str, int]]:
        """按用户聚合任务统计（管理后台用户列表/详情用）。

        Returns:
            {user_id: {"task_count": n, "upload_count": n, "token_total": n}}
            user_id 为 NULL 的匿名任务不参与聚合。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, COUNT(*) AS task_count, "
                "SUM(CASE WHEN file_size > 0 THEN 1 ELSE 0 END) AS upload_count, "
                "COALESCE(SUM(token_consumed), 0) AS token_total "
                "FROM tasks WHERE user_id IS NOT NULL GROUP BY user_id"
            ).fetchall()
        return {
            r["user_id"]: {
                "task_count": int(r["task_count"] or 0),
                "upload_count": int(r["upload_count"] or 0),
                "token_total": int(r["token_total"] or 0),
            }
            for r in rows
        }

    def daily_stats(self, days: int = 7) -> list[dict[str, Any]]:
        """近 N 天每日任务数与 Token 消耗（本地时区日期，管理仪表盘趋势图用）。

        Returns:
            [{"date": "2026-08-21", "task_count": 3, "token_total": 12000}, ...]
        """
        since = time.time() - days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT date(created_at, 'unixepoch', 'localtime') AS d, "
                "COUNT(*) AS task_count, "
                "COALESCE(SUM(token_consumed), 0) AS token_total "
                "FROM tasks WHERE created_at >= ? GROUP BY d ORDER BY d",
                (since,),
            ).fetchall()
        return [
            {
                "date": r["d"],
                "task_count": int(r["task_count"] or 0),
                "token_total": int(r["token_total"] or 0),
            }
            for r in rows
        ]

    def user_daily_trend(
        self, user_id: str, days: int = 7
    ) -> list[dict[str, Any]]:
        """某用户近 N 天（含今日，本地时区）每日任务数与 Token 消耗趋势。

        返回长度恒为 days 的列表，按日期升序（days-1 天前 ~ 今日），
        空日期填 0。

        Returns:
            [{"date": "2026-08-26", "task_count": 0, "token_total": 0},
             {"date": "2026-08-27", "task_count": 3, "token_total": 12000},
             ... ]  # 长度 = days，升序
        """
        now_ts = time.time()
        since = now_ts - days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT date(created_at, 'unixepoch', 'localtime') AS d, "
                "COUNT(*) AS task_count, "
                "COALESCE(SUM(token_consumed), 0) AS token_total "
                "FROM tasks WHERE user_id = ? AND created_at >= ? "
                "GROUP BY d ORDER BY d",
                (user_id, since),
            ).fetchall()
        stats_map = {
            r["d"]: {
                "task_count": int(r["task_count"] or 0),
                "token_total": int(r["token_total"] or 0),
            }
            for r in rows
        }
        # 生成本地时区 N 天的日期字符串（从 days-1 天前到今日，升序）
        now = time.localtime()
        today_start = time.mktime(
            time.struct_time(
                (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)
            )
        )
        result: list[dict[str, Any]] = []
        for i in range(days - 1, -1, -1):
            ts = today_start - i * 86400
            lt = time.localtime(ts)
            date_str = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
            s = stats_map.get(date_str, {"task_count": 0, "token_total": 0})
            result.append(
                {
                    "date": date_str,
                    "task_count": s["task_count"],
                    "token_total": s["token_total"],
                }
            )
        return result

    def daily_user_stats(self, user_id: str) -> dict[str, int]:
        """某用户今日（本地时区）任务数 / 上传文档数 / Token 消耗（配额执法用）。"""
        now = time.localtime()
        today_start = time.mktime(
            time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS task_count, "
                "SUM(CASE WHEN file_size > 0 THEN 1 ELSE 0 END) AS upload_count, "
                "COALESCE(SUM(token_consumed), 0) AS token_total "
                "FROM tasks WHERE user_id = ? AND created_at >= ?",
                (user_id, today_start),
            ).fetchone()
        return {
            "task_count": int(row["task_count"] or 0),
            "upload_count": int(row["upload_count"] or 0),
            "token_total": int(row["token_total"] or 0),
        }

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
        row["token_consumed"] = row.get("token_consumed") or 0
        # result 序列化为 JSON 文本存储
        result = row.get("result")
        row["result"] = json.dumps(result, ensure_ascii=False) if result is not None else None
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(task_id, status, filename, user_id, created_at, updated_at, file_size, token_consumed, error, result) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, row["status"], row["filename"], row.get("user_id"),
                 row["created_at"], now, row["file_size"], row["token_consumed"],
                 row.get("error"), row["result"]),
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
