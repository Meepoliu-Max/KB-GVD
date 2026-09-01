"""认证与授权：密码哈希（stdlib pbkdf2）+ HMAC 签名令牌 + FastAPI 依赖。

零第三方依赖（Python 3.9+ stdlib），令牌格式类 JWT：
    base64url(payload).base64url(hmac_sha256(payload, secret))

payload: {"sub": "<user_id>", "role": "<role>", "exp": <unix 秒>}

签名密钥来源（优先级）：AUTH_SECRET 环境变量 > 自动生成并持久化到
data/.auth_secret（重启不变，令牌不失效）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security.utils import get_authorization_scheme_param

# Cookie / Header 中的令牌键名
TOKEN_COOKIE = "kb_token"

# 默认令牌有效期（小时）；可在 .env 用 AUTH_TOKEN_EXPIRE_HOURS 覆盖
DEFAULT_TOKEN_EXPIRE_HOURS = 24 * 7

_PBKDF2_ITERATIONS = 60_000


# =====================================================================
# 密码哈希（pbkdf2-hmac-sha256，Django 同款算法，格式可自描述升级）
# =====================================================================

def hash_password(password: str) -> str:
    """生成 pbkdf2 哈希，格式：pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "$".join([
        "pbkdf2_sha256",
        str(_PBKDF2_ITERATIONS),
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    ])


def verify_password(password: str, stored: str) -> bool:
    """校验密码与存储哈希。格式不符（历史遗留/手工填错）返回 False。"""
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            base64.b64decode(salt_b64), int(iters),
        )
        return hmac.compare_digest(digest, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


# =====================================================================
# HMAC 签名令牌
# =====================================================================

_secret_cache: Optional[bytes] = None


def _get_secret() -> bytes:
    """签名密钥：AUTH_SECRET 配置 > data/.auth_secret（首次自动生成）。"""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    import os

    from kbrefiner.config import get_settings

    env_secret = os.environ.get("AUTH_SECRET", "") or get_settings().auth_secret
    if env_secret:
        _secret_cache = env_secret.encode("utf-8")
        return _secret_cache

    secret_path = Path(os.environ.get("AUTH_SECRET_FILE", "./data/.auth_secret"))
    try:
        if secret_path.exists():
            _secret_cache = secret_path.read_bytes().strip()
        else:
            _secret_cache = secrets.token_hex(32).encode("utf-8")
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret_path.write_bytes(_secret_cache)
            try:  # 限制权限（POSIX）
                secret_path.chmod(0o600)
            except OSError:
                pass
    except OSError:
        # 只读环境（如容器临时目录）：退化为进程内随机（重启令牌失效）
        _secret_cache = secrets.token_hex(32).encode("utf-8")
    return _secret_cache


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def create_token(user_id: str, role: str, expire_hours: int | None = None) -> str:
    """签发令牌。"""
    from kbrefiner.config import get_settings

    hours = expire_hours
    if hours is None:
        hours = getattr(get_settings(), "auth_token_expire_hours", DEFAULT_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": user_id,
        "role": role,
        "exp": int(time.time()) + int(hours * 3600),
    }
    body = _b64url(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sig = _b64url(hmac.new(_get_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_token(token: str) -> Optional[dict[str, Any]]:
    """校验并解析令牌。签名不符/过期返回 None。"""
    try:
        body, sig = token.split(".")
        expected = hmac.new(_get_secret(), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expected), sig):
            return None
        payload = json.loads(_b64url_decode(body))
        if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


# =====================================================================
# FastAPI 依赖
# =====================================================================

def _extract_token(request: Request) -> Optional[str]:
    """从 Authorization Bearer 或 Cookie 提取令牌（浏览器页面走 Cookie）。"""
    authz = request.headers.get("Authorization", "")
    if authz:
        scheme, param = get_authorization_scheme_param(authz)
        if scheme.lower() == "bearer" and param:
            return param
    return request.cookies.get(TOKEN_COOKIE)


def make_user_dependency(user_store, *, require_login: bool):
    """构造解析当前用户的依赖函数（注入 UserStore 与登录开关）。

    Returns:
        current_user(request) -> Optional[dict]：未登录时，强制登录模式抛 401，
        开放模式（require_login=False）返回 None（匿名可用）。
    """

    def current_user(request: Request) -> Optional[dict[str, Any]]:
        token = _extract_token(request)
        if not token:
            if require_login:
                raise HTTPException(status_code=401, detail="未登录")
            return None
        payload = decode_token(token)
        if not payload:
            if require_login:
                raise HTTPException(status_code=401, detail="登录已过期")
            return None
        user = user_store.get(payload.get("sub", ""))
        if not user or user.get("status") != "active":
            raise HTTPException(status_code=401, detail="账号不可用")
        return user

    return current_user


def make_admin_dependency(current_user_dep, *, allow_roles=("super_admin", "admin")):
    """构造管理员守卫依赖：未登录 401、已登录但非管理员 403。"""

    def require_admin(user: dict = Depends(current_user_dep)) -> dict:
        if user is None:
            raise HTTPException(status_code=401, detail="未登录")
        if user.get("role") not in allow_roles:
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return user

    return require_admin


def _reset_secret_cache() -> None:
    """清空密钥缓存（测试切换环境用）。"""
    global _secret_cache
    _secret_cache = None


__all__ = [
    "TOKEN_COOKIE",
    "DEFAULT_TOKEN_EXPIRE_HOURS",
    "hash_password",
    "verify_password",
    "create_token",
    "decode_token",
    "make_user_dependency",
    "make_admin_dependency",
]
