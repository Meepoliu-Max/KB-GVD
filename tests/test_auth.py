"""认证与管理后台测试。

覆盖：
- kbrefiner.auth：密码哈希、令牌签发/校验/过期/伪造
- UserStore：创建、登录校验、状态/密码/角色更新、筛选
- /api/auth/*：登录（Cookie）、登出、当前用户
- /api/admin/*：权限守卫（401/403）、用户 CRUD、保护规则、统计
- /api/tasks：用户数据隔离

运行：python -m pytest tests/test_auth.py -v
"""
from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from kbrefiner import auth as auth_mod
from kbrefiner.api import deps, routes
from kbrefiner.config import Settings, get_settings
from kbrefiner.db import AuditStore, PasswordResetStore, SettingsStore, TaskStore, UserStore
from kbrefiner.main import app

# 真实存储（测试结束后恢复）
_original_task_store = routes._task_store
_original_user_store = deps._user_store
_original_deps_task_store = deps._task_store
_original_settings_store = deps._settings_store
_original_audit_store = deps._audit_store
_original_password_reset_store = deps._password_reset_store


class _StoreBundle:
    """临时目录内的任务库 + 用户库 + 设置库（同一 SQLite 文件）。"""

    def __init__(self, temp_dir: str):
        db = str(Path(temp_dir) / "tasks.db")
        self.task_store = TaskStore(db)
        self.user_store = UserStore(db)
        self.settings_store = SettingsStore(db)
        self.audit_store = AuditStore(db)
        self.password_reset_store = PasswordResetStore(db)

    def install(self) -> None:
        routes._task_store = self.task_store
        deps._task_store = self.task_store
        deps._user_store = self.user_store
        # 隔离运行时设置：否则真实库的 DB 覆盖（如 access_require_login）
        # 会压过测试注入的 .env 值
        deps._settings_store = self.settings_store
        deps._audit_store = self.audit_store
        deps._password_reset_store = self.password_reset_store

    def restore(self) -> None:
        routes._task_store = _original_task_store
        deps._task_store = _original_deps_task_store
        deps._user_store = _original_user_store
        deps._settings_store = _original_settings_store
        deps._audit_store = _original_audit_store
        deps._password_reset_store = _original_password_reset_store
        self.task_store.close()
        self.user_store.close()
        self.settings_store.close()
        self.audit_store.close()
        self.password_reset_store.close()


def _make_client(*, require_login: bool = False, temp_dir: str = ".") -> TestClient:
    """创建测试客户端：临时存储 + 隔离配置。"""
    settings = Settings(
        _env_file=None,
        upload_dir=str(Path(temp_dir) / "uploads"),
        output_dir=str(Path(temp_dir) / "outputs"),
        llm_api_key="test-key",
        require_login=require_login,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


# =====================================================================
# auth 模块单元测试
# =====================================================================

class TestAuthModule(unittest.TestCase):

    def test_password_hash_roundtrip(self):
        stored = auth_mod.hash_password("s3cret!")
        self.assertNotIn("s3cret!", stored)
        self.assertTrue(auth_mod.verify_password("s3cret!", stored))
        # 每次加盐不同
        self.assertNotEqual(stored, auth_mod.hash_password("s3cret!"))

    def test_verify_password_failures(self):
        stored = auth_mod.hash_password("s3cret!")
        self.assertFalse(auth_mod.verify_password("wrong", stored))
        self.assertFalse(auth_mod.verify_password("s3cret!", "not-a-valid-hash"))
        self.assertFalse(auth_mod.verify_password("s3cret!", ""))

    def test_token_roundtrip(self):
        token = auth_mod.create_token("u1", "admin", expire_hours=1)
        payload = auth_mod.decode_token(token)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["sub"], "u1")
        self.assertEqual(payload["role"], "admin")

    def test_token_expired(self):
        token = auth_mod.create_token("u1", "admin", expire_hours=-1)
        self.assertIsNone(auth_mod.decode_token(token))

    def test_token_tampered(self):
        token = auth_mod.create_token("u1", "admin", expire_hours=1)
        body, _sig = token.split(".")
        self.assertIsNone(auth_mod.decode_token(f"{body}.forgedsig"))
        self.assertIsNone(auth_mod.decode_token("garbage"))


# =====================================================================
# UserStore 单元测试
# =====================================================================

class TestUserStore(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = UserStore(str(Path(self.temp.name) / "users.db"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_create_and_get(self):
        user = self.store.create("A@x.com", "alice", "pass123")
        self.assertEqual(user["email"], "a@x.com")  # 邮箱归一化小写
        self.assertEqual(user["role"], "user")
        self.assertNotIn("password_hash", user)
        self.assertEqual(self.store.get(user["id"])["username"], "alice")

    def test_duplicate_email(self):
        self.store.create("a@x.com", "alice", "pass123")
        with self.assertRaises(ValueError):
            self.store.create("a@x.com", "bob", "pass456")

    def test_verify_login(self):
        self.store.create("a@x.com", "alice", "pass123")
        self.assertIsNotNone(self.store.verify_login("A@X.COM", "pass123"))  # 大小写不敏感
        self.assertIsNone(self.store.verify_login("a@x.com", "wrong"))
        self.assertIsNone(self.store.verify_login("nobody@x.com", "pass123"))

    def test_verify_login_disabled(self):
        u = self.store.create("a@x.com", "alice", "pass123")
        self.store.update_status(u["id"], "disabled")
        self.assertIsNone(self.store.verify_login("a@x.com", "pass123"))

    def test_update_password(self):
        u = self.store.create("a@x.com", "alice", "old1234")
        self.store.update_password(u["id"], "new1234")
        self.assertIsNone(self.store.verify_login("a@x.com", "old1234"))
        self.assertIsNotNone(self.store.verify_login("a@x.com", "new1234"))

    def test_list_all_filters(self):
        self.store.create("a@x.com", "alice", "pass123", role="user")
        self.store.create("b@x.com", "bob", "pass123", role="admin")
        self.store.create("c@x.com", "carol", "pass123", role="user", status="disabled")

        self.assertEqual(len(self.store.list_all()), 3)
        self.assertEqual(len(self.store.list_all(role="user")), 2)
        self.assertEqual(len(self.store.list_all(status="disabled")), 1)
        # 模糊搜索（用户名或邮箱）
        self.assertEqual(len(self.store.list_all(search="bob")), 1)
        self.assertEqual(len(self.store.list_all(search="x.com")), 3)


# =====================================================================
# 认证 API + 管理 API 测试（共用临时存储）
# =====================================================================

class _AuthApiTestBase(unittest.TestCase):
    """认证/管理 API 测试基类：隔离存储与签名密钥。"""

    @classmethod
    def setUpClass(cls):
        cls._orig_secret = os.environ.get("AUTH_SECRET")
        os.environ["AUTH_SECRET"] = "unit-test-secret"
        auth_mod._reset_secret_cache()

    @classmethod
    def tearDownClass(cls):
        if cls._orig_secret is None:
            os.environ.pop("AUTH_SECRET", None)
        else:
            os.environ["AUTH_SECRET"] = cls._orig_secret
        auth_mod._reset_secret_cache()

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.bundle = _StoreBundle(self.temp.name)
        self.bundle.install()
        # 预置三种角色账号
        self.users = {
            "super": self.bundle.user_store.create(
                "super@x.com", "超管", "super123", role="super_admin"),
            "admin": self.bundle.user_store.create(
                "admin@x.com", "管理员", "admin123", role="admin"),
            "user": self.bundle.user_store.create(
                "user@x.com", "普通用户", "user123", role="user"),
        }

    def tearDown(self):
        app.dependency_overrides.clear()
        self.bundle.restore()
        self.temp.cleanup()

    # ===== 工具 =====

    def _login_token(self, email: str, password: str) -> str:
        client = _make_client(temp_dir=self.temp.name)
        resp = client.post("/api/auth/login", json={"email": email, "password": password})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["token"]

    def _client_as(self, role: str) -> TestClient:
        """以指定角色登录的客户端（Cookie 认证）。"""
        creds = {
            "super": ("super@x.com", "super123"),
            "admin": ("admin@x.com", "admin123"),
            "user": ("user@x.com", "user123"),
        }
        email, password = creds[role]
        client = _make_client(temp_dir=self.temp.name)
        resp = client.post("/api/auth/login", json={"email": email, "password": password})
        self.assertEqual(resp.status_code, 200, resp.text)
        return client


class TestAuthApi(_AuthApiTestBase):

    def test_login_success_sets_cookie(self):
        client = _make_client(temp_dir=self.temp.name)
        resp = client.post("/api/auth/login", json={"email": "user@x.com", "password": "user123"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["user"]["email"], "user@x.com")
        self.assertNotIn("password_hash", data["user"])
        self.assertIn(auth_mod.TOKEN_COOKIE, resp.cookies)
        # 返回的 token 可直接解码
        payload = auth_mod.decode_token(data["token"])
        self.assertEqual(payload["sub"], data["user"]["id"])

    def test_login_wrong_password(self):
        client = _make_client(temp_dir=self.temp.name)
        resp = client.post("/api/auth/login", json={"email": "user@x.com", "password": "bad"})
        self.assertEqual(resp.status_code, 401)

    def test_login_disabled_account(self):
        self.bundle.user_store.update_status(self.users["user"]["id"], "disabled")
        client = _make_client(temp_dir=self.temp.name)
        resp = client.post("/api/auth/login", json={"email": "user@x.com", "password": "user123"})
        self.assertEqual(resp.status_code, 401)

    def test_me_anonymous_open_mode(self):
        client = _make_client(temp_dir=self.temp.name)  # require_login=False
        resp = client.get("/api/auth/me")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["user"])

    def test_me_logged_in(self):
        client = self._client_as("user")
        resp = client.get("/api/auth/me")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user"]["email"], "user@x.com")

    def test_logout_clears_cookie(self):
        client = self._client_as("user")
        resp = client.post("/api/auth/logout")
        self.assertEqual(resp.status_code, 200)
        # 登出后 me 回到匿名
        resp = client.get("/api/auth/me")
        self.assertIsNone(resp.json()["user"])

    def test_invalid_token_treated_as_anonymous(self):
        client = _make_client(temp_dir=self.temp.name)
        client.cookies.set(auth_mod.TOKEN_COOKIE, "bad.token")
        resp = client.get("/api/auth/me")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["user"])


class TestAdminGuard(_AuthApiTestBase):

    def test_admin_api_requires_login(self):
        client = _make_client(temp_dir=self.temp.name)
        for url in ("/api/admin/stats", "/api/admin/users", "/api/admin/tasks"):
            resp = client.get(url)
            self.assertEqual(resp.status_code, 401, url)

    def test_admin_api_forbidden_for_normal_user(self):
        client = self._client_as("user")
        for url in ("/api/admin/stats", "/api/admin/users", "/api/admin/tasks"):
            resp = client.get(url)
            self.assertEqual(resp.status_code, 403, url)

    def test_admin_api_ok_for_admin_and_super(self):
        for role in ("admin", "super"):
            client = self._client_as(role)
            resp = client.get("/api/admin/stats")
            self.assertEqual(resp.status_code, 200, role)


class TestAdminUsersApi(_AuthApiTestBase):

    def test_create_user(self):
        client = self._client_as("super")
        resp = client.post("/api/admin/users", json={
            "username": "newbie", "email": "new@x.com",
            "password": "pass123", "role": "user", "status": "active",
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["email"], "new@x.com")
        # 新用户可登录
        self.assertIsNotNone(self.bundle.user_store.verify_login("new@x.com", "pass123"))

    def test_create_user_duplicate_email_409(self):
        client = self._client_as("super")
        resp = client.post("/api/admin/users", json={
            "username": "dup", "email": "user@x.com", "password": "pass123",
        })
        self.assertEqual(resp.status_code, 409)

    def test_create_user_bad_role_400(self):
        client = self._client_as("super")
        resp = client.post("/api/admin/users", json={
            "username": "x", "email": "x@x.com", "password": "pass123", "role": "boss",
        })
        self.assertEqual(resp.status_code, 400)

    def test_admin_cannot_create_super_admin(self):
        client = self._client_as("admin")
        resp = client.post("/api/admin/users", json={
            "username": "s2", "email": "s2@x.com", "password": "pass123",
            "role": "super_admin",
        })
        self.assertEqual(resp.status_code, 403)

    def test_short_password_422(self):
        client = self._client_as("super")
        resp = client.post("/api/admin/users", json={
            "username": "x", "email": "x@x.com", "password": "123",
        })
        self.assertEqual(resp.status_code, 422)

    def test_list_users_hides_super_admin_from_admin(self):
        client = self._client_as("admin")
        resp = client.get("/api/admin/users")
        self.assertEqual(resp.status_code, 200)
        roles = {u["role"] for u in resp.json()["users"]}
        self.assertNotIn("super_admin", roles)
        self.assertIn("admin", roles)

    def test_list_users_super_sees_all(self):
        client = self._client_as("super")
        resp = client.get("/api/admin/users")
        roles = {u["role"] for u in resp.json()["users"]}
        self.assertIn("super_admin", roles)

    def test_list_users_with_stats(self):
        store = self.bundle.task_store
        uid = self.users["user"]["id"]
        store["t1"] = {"status": "completed", "filename": "a.pdf",
                       "user_id": uid, "file_size": 100}
        store.add_tokens("t1", 5000)
        store["t2"] = {"status": "failed", "filename": "b.pdf",
                       "user_id": uid, "file_size": 200}

        client = self._client_as("super")
        resp = client.get("/api/admin/users")
        users = {u["id"]: u for u in resp.json()["users"]}
        self.assertEqual(users[uid]["task_count"], 2)
        self.assertEqual(users[uid]["upload_count"], 2)
        self.assertEqual(users[uid]["token_consumed"], 5000)

    def test_list_users_search_and_filters(self):
        client = self._client_as("super")
        resp = client.get("/api/admin/users", params={"search": "user@"})
        self.assertEqual({u["email"] for u in resp.json()["users"]}, {"user@x.com"})
        resp = client.get("/api/admin/users", params={"role": "admin"})
        self.assertEqual(len(resp.json()["users"]), 1)

    def test_get_user_detail_with_recent_tasks(self):
        uid = self.users["user"]["id"]
        store = self.bundle.task_store
        store["t1"] = {"status": "completed", "filename": "a.pdf",
                       "user_id": uid, "created_at": time.time()}
        store.add_tokens("t1", 100)

        client = self._client_as("super")
        resp = client.get(f"/api/admin/users/{uid}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], uid)
        self.assertEqual(data["task_count"], 1)
        self.assertEqual(len(data["recent_tasks"]), 1)
        self.assertEqual(data["recent_tasks"][0]["token_consumed"], 100)

    def test_get_user_404(self):
        client = self._client_as("super")
        resp = client.get("/api/admin/users/nosuchid")
        self.assertEqual(resp.status_code, 404)

    def test_admin_cannot_view_super_admin(self):
        client = self._client_as("admin")
        sid = self.users["super"]["id"]
        resp = client.get(f"/api/admin/users/{sid}")
        self.assertEqual(resp.status_code, 403)

    def test_update_user_profile_and_role(self):
        client = self._client_as("super")
        uid = self.users["user"]["id"]
        resp = client.patch(f"/api/admin/users/{uid}", json={
            "username": "改名", "role": "admin",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["username"], "改名")
        self.assertEqual(resp.json()["role"], "admin")

    def test_update_user_email_conflict_409(self):
        client = self._client_as("super")
        uid = self.users["user"]["id"]
        resp = client.patch(f"/api/admin/users/{uid}", json={"email": "admin@x.com"})
        self.assertEqual(resp.status_code, 409)

    def test_disable_user(self):
        client = self._client_as("super")
        uid = self.users["user"]["id"]
        resp = client.patch(f"/api/admin/users/{uid}", json={"status": "disabled"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.bundle.user_store.verify_login("user@x.com", "user123"))

    def test_cannot_disable_last_super_admin(self):
        client = self._client_as("super")
        sid = self.users["super"]["id"]
        resp = client.patch(f"/api/admin/users/{sid}", json={"status": "disabled"})
        self.assertEqual(resp.status_code, 400)

    def test_cannot_demote_last_super_admin(self):
        client = self._client_as("super")
        sid = self.users["super"]["id"]
        resp = client.patch(f"/api/admin/users/{sid}", json={"role": "user"})
        self.assertEqual(resp.status_code, 400)

    def test_reset_password(self):
        client = self._client_as("super")
        uid = self.users["user"]["id"]
        resp = client.post(f"/api/admin/users/{uid}/reset-password",
                           json={"new_password": "brandnew9"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.bundle.user_store.verify_login("user@x.com", "user123"))
        self.assertIsNotNone(self.bundle.user_store.verify_login("user@x.com", "brandnew9"))

    def test_delete_user(self):
        client = self._client_as("super")
        uid = self.users["user"]["id"]
        resp = client.delete(f"/api/admin/users/{uid}")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.bundle.user_store.get(uid))

    def test_cannot_delete_self(self):
        client = self._client_as("super")
        sid = self.users["super"]["id"]
        resp = client.delete(f"/api/admin/users/{sid}")
        self.assertEqual(resp.status_code, 400)

    def test_admin_cannot_delete(self):
        client = self._client_as("admin")
        uid = self.users["user"]["id"]
        resp = client.delete(f"/api/admin/users/{uid}")
        self.assertEqual(resp.status_code, 403)  # 仅超级管理员可删除


class TestAdminStatsAndTasks(_AuthApiTestBase):

    def test_stats(self):
        store = self.bundle.task_store
        store["t1"] = {"status": "completed", "filename": "a.pdf",
                       "user_id": self.users["user"]["id"],
                       "created_at": time.time(), "file_size": 10}
        store.add_tokens("t1", 300)
        store["t2"] = {"status": "processing", "filename": "b.pdf",
                       "created_at": time.time() - 10 * 86400}

        client = self._client_as("super")
        resp = client.get("/api/admin/stats")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["users"]["total"], 3)
        self.assertEqual(data["users"]["active"], 3)
        self.assertEqual(data["tasks"]["total"], 2)
        self.assertEqual(data["tasks"]["today"], 1)
        self.assertEqual(data["tasks"]["completed"], 1)
        self.assertEqual(data["tasks"]["processing"], 1)
        self.assertEqual(data["tokens"]["total"], 300)

    def test_admin_tasks_list(self):
        store = self.bundle.task_store
        store["t1"] = {"status": "completed", "filename": "a.pdf",
                       "user_id": self.users["user"]["id"]}
        store.add_tokens("t1", 42)

        client = self._client_as("admin")
        resp = client.get("/api/admin/tasks")
        self.assertEqual(resp.status_code, 200)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["token_consumed"], 42)
        self.assertEqual(tasks[0]["user_id"], self.users["user"]["id"])


class TestTaskIsolation(_AuthApiTestBase):

    def _seed_tasks(self):
        store = self.bundle.task_store
        store["mine"] = {"status": "completed", "filename": "mine.pdf",
                         "user_id": self.users["user"]["id"], "file_size": 1}
        store["others"] = {"status": "completed", "filename": "others.pdf",
                           "user_id": self.users["admin"]["id"], "file_size": 1}

    def test_normal_user_sees_only_own_tasks(self):
        self._seed_tasks()
        client = self._client_as("user")
        resp = client.get("/api/tasks")
        self.assertEqual(resp.status_code, 200)
        names = {t["filename"] for t in resp.json()["tasks"]}
        self.assertEqual(names, {"mine.pdf"})

    def test_admin_sees_all_tasks(self):
        self._seed_tasks()
        client = self._client_as("admin")
        resp = client.get("/api/tasks")
        names = {t["filename"] for t in resp.json()["tasks"]}
        self.assertEqual(names, {"mine.pdf", "others.pdf"})

    def test_anonymous_sees_all_in_open_mode(self):
        self._seed_tasks()
        client = _make_client(temp_dir=self.temp.name)  # 匿名（开放模式）
        resp = client.get("/api/tasks")
        names = {t["filename"] for t in resp.json()["tasks"]}
        self.assertEqual(names, {"mine.pdf", "others.pdf"})

    def test_upload_associates_user(self):
        client = self._client_as("user")
        resp = client.post("/api/upload", files={
            "file": ("doc.md", b"# hello", "text/markdown"),
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        file_id = resp.json()["file_id"]
        info = self.bundle.task_store.get(file_id)
        self.assertEqual(info["user_id"], self.users["user"]["id"])

    def test_upload_anonymous_in_open_mode(self):
        client = _make_client(temp_dir=self.temp.name)
        resp = client.post("/api/upload", files={
            "file": ("doc.md", b"# hello", "text/markdown"),
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        info = self.bundle.task_store.get(resp.json()["file_id"])
        self.assertIsNone(info["user_id"])


class TestRequireLoginMode(_AuthApiTestBase):
    """强制登录模式（require_login=True）下的行为。"""

    def test_me_401_when_not_logged_in(self):
        client = _make_client(require_login=True, temp_dir=self.temp.name)
        resp = client.get("/api/auth/me")
        self.assertEqual(resp.status_code, 401)

    def test_tasks_401_when_not_logged_in(self):
        client = _make_client(require_login=True, temp_dir=self.temp.name)
        resp = client.get("/api/tasks")
        self.assertEqual(resp.status_code, 401)

    def test_tasks_ok_after_login(self):
        client = _make_client(require_login=True, temp_dir=self.temp.name)
        resp = client.post("/api/auth/login", json={"email": "user@x.com", "password": "user123"})
        self.assertEqual(resp.status_code, 200)
        resp = client.get("/api/tasks")
        self.assertEqual(resp.status_code, 200)


class TestPageGuards(_AuthApiTestBase):
    """页面路由守卫：登录页免鉴权、管理页要求管理员。"""

    def test_login_pages_accessible(self):
        client = _make_client(temp_dir=self.temp.name)
        for url in ("/login", "/admin/login"):
            resp = client.get(url, follow_redirects=False)
            self.assertEqual(resp.status_code, 200, url)

    def test_admin_pages_redirect_anonymous(self):
        client = _make_client(temp_dir=self.temp.name)
        for url in ("/admin/dashboard", "/admin/users", "/admin/users/new", "/admin/users/xyz"):
            resp = client.get(url, follow_redirects=False)
            self.assertEqual(resp.status_code, 302, url)
            self.assertEqual(resp.headers["location"], "/admin/login")

    def test_admin_pages_redirect_normal_user(self):
        client = self._client_as("user")
        resp = client.get("/admin/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

    def test_admin_pages_ok_for_admin(self):
        client = self._client_as("admin")
        for url in ("/admin/dashboard", "/admin/users", "/admin/users/new"):
            resp = client.get(url, follow_redirects=False)
            self.assertEqual(resp.status_code, 200, url)

    def test_front_pages_open_in_open_mode(self):
        client = _make_client(temp_dir=self.temp.name)  # require_login=False
        resp = client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

    def test_front_pages_redirect_when_require_login(self):
        import kbrefiner.main as main_mod

        client = _make_client(require_login=True, temp_dir=self.temp.name)
        original = main_mod.settings.require_login
        main_mod.settings.require_login = True
        try:
            resp = client.get("/", follow_redirects=False)
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp.headers["location"], "/login")
            # 登录后可访问
            r = client.post("/api/auth/login",
                            json={"email": "user@x.com", "password": "user123"})
            self.assertEqual(r.status_code, 200)
            resp = client.get("/", follow_redirects=False)
            self.assertEqual(resp.status_code, 200)
        finally:
            main_mod.settings.require_login = original


class TestCliCreateSuperuser(_AuthApiTestBase):
    """CLI createsuperuser 命令。"""

    def test_createsuperuser(self):
        from click.testing import CliRunner
        from kbrefiner.cli import cli

        db = str(Path(self.temp.name) / "cli.db")
        result = CliRunner().invoke(
            cli,
            ["createsuperuser", "--email", "root@x.com", "--username", "root",
             "--password", "root1234", "--db", db],
            input="root1234\n",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("创建成功", result.output)

        store = UserStore(db)
        try:
            user = store.get_by_email("root@x.com")
            self.assertIsNotNone(user)
            self.assertEqual(user["role"], "super_admin")
        finally:
            store.close()

    def test_createsuperuser_short_password_fails(self):
        from click.testing import CliRunner
        from kbrefiner.cli import cli

        db = str(Path(self.temp.name) / "cli2.db")
        result = CliRunner().invoke(
            cli,
            ["createsuperuser", "--email", "root2@x.com", "--username", "root",
             "--password", "123", "--db", db],
            input="123\n",
        )
        self.assertNotEqual(result.exit_code, 0)

    def test_createsuperuser_duplicate_email_fails(self):
        from click.testing import CliRunner
        from kbrefiner.cli import cli

        db = str(Path(self.temp.name) / "cli3.db")
        args = ["createsuperuser", "--email", "dup@x.com", "--username", "root",
                "--password", "root1234", "--db", db]
        r1 = CliRunner().invoke(cli, args, input="root1234\n")
        self.assertEqual(r1.exit_code, 0)
        r2 = CliRunner().invoke(cli, args, input="root1234\n")
        self.assertNotEqual(r2.exit_code, 0)


# =====================================================================
# FR-1: admin_stats 扩展字段（active_users / abnormal_tasks / user_rankings）
# =====================================================================

class TestFR1AdminStatsExtra(_AuthApiTestBase):

    def test_stats_contains_fr1_fields(self):
        uid = self.users["user"]["id"]
        aid = self.users["admin"]["id"]
        store = self.bundle.task_store
        # 一步完成所有字段（避免第二次 __setitem__ 缺少 status 时被置回 pending）
        store["t1"] = {
            "status": "completed", "filename": "a.pdf", "file_size": 10,
            "user_id": uid, "created_at": time.time(), "token_consumed": 300,
        }
        store["t2"] = {
            "status": "failed", "filename": "b.pdf", "file_size": 10,
            "user_id": uid, "created_at": time.time(),
            "error": "解析失败: 文件损坏",
        }
        store["t3"] = {
            "status": "processing", "filename": "c.pdf", "file_size": 10,
            "user_id": aid, "created_at": time.time() - 3 * 3600,
            "token_consumed": 50,
        }
        store["t4"] = {
            "status": "completed", "filename": "d.pdf", "file_size": 10,
            "user_id": aid, "created_at": time.time(), "token_consumed": 1000,
        }

        client = self._client_as("super")
        resp = client.get("/api/admin/stats")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()

        # FR-1 新增字段存在
        self.assertIn("active_users", data)
        self.assertIn("abnormal_tasks", data)
        self.assertIn("user_rankings", data)

        # active_users：2 个不同 user_id 均出现在 tasks
        self.assertEqual(data["active_users"], 2)

        # abnormal_tasks：failed(1)
        self.assertGreaterEqual(data["abnormal_tasks"], 1)

        # user_rankings：按消费 token 排序
        rankings = data["user_rankings"]
        self.assertIsInstance(rankings, list)
        self.assertTrue(len(rankings) <= 10)
        self.assertGreaterEqual(len(rankings), 2)
        # aid 的 token 1050 > uid 的 300
        ranking_ids = [r["user_id"] for r in rankings]
        self.assertLess(ranking_ids.index(aid), ranking_ids.index(uid))
        # 结构校验
        for r in rankings[:3]:
            for k in ("user_id", "username", "task_count", "token_consumed"):
                self.assertIn(k, r)


# =====================================================================
# FR-2: 用户详情 trend_7d（最近 7 天任务趋势）
# =====================================================================

class TestFR2UserDetailTrend7d(_AuthApiTestBase):

    def test_user_detail_contains_trend_7d(self):
        uid = self.users["user"]["id"]
        store = self.bundle.task_store
        # 用 __setitem__ 设置 user_id + created_at（含 2 条当天任务）
        now = time.time()
        store["t101"] = {
            "status": "completed", "filename": "a.pdf", "file_size": 10,
            "user_id": uid, "created_at": now, "token_consumed": 150,
        }
        store["t102"] = {
            "status": "failed", "filename": "b.pdf", "file_size": 10,
            "user_id": uid, "created_at": now - 3600,
        }
        # 旧任务（超过 7 天）：不应出现在趋势统计里，但应计入用户总任务
        store["t_old"] = {
            "status": "completed", "filename": "old.pdf", "file_size": 10,
            "user_id": uid, "created_at": now - 30 * 86400,
        }

        client = self._client_as("super")
        resp = client.get(f"/api/admin/users/{uid}")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("trend_7d", data)

        trend = data["trend_7d"]
        # 应为最近 7 天，每天一条记录
        self.assertEqual(len(trend), 7)
        # 结构
        for d in trend:
            for k in ("date", "task_count", "token_consumed"):
                self.assertIn(k, d)
        # 最近 7 天至少 2 条任务
        total = sum(d["task_count"] for d in trend)
        self.assertGreaterEqual(total, 2)
        # token_consumed 至少 150（t101）
        total_tokens = sum(d["token_consumed"] for d in trend)
        self.assertGreaterEqual(total_tokens, 150)

    def test_user_detail_404_still_works(self):
        client = self._client_as("super")
        resp = client.get("/api/admin/users/nosuchid")
        self.assertEqual(resp.status_code, 404)


# =====================================================================
# FR-4: 密码重置（生成重置链接 + 消费 token 改密）
# =====================================================================

class TestFR4PasswordReset(_AuthApiTestBase):

    def test_reset_link_requires_super_admin(self):
        # 普通管理员不可发重置链接
        client = self._client_as("admin")
        uid = self.users["user"]["id"]
        resp = client.post(f"/api/admin/users/{uid}/reset-link")
        self.assertEqual(resp.status_code, 403)

        # 未登录不可
        guest = _make_client(temp_dir=self.temp.name)
        resp = guest.post(f"/api/admin/users/{uid}/reset-link")
        self.assertEqual(resp.status_code, 401)

    def test_reset_link_success_and_consume(self):
        client = self._client_as("super")
        uid = self.users["user"]["id"]
        resp = client.post(f"/api/admin/users/{uid}/reset-link")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIn("token", body)
        self.assertIn("reset_url", body)
        self.assertTrue(body["token"])

        # 未消费前 verify 能读到
        info = self.bundle.password_reset_store.verify(body["token"])
        self.assertIsNotNone(info)
        self.assertEqual(str(info.get("user_id")), str(uid))

        # 通过 API 重置密码（无需登录）
        guest = _make_client(temp_dir=self.temp.name)
        resp = guest.post("/api/auth/reset-password", json={
            "token": body["token"], "new_password": "newpass9876",
        })
        self.assertEqual(resp.status_code, 200, resp.text)

        # 新密码可用，旧密码不可
        self.assertIsNotNone(self.bundle.user_store.verify_login("user@x.com", "newpass9876"))
        self.assertIsNone(self.bundle.user_store.verify_login("user@x.com", "user123"))

        # token 已消费
        self.assertIsNone(self.bundle.password_reset_store.verify(body["token"]))

    def test_reset_password_invalid_token(self):
        guest = _make_client(temp_dir=self.temp.name)
        resp = guest.post("/api/auth/reset-password", json={
            "token": "does-not-exist", "new_password": "helloworld8",
        })
        self.assertEqual(resp.status_code, 400)

    def test_reset_password_short_password_422(self):
        # 先申请一个真实 token
        client = self._client_as("super")
        uid = self.users["user"]["id"]
        token = client.post(f"/api/admin/users/{uid}/reset-link").json()["token"]

        guest = _make_client(temp_dir=self.temp.name)
        resp = guest.post("/api/auth/reset-password", json={
            "token": token, "new_password": "123",
        })
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
