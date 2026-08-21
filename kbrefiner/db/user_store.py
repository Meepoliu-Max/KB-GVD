"""用户存储（SQLite）。

管理员/普通用户统一表，配合 kbrefiner.auth 的密码哈希与令牌使用。
沿用 TaskStore 的线程安全模式（check_same_thread=False + 锁）。

表结构：
    users(id TEXT PRIMARY KEY,
          username TEXT, email TEXT UNIQUE,
          password_hash TEXT,
          role TEXT DEFAULT 'user',        -- super_admin / admin / user
          status TEXT DEFAULT 'active',    -- active / disabled
          created_at REAL, last_active_at REAL)

用法：
    from kbrefiner.db.user_store import UserStore
    store = UserStore("./data/tasks.db")   # 与任务库同库不同表
    store.create("admin@x.com", "admin", "密码", role="super_admin")
    user = store.verify_login("admin@x.com", "密码")
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


class UserStore:
    """SQLite 用户存储。"""

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
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT,
                    email TEXT UNIQUE,
                    password_hash TEXT,
                    role TEXT DEFAULT 'user',
                    status TEXT DEFAULT 'active',
                    created_at REAL,
                    last_active_at REAL
                )
            """)
            self._conn.commit()

    # ===== 写操作 =====

    def create(
        self,
        email: str,
        username: str,
        password: str,
        role: str = "user",
        status: str = "active",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """创建用户，邮箱重复抛 ValueError。"""
        from kbrefiner.auth import hash_password

        now = time.time()
        uid = user_id or secrets.token_hex(6)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO users (id, username, email, password_hash, role, status, created_at, last_active_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (uid, username, email.strip().lower(), hash_password(password), role, status, now, now),
                )
                self._conn.commit()
        except sqlite3.IntegrityError as e:
            raise ValueError(f"邮箱已存在: {email}") from e
        return self.get(uid)  # type: ignore[return-value]

    def update_status(self, user_id: str, status: str) -> None:
        """启用/禁用用户。"""
        with self._lock:
            self._conn.execute(
                "UPDATE users SET status = ? WHERE id = ?", (status, user_id)
            )
            self._conn.commit()

    def update_password(self, user_id: str, new_password: str) -> None:
        """重置密码。"""
        from kbrefiner.auth import hash_password

        with self._lock:
            self._conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(new_password), user_id),
            )
            self._conn.commit()

    def update_role(self, user_id: str, role: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET role = ? WHERE id = ?", (role, user_id)
            )
            self._conn.commit()

    def update_profile(self, user_id: str, username: str | None = None, email: str | None = None) -> None:
        """更新用户名/邮箱。"""
        sets, params = [], []
        if username is not None:
            sets.append("username = ?")
            params.append(username)
        if email is not None:
            sets.append("email = ?")
            params.append(email.strip().lower())
        if not sets:
            return
        params.append(user_id)
        with self._lock:
            self._conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
            self._conn.commit()

    def touch_last_active(self, user_id: str) -> None:
        """刷新最后活跃时间（登录/请求时调用）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE users SET last_active_at = ? WHERE id = ?",
                (time.time(), user_id),
            )
            self._conn.commit()

    def delete(self, user_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._conn.commit()

    # ===== 读操作 =====

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        """转公开 dict（绝不含 password_hash）。"""
        return {
            "id": row["id"],
            "username": row["username"],
            "email": row["email"],
            "role": row["role"],
            "status": row["status"],
            "created_at": row["created_at"],
            "last_active_at": row["last_active_at"],
        }

    def get(self, user_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._public(row) if row else None

    def get_by_email(self, email: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
            ).fetchone()
        return self._public(row) if row else None

    def _get_hash_by_email(self, email: str) -> Optional[tuple[str, str, str]]:
        """内部：按邮箱取 (id, password_hash, status)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, password_hash, status FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        return (row["id"], row["password_hash"], row["status"]) if row else None

    def verify_login(self, email: str, password: str) -> Optional[dict[str, Any]]:
        """校验登录。成功返回用户（含 id/role），失败或被禁用返回 None。"""
        from kbrefiner.auth import verify_password

        info = self._get_hash_by_email(email)
        if not info:
            return None
        uid, pwd_hash, status = info
        if not verify_password(password, pwd_hash):
            return None
        if status != "active":
            return None
        self.touch_last_active(uid)
        return self.get(uid)

    def list_all(
        self,
        search: str = "",
        role: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        """列表（按创建时间倒序），支持用户名/邮箱模糊搜索与角色/状态过滤。"""
        sql = "SELECT * FROM users WHERE 1=1"
        params: list[Any] = []
        if search:
            sql += " AND (username LIKE ? OR email LIKE ?)"
            kw = f"%{search}%"
            params.extend([kw, kw])
        if role:
            sql += " AND role = ?"
            params.append(role)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._public(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            (n,) = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(n)

    def close(self) -> None:
        self._conn.close()


__all__ = ["UserStore"]
