"""审计日志存储（SQLite）。

记录管理员和用户的关键操作：登录、用户 CRUD、设置修改、消息、访问规则、任务等。
每条日志包含：操作人 user_id、动作 action、目标对象类型/ID、详情、IP、UA、时间戳。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


class AuditStore:
    """SQLite 审计日志存储。"""

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
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    action TEXT NOT NULL,
                    target_type TEXT,
                    target_id TEXT,
                    detail TEXT,
                    ip TEXT,
                    user_agent TEXT,
                    created_at REAL NOT NULL
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action)"
            )
            self._conn.commit()

    def record(
        self,
        *,
        user: Optional[dict] = None,
        action: str,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        detail: Optional[str] = None,
        request: Optional[Any] = None,
    ) -> None:
        """写入一条审计日志（best-effort，异常静默）。"""
        try:
            user_id = user["id"] if user else None
            ip = ""
            ua = ""
            if request is not None:
                try:
                    # FastAPI Request
                    xff = request.headers.get("X-Forwarded-For", "")
                    if xff:
                        ip = xff.split(",")[0].strip()
                    else:
                        ip = getattr(request.client, "host", "") if request.client else ""
                    ua = request.headers.get("User-Agent", "") or ""
                except Exception:
                    pass
            with self._lock:
                self._conn.execute(
                    "INSERT INTO audit_logs "
                    "(user_id, action, target_type, target_id, detail, ip, user_agent, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        action,
                        target_type,
                        str(target_id) if target_id is not None else None,
                        str(detail) if detail is not None else None,
                        ip,
                        ua,
                        time.time(),
                    ),
                )
                self._conn.commit()
        except Exception:
            # 日志失败不影响主流程
            pass

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        since: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """查询审计日志（按 created_at desc）。"""
        sql = "SELECT * FROM audit_logs WHERE 1=1"
        params: list[Any] = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if action:
            sql += " AND action = ?"
            params.append(action)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count(
        self,
        *,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        since: Optional[float] = None,
    ) -> int:
        """统计符合条件的日志总数。"""
        sql = "SELECT COUNT(*) AS n FROM audit_logs WHERE 1=1"
        params: list[Any] = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if action:
            sql += " AND action = ?"
            params.append(action)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since)
        with self._lock:
            (n,) = self._conn.execute(sql, params).fetchone()
        return int(n or 0)

    def close(self) -> None:
        self._conn.close()


__all__ = ["AuditStore"]
