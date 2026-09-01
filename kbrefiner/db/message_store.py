"""系统消息存储（SQLite）。

管理后台「消息列表」的持久化层，配合用户端 /api/messages 展示。

表结构：
    messages(id TEXT PRIMARY KEY, title TEXT, content TEXT,
             type TEXT,        -- system / task / maintenance / security
             status TEXT,      -- sent / draft / scheduled / revoked
             target_users TEXT,-- JSON："all" 或 user_id 列表
             scheduled_at REAL, sent_at REAL, created_at REAL, created_by TEXT)

定时消息无后台调度器，采用惰性晋升：列表/用户端查询时把到点的
scheduled 统一翻转为 sent（对管理员视角无感知差异）。
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


_MESSAGE_TYPES = ("system", "task", "maintenance", "security")


class MessageStore:
    """SQLite 消息存储。"""

    TYPES = _MESSAGE_TYPES

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
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    content TEXT,
                    type TEXT,
                    status TEXT DEFAULT 'draft',
                    target_users TEXT DEFAULT '"all"',
                    scheduled_at REAL,
                    sent_at REAL,
                    created_at REAL,
                    created_by TEXT
                )
            """)
            self._conn.commit()

    # ===== 内部 =====

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        # target_users 反序列化
        try:
            import json
            d["target_users"] = json.loads(d.get("target_users") or '"all"')
        except (ValueError, TypeError):
            d["target_users"] = "all"
        return d

    def _promote_due(self) -> None:
        """把到点的定时消息翻转为已发送（惰性调度）。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET status = 'sent', sent_at = ? "
                "WHERE status = 'scheduled' AND scheduled_at IS NOT NULL "
                "AND scheduled_at <= ?",
                (now, now),
            )
            self._conn.commit()

    # ===== 写操作 =====

    def create(
        self,
        title: str,
        content: str,
        msg_type: str = "system",
        status: str = "draft",
        target_users: Any = "all",
        scheduled_at: Optional[float] = None,
        created_by: str = "",
    ) -> dict[str, Any]:
        """创建消息。status=sent 时立即记录 sent_at。"""
        import json

        if msg_type not in _MESSAGE_TYPES:
            raise ValueError(f"非法消息类型: {msg_type}")
        if status not in ("sent", "draft", "scheduled"):
            raise ValueError(f"非法消息状态: {status}")
        if status == "scheduled" and not scheduled_at:
            raise ValueError("定时消息必须提供 scheduled_at")

        now = time.time()
        mid = secrets.token_hex(6)
        if not isinstance(target_users, str):
            target_users = json.dumps(list(target_users), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (id, title, content, type, status, target_users, "
                "scheduled_at, sent_at, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mid, title, content, msg_type, status, target_users,
                 scheduled_at, now if status == "sent" else None, now, created_by),
            )
            self._conn.commit()
        return self.get(mid)  # type: ignore[return-value]

    def update(
        self, message_id: str, title: Optional[str] = None,
        content: Optional[str] = None, msg_type: Optional[str] = None,
        target_users: Any = None,
    ) -> None:
        """编辑消息（仅草稿可改，调用方负责校验）。"""
        import json

        sets, params = [], []
        if title is not None:
            sets.append("title = ?"); params.append(title)
        if content is not None:
            sets.append("content = ?"); params.append(content)
        if msg_type is not None:
            sets.append("type = ?"); params.append(msg_type)
        if target_users is not None:
            tv = target_users if isinstance(target_users, str) else json.dumps(
                list(target_users), ensure_ascii=False)
            sets.append("target_users = ?"); params.append(tv)
        if not sets:
            return
        params.append(message_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", params
            )
            self._conn.commit()

    def set_status(self, message_id: str, status: str,
                   *, clear_scheduled: bool = False) -> None:
        """状态流转：发送 / 撤回 / 取消定时。"""
        now = time.time()
        with self._lock:
            if status == "sent":
                self._conn.execute(
                    "UPDATE messages SET status = 'sent', sent_at = ? WHERE id = ?",
                    (now, message_id),
                )
            elif status == "revoked":
                self._conn.execute(
                    "UPDATE messages SET status = 'revoked' WHERE id = ?", (message_id,)
                )
            elif status == "draft":
                # 取消定时转草稿：清空计划时间与已发送时间
                self._conn.execute(
                    "UPDATE messages SET status = 'draft', scheduled_at = NULL, "
                    "sent_at = NULL WHERE id = ?",
                    (message_id,),
                )
            else:
                raise ValueError(f"不支持的目标状态: {status}")
            _ = clear_scheduled
            self._conn.commit()

    def delete(self, message_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            self._conn.commit()

    # ===== 读操作 =====

    def get(self, message_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._public(row) if row else None

    def list_all(
        self, msg_type: str = "", status: str = "", search: str = "",
    ) -> list[dict[str, Any]]:
        """管理视角全量列表（含草稿/已撤回），按创建时间倒序。"""
        self._promote_due()
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list[Any] = []
        if msg_type:
            sql += " AND type = ?"; params.append(msg_type)
        if status:
            sql += " AND status = ?"; params.append(status)
        if search:
            sql += " AND title LIKE ?"; params.append(f"%{search}%")
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._public(r) for r in rows]

    def list_for_user(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """用户视角：已发送且面向全体或点名该用户的消息，最新在前。

        已撤回（revoked）不展示（PRD 9.5.4 撤回语义）。
        """
        import json

        self._promote_due()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE status = 'sent' "
                "ORDER BY sent_at DESC LIMIT 200"
            ).fetchall()
        result = []
        for r in rows:
            m = self._public(r)
            targets = m["target_users"]
            hit = targets == "all" or (
                isinstance(targets, list) and user_id in targets
            )
            if hit:
                result.append(m)
            if len(result) >= limit:
                break
        return result

    def close(self) -> None:
        self._conn.close()


__all__ = ["MessageStore"]
