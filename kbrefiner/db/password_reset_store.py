"""密码重置令牌存储（SQLite）。

用于"忘记密码"流程：
1. create(user_id) 生成随机 token 并记录过期时间（默认 24h）。
2. verify(token) 校验令牌（存在 / 未过期 / 未使用），返回 user_id 等信息。
3. consume(token) 标记已使用，一次性消费。

线程安全：check_same_thread=False + threading.Lock。
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


class PasswordResetStore:
    """SQLite 密码重置令牌存储。"""

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
                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL
                )
            """)
            self._conn.commit()

    def create(
        self,
        user_id: str,
        *,
        ttl_seconds: int = 86400,
    ) -> str:
        """创建重置令牌。

        Args:
            user_id: 目标用户 ID。
            ttl_seconds: 有效期（秒），默认 24h。

        Returns:
            新生成的 token 字符串（secrets.token_urlsafe(32)）。
        """
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + float(ttl_seconds)
        with self._lock:
            self._conn.execute(
                "INSERT INTO password_reset_tokens "
                "(token, user_id, created_at, expires_at, used_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (token, user_id, now, expires_at),
            )
            self._conn.commit()
        return token

    def verify(self, token: str) -> Optional[dict[str, Any]]:
        """校验令牌。

        Returns:
            令牌合法 → 整行 dict（含 user_id / created_at / expires_at）。
            不存在 / 已过期 / 已使用 → 返回 None。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM password_reset_tokens WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        now = time.time()
        if row["expires_at"] < now:
            return None
        if row["used_at"] is not None:
            return None
        return dict(row)

    def consume(self, token: str) -> bool:
        """消费令牌（标记 used_at = now），只能消费一次。

        Returns:
            消费成功 → True；令牌不存在 / 已被消费 → False。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT used_at FROM password_reset_tokens WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None or row["used_at"] is not None:
                return False
            self._conn.execute(
                "UPDATE password_reset_tokens SET used_at = ? WHERE token = ?",
                (time.time(), token),
            )
            self._conn.commit()
        return True

    def close(self) -> None:
        self._conn.close()


__all__ = ["PasswordResetStore"]
