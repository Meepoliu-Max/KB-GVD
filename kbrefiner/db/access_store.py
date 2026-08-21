"""黑白名单访问控制存储（SQLite）。

表结构：
    access_rules(id TEXT PRIMARY KEY,
                 type TEXT,   -- ip_whitelist / domain_whitelist / user_blacklist
                 value TEXT,  -- IP / CIDR / 域名 / 用户邮箱
                 note TEXT, created_at REAL, created_by TEXT)

开关（是否启用校验）存 SettingsStore：
    access_ip_whitelist_enabled / access_domain_whitelist_enabled

执法优先级（PRD 9.6.4）：用户黑名单 > IP 白名单 > 邮箱域名白名单。
"""
from __future__ import annotations

import ipaddress
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_RULE_TYPES = ("ip_whitelist", "domain_whitelist", "user_blacklist")


class AccessStore:
    """SQLite 访问控制规则存储。"""

    RULE_TYPES = _RULE_TYPES

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
                CREATE TABLE IF NOT EXISTS access_rules (
                    id TEXT PRIMARY KEY,
                    type TEXT,
                    value TEXT,
                    note TEXT,
                    created_at REAL,
                    created_by TEXT,
                    UNIQUE(type, value)
                )
            """)
            self._conn.commit()

    # ===== 写操作 =====

    def create(self, rule_type: str, value: str, note: str = "",
               created_by: str = "") -> dict[str, Any]:
        """添加规则。类型非法抛 ValueError，重复抛 ValueError。"""
        if rule_type not in _RULE_TYPES:
            raise ValueError(f"非法规则类型: {rule_type}")
        value = value.strip()
        self.validate_value(rule_type, value)

        rid = secrets.token_hex(6)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO access_rules (id, type, value, note, created_at, created_by) "
                    "VALUES (?,?,?,?,?,?)",
                    (rid, rule_type, value, note, time.time(), created_by),
                )
                self._conn.commit()
        except sqlite3.IntegrityError as e:
            raise ValueError(f"规则已存在: {value}") from e
        return self.get(rid)  # type: ignore[return-value]

    @staticmethod
    def validate_value(rule_type: str, value: str) -> None:
        """校验规则取值格式，非法抛 ValueError。"""
        if rule_type == "ip_whitelist":
            try:
                ipaddress.ip_network(value, strict=False)
            except ValueError as e:
                raise ValueError(
                    f"非法 IP 或 CIDR: {value}（示例 192.168.1.5 或 10.0.0.0/24）"
                ) from e
        elif rule_type == "domain_whitelist":
            v = value.lstrip("@").lower()
            if not v or "." not in v or " " in v:
                raise ValueError(f"非法域名: {value}（示例 company.com）")
        elif rule_type == "user_blacklist":
            if "@" not in value:
                raise ValueError(f"黑名单需填用户邮箱: {value}")

    def delete(self, rule_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM access_rules WHERE id = ?", (rule_id,))
            self._conn.commit()

    # ===== 读操作 =====

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def get(self, rule_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM access_rules WHERE id = ?", (rule_id,)
            ).fetchone()
        return self._public(row) if row else None

    def list_rules(self, rule_type: str = "", search: str = "") -> list[dict[str, Any]]:
        """规则列表（按类型过滤，value/note 模糊搜索），按添加时间倒序。"""
        sql = "SELECT * FROM access_rules WHERE 1=1"
        params: list[Any] = []
        if rule_type:
            sql += " AND type = ?"; params.append(rule_type)
        if search:
            sql += " AND (value LIKE ? OR note LIKE ?)"
            kw = f"%{search}%"
            params.extend([kw, kw])
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._public(r) for r in rows]

    def values(self, rule_type: str) -> list[str]:
        """某类型的全部取值（执法用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT value FROM access_rules WHERE type = ?", (rule_type,)
            ).fetchall()
        return [r["value"] for r in rows]

    # ===== 执法判断 =====

    @staticmethod
    def ip_allowed(client_ip: str, rules: list[str]) -> bool:
        """客户端 IP 是否命中白名单（支持 IPv4/IPv6/CIDR）。

        回环地址（127.0.0.1 / ::1）始终放行：防止管理员把自己锁死
        （单机部署可经 ssh 隧道自救）。
        """
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            return True
        try:
            addr = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        for rule in rules:
            try:
                if addr in ipaddress.ip_network(rule, strict=False):
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def domain_allowed(email: str, domains: list[str]) -> bool:
        """邮箱域名是否命中白名单。"""
        if "@" not in email:
            return False
        domain = email.rsplit("@", 1)[1].strip().lower()
        allowed = {d.lstrip("@").strip().lower() for d in domains}
        return domain in allowed

    @staticmethod
    def is_blacklisted(email: str, blacklist: list[str]) -> bool:
        """邮箱是否在用户黑名单（大小写不敏感）。"""
        return email.strip().lower() in {b.strip().lower() for b in blacklist}

    def close(self) -> None:
        self._conn.close()


def client_ip(request) -> str:
    """提取客户端 IP：X-Forwarded-For 首个 > request.client.host。

    注意：X-Forwarded-For 可被伪造，仅当部署在可信反代之后才有意义。
    """
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


__all__ = ["AccessStore", "client_ip"]
