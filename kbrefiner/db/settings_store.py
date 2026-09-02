"""系统设置存储（SQLite key-value）。

管理后台「系统设置」页的持久化层，同时作为运行时动态配置源：
- .env 静态配置（kbrefiner.config.Settings）仍是默认值来源
- 本表中的覆盖值优先于 .env，改动即时生效（路径类除外，重启生效）

值的 JSON 编码存储，支持 str / int / bool / list。
llm_api_key 等敏感值加密存储（HMAC-SHA256 密钥流混淆，防明文落库，
非 AES，防的是数据库文件直接泄露场景）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class SettingsStore:
    """SQLite 系统设置存储。"""

    # 全量默认值（未落库时生效）。与 .env 的关系见模块 docstring。
    DEFAULTS: dict[str, Any] = {
        # ===== 用户配额 =====
        "quota_task_daily": 100,        # 单用户每日任务数上限
        "quota_token_daily": 1_000_000,  # 单用户每日 Token 消耗上限
        "quota_upload_daily": 500,      # 单用户每日上传文档数上限
        "quota_file_size_mb": 50,       # 单文件大小上限（MB）
        "quota_batch_count": 20,        # 单次批量上传文件数上限（预留）
        # ===== 文件与格式 =====
        # 允许的文件类型（不含点，小写）。doc 旧格式解析器不支持，不开放
        "allowed_file_types": ["pdf", "docx", "md", "txt"],
        "retain_file_days": 30,         # 上传原始文件保留天数
        "retain_result_days": 90,       # 处理结果保留天数
        # ===== 通知设置（v1 仅存储开关，推送渠道后续迭代） =====
        "notify_task_done": True,
        "notify_task_failed": True,
        "notify_report": False,
        # ===== LLM API =====
        "llm_provider": "deepseek",
        "llm_api_key": "",              # 加密存储
        "llm_base_url": "",
        "llm_model": "",
        # ===== 前台访问控制 =====
        # None 表示跟随 .env REQUIRE_LOGIN；true/false 为后台显式覆盖
        "access_require_login": None,
        "access_allow_register": True,
        # ===== 访问控制开关（黑白名单页） =====
        "access_ip_whitelist_enabled": False,
        "access_domain_whitelist_enabled": False,
        # ===== 安全与审计 =====
        "login_fail_limit": 5,          # 连续失败 N 次锁定
        "login_lock_minutes": 30,       # 锁定时长（分钟）
        "session_timeout_minutes": 30,  # v1 仅存储（令牌有效期由 .env 控制）
        "audit_log_enabled": True,      # v1 仅存储（审计落库后续迭代）
        # ===== 存储路径 =====
        "tmp_dir": "",                  # 临时文件目录（上传后解析用）
        "disk_max_gb": 500,             # 最大磁盘占用 (GB)（PRD §9.7.6）
        "disk_warn_percent": 80,        # 磁盘告警阈值 (%)
    }

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
                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL,
                    updated_by TEXT
                )
            """)
            self._conn.commit()

    # ===== 读写 =====

    def get(self, key: str, default: Any = None) -> Any:
        """读取配置：落库值 > default 参数 > DEFAULTS。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM system_settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return self.DEFAULTS.get(key, default) if default is None else default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set(self, key: str, value: Any, updated_by: str = "") -> None:
        """写入配置（JSON 编码）。值为 None 时删除覆盖（回到默认）。"""
        now = time.time()
        with self._lock:
            if value is None:
                self._conn.execute(
                    "DELETE FROM system_settings WHERE key = ?", (key,)
                )
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO system_settings (key, value, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), now, updated_by),
                )
            self._conn.commit()

    def get_all(self) -> dict[str, Any]:
        """全量配置（落库值覆盖默认值合并），敏感值明文（API 层负责脱敏）。"""
        result = dict(self.DEFAULTS)
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM system_settings"
            ).fetchall()
        for r in rows:
            try:
                result[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                continue
        return result

    def meta(self, key: str) -> dict[str, Any] | None:
        """读取某配置的更新元信息（API 展示"最后修改"用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT updated_at, updated_by FROM system_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self._conn.close()


# 敏感值加密标记前缀
_ENC_PREFIX = "enc$"


def encrypt_value(text: str) -> str:
    """加密敏感配置（HMAC-SHA256 密钥流 + 随机 nonce）。

    非 AES，防数据库文件明文泄露；密钥来自 auth 模块的签名密钥。
    """
    import base64
    import hashlib
    import hmac
    import secrets

    from kbrefiner.auth import _get_secret

    nonce = secrets.token_bytes(8)
    data = text.encode("utf-8")

    def keystream(length: int) -> bytes:
        out = b""
        counter = 0
        while len(out) < length:
            out += hmac.new(
                _get_secret(), nonce + counter.to_bytes(4, "big"), hashlib.sha256
            ).digest()
            counter += 1
        return out[:length]

    enc = bytes(a ^ b for a, b in zip(data, keystream(len(data))))
    return _ENC_PREFIX + base64.b64encode(nonce + enc).decode("ascii")


def decrypt_value(stored: str) -> str:
    """解密 encrypt_value 的产物；非加密格式（历史明文）原样返回。"""
    import base64
    import hashlib
    import hmac

    from kbrefiner.auth import _get_secret

    if not stored or not stored.startswith(_ENC_PREFIX):
        return stored
    try:
        raw = base64.b64decode(stored[len(_ENC_PREFIX):])
        nonce, enc = raw[:8], raw[8:]

        def keystream(length: int) -> bytes:
            out = b""
            counter = 0
            while len(out) < length:
                out += hmac.new(
                    _get_secret(), nonce + counter.to_bytes(4, "big"), hashlib.sha256
                ).digest()
                counter += 1
            return out[:length]

        return bytes(a ^ b for a, b in zip(enc, keystream(len(enc)))).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


__all__ = ["SettingsStore", "encrypt_value", "decrypt_value"]
