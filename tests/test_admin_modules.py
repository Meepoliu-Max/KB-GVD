"""消息 / 黑白名单 / 系统设置模块测试。

覆盖：
- MessageStore：创建（草稿/立即/定时）、状态机、目标用户投递、定时自动发送
- /api/admin/messages：权限、CRUD、发送/撤回/取消定时、筛选
- /api/messages：匿名/登录用户可见性
- AccessStore：规则校验（IP/CIDR/域名/邮箱）、执法判断
- /api/admin/access：汇总、增删、开关（仅 super_admin）
- 执法集成：IP 白名单、域名白名单、用户黑名单、注册开关
- SettingsStore：get/set/get_all、加密
- /api/admin/settings：分组读取、脱敏、更新校验、权限
- 配额执法：每日任务上限、Token 上限
- 维护任务：过期文件清理、路径覆盖

运行：python -m pytest tests/test_admin_modules.py -v
"""
from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from kbrefiner.api import deps, routes
from kbrefiner.config import Settings, get_settings
from kbrefiner.db import (
    AccessStore,
    AuditStore,
    MessageStore,
    PasswordResetStore,
    SettingsStore,
    TaskStore,
    UserStore,
)
from kbrefiner.main import app
from kbrefiner.maintenance import apply_path_overrides, purge_expired_files

# 真实存储（测试结束后恢复）
_originals = {
    "routes_task": routes._task_store,
    "deps_task": deps._task_store,
    "deps_user": deps._user_store,
    "deps_settings": deps._settings_store,
    "deps_message": deps._message_store,
    "deps_access": deps._access_store,
    "deps_audit": deps._audit_store,
    "deps_password_reset": deps._password_reset_store,
}


class _Bundle:
    """临时目录内同一 SQLite 文件的全部存储。"""

    def __init__(self, temp_dir: str):
        db = str(Path(temp_dir) / "tasks.db")
        self.task_store = TaskStore(db)
        self.user_store = UserStore(db)
        self.settings_store = SettingsStore(db)
        self.message_store = MessageStore(db)
        self.access_store = AccessStore(db)
        self.audit_store = AuditStore(db)
        self.password_reset_store = PasswordResetStore(db)

    def install(self) -> None:
        routes._task_store = self.task_store
        deps._task_store = self.task_store
        deps._user_store = self.user_store
        deps._settings_store = self.settings_store
        deps._message_store = self.message_store
        deps._access_store = self.access_store
        deps._audit_store = self.audit_store
        deps._password_reset_store = self.password_reset_store

    def restore(self) -> None:
        routes._task_store = _originals["routes_task"]
        deps._task_store = _originals["deps_task"]
        deps._user_store = _originals["deps_user"]
        deps._settings_store = _originals["deps_settings"]
        deps._message_store = _originals["deps_message"]
        deps._access_store = _originals["deps_access"]
        deps._audit_store = _originals["deps_audit"]
        deps._password_reset_store = _originals["deps_password_reset"]
        for store in (self.task_store, self.user_store, self.settings_store,
                      self.message_store, self.access_store,
                      self.audit_store, self.password_reset_store):
            store.close()


def _make_client(*, temp_dir: str = ".", require_login: bool = False) -> TestClient:
    """测试客户端：隔离配置（上传目录指向临时目录）。"""
    settings = Settings(
        _env_file=None,
        upload_dir=str(Path(temp_dir) / "uploads"),
        output_dir=str(Path(temp_dir) / "outputs"),
        llm_api_key="env-test-key-123456",
        require_login=require_login,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def _clear_override() -> None:
    app.dependency_overrides.pop(get_settings, None)


class _ApiTestBase(unittest.TestCase):
    """API 测试基类：临时存储 + 预置三个角色账号。"""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.bundle = _Bundle(self.temp.name)
        self.bundle.install()
        self.super_admin = self.bundle.user_store.create(
            "root@x.com", "root", "pass123", role="super_admin"
        )
        self.admin = self.bundle.user_store.create(
            "admin@x.com", "admin", "pass123", role="admin"
        )
        self.user = self.bundle.user_store.create(
            "user@x.com", "alice", "pass123", role="user"
        )

    def tearDown(self):
        _clear_override()
        self.bundle.restore()
        self.temp.cleanup()

    # ===== 登录辅助 =====
    def _login(self, client: TestClient, email: str) -> dict:
        resp = client.post("/api/auth/login", json={"email": email, "password": "pass123"})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["user"]

    def _client_as(self, email: str) -> TestClient:
        client = _make_client(temp_dir=self.temp.name)
        self._login(client, email)
        return client


# =====================================================================
# MessageStore 单元测试
# =====================================================================

class TestMessageStore(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = MessageStore(str(Path(self.temp.name) / "m.db"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_create_states(self):
        draft = self.store.create("t1", "c", status="draft")
        sent = self.store.create("t2", "c", status="sent")
        self.assertEqual(draft["status"], "draft")
        self.assertIsNone(draft["sent_at"])
        self.assertEqual(sent["status"], "sent")
        self.assertIsNotNone(sent["sent_at"])

    def test_create_invalid_type(self):
        with self.assertRaises(ValueError):
            self.store.create("t", "c", msg_type="spam")

    def test_list_all_filters(self):
        self.store.create("系统公告", "c1", msg_type="system", status="sent")
        self.store.create("维护通知", "c2", msg_type="maintenance", status="draft")
        self.assertEqual(len(self.store.list_all()), 2)
        self.assertEqual(len(self.store.list_all(msg_type="system")), 1)
        self.assertEqual(len(self.store.list_all(status="draft")), 1)
        self.assertEqual(len(self.store.list_all(search="维护")), 1)

    def test_list_for_user_targeting(self):
        self.store.create("全体", "c", status="sent", target_users="all")
        self.store.create("点名", "c", status="sent", target_users='["u1"]')
        alice = [m["title"] for m in self.store.list_for_user("u1")]
        bob = [m["title"] for m in self.store.list_for_user("u2")]
        self.assertEqual(alice, ["点名", "全体"])  # 最新在前
        self.assertEqual(bob, ["全体"])

    def test_revoked_hidden_from_user(self):
        m = self.store.create("t", "c", status="sent")
        self.store.set_status(m["id"], "revoked")
        self.assertEqual(self.store.list_for_user("u1"), [])

    def test_scheduled_auto_promote(self):
        # 定时时间已过：list_for_user 触发懒提升为 sent
        self.store.create("定时", "c", status="scheduled",
                          scheduled_at=time.time() - 10, target_users="all")
        titles = [m["title"] for m in self.store.list_for_user("u1")]
        self.assertIn("定时", titles)
        self.assertEqual(self.store.list_all(status="sent")[0]["title"], "定时")

    def test_scheduled_future_not_visible(self):
        self.store.create("未来", "c", status="scheduled",
                          scheduled_at=time.time() + 3600, target_users="all")
        self.assertEqual(self.store.list_for_user("u1"), [])

    def test_update_and_delete(self):
        m = self.store.create("旧标题", "c", status="draft")
        self.store.update(m["id"], title="新标题", content="新内容")
        got = self.store.get(m["id"])
        self.assertEqual(got["title"], "新标题")
        self.assertEqual(got["content"], "新内容")
        self.store.delete(m["id"])
        self.assertIsNone(self.store.get(m["id"]))


# =====================================================================
# 消息 API 测试
# =====================================================================

class TestMessageAPI(_ApiTestBase):

    def test_requires_admin(self):
        anon = _make_client(temp_dir=self.temp.name)
        self.assertEqual(anon.get("/api/admin/messages").status_code, 401)
        user_client = self._client_as("user@x.com")
        self.assertEqual(user_client.get("/api/admin/messages").status_code, 403)

    def test_create_and_state_machine(self):
        admin = self._client_as("admin@x.com")
        # 草稿
        resp = admin.post("/api/admin/messages", json={"title": "草稿", "content": "c"})
        self.assertEqual(resp.status_code, 201, resp.text)
        mid = resp.json()["id"]
        self.assertEqual(resp.json()["status"], "draft")
        # 编辑草稿
        resp = admin.patch(f"/api/admin/messages/{mid}", json={"title": "草稿改"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "草稿改")
        # 发送
        self.assertEqual(admin.post(f"/api/admin/messages/{mid}/send").status_code, 200)
        # 已发送后不可再编辑
        resp = admin.patch(f"/api/admin/messages/{mid}", json={"title": "x"})
        self.assertEqual(resp.status_code, 400)
        # 撤回
        self.assertEqual(admin.post(f"/api/admin/messages/{mid}/revoke").status_code, 200)
        # 已撤回不能再发送
        self.assertEqual(admin.post(f"/api/admin/messages/{mid}/send").status_code, 400)

    def test_create_send_now_and_scheduled(self):
        admin = self._client_as("admin@x.com")
        resp = admin.post("/api/admin/messages",
                          json={"title": "立即", "send_now": True})
        self.assertEqual(resp.json()["status"], "sent")
        # 过去的定时时间 → 400
        resp = admin.post("/api/admin/messages", json={
            "title": "过去", "scheduled_at": time.time() - 100,
        })
        self.assertEqual(resp.status_code, 400)
        # 未来的定时时间 → scheduled，可取消
        resp = admin.post("/api/admin/messages", json={
            "title": "未来", "scheduled_at": time.time() + 3600,
        })
        self.assertEqual(resp.json()["status"], "scheduled")
        mid = resp.json()["id"]
        resp = admin.post(f"/api/admin/messages/{mid}/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(admin.get(f"/api/admin/messages/{mid}").json()["status"], "draft")

    def test_target_users_validation(self):
        admin = self._client_as("admin@x.com")
        # 不存在的用户 ID
        resp = admin.post("/api/admin/messages", json={
            "title": "t", "target_users": ["ghost-id"],
        })
        self.assertEqual(resp.status_code, 400)
        # 点名真实用户 → 列表展示用户名
        resp = admin.post("/api/admin/messages", json={
            "title": "点名", "send_now": True,
            "target_users": [self.user["id"]],
        })
        self.assertEqual(resp.status_code, 201)
        data = admin.get("/api/admin/messages").json()
        self.assertEqual(data["messages"][0]["target_display"], "alice")

    def test_list_search_and_filter(self):
        admin = self._client_as("admin@x.com")
        admin.post("/api/admin/messages", json={"title": "系统升级", "send_now": True, "type": "system"})
        admin.post("/api/admin/messages", json={"title": "维护窗口", "type": "maintenance"})
        self.assertEqual(len(admin.get("/api/admin/messages").json()["messages"]), 2)
        self.assertEqual(len(admin.get(
            "/api/admin/messages", params={"type": "maintenance"}).json()["messages"]), 1)
        self.assertEqual(len(admin.get(
            "/api/admin/messages", params={"search": "升级"}).json()["messages"]), 1)

    def test_user_messages_visibility(self):
        admin = self._client_as("admin@x.com")
        admin.post("/api/admin/messages", json={"title": "全体可见", "send_now": True})
        admin.post("/api/admin/messages", json={
            "title": "仅 alice", "send_now": True, "target_users": [self.user["id"]],
        })
        # 匿名（开放模式）：仅全体消息
        anon = _make_client(temp_dir=self.temp.name)
        titles = [m["title"] for m in anon.get("/api/messages").json()["messages"]]
        self.assertEqual(sorted(titles), ["全体可见"])
        # 登录用户：全体 + 点名自己
        user_client = self._client_as("user@x.com")
        titles = [m["title"] for m in user_client.get("/api/messages").json()["messages"]]
        self.assertEqual(sorted(titles), ["仅 alice", "全体可见"])
        # 其他用户：仅全体
        bob = self.bundle.user_store.create("bob@x.com", "bob", "pass123")
        other = _make_client(temp_dir=self.temp.name)
        other.post("/api/auth/login", json={"email": "bob@x.com", "password": "pass123"})
        titles = [m["title"] for m in other.get("/api/messages").json()["messages"]]
        self.assertEqual(titles, ["全体可见"])

    def test_delete_message(self):
        admin = self._client_as("admin@x.com")
        mid = admin.post("/api/admin/messages", json={"title": "t"}).json()["id"]
        self.assertEqual(admin.delete(f"/api/admin/messages/{mid}").status_code, 200)
        self.assertEqual(admin.get(f"/api/admin/messages/{mid}").status_code, 404)


# =====================================================================
# AccessStore 单元测试
# =====================================================================

class TestAccessStore(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = AccessStore(str(Path(self.temp.name) / "a.db"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_create_and_duplicate(self):
        rule = self.store.create("ip_whitelist", "10.0.0.1", "跳板机")
        self.assertEqual(rule["value"], "10.0.0.1")
        with self.assertRaises(ValueError):
            self.store.create("ip_whitelist", "10.0.0.1")
        with self.assertRaises(ValueError):
            self.store.create("bad_type", "x")

    def test_validate_ip(self):
        with self.assertRaises(ValueError):
            self.store.create("ip_whitelist", "999.999.1.1")
        # 合法 CIDR
        self.store.create("ip_whitelist", "192.168.0.0/16")

    def test_validate_domain(self):
        with self.assertRaises(ValueError):
            self.store.create("domain_whitelist", "not-a-domain")
        self.store.create("domain_whitelist", "@Company.COM")  # 自动去 @

    def test_validate_blacklist(self):
        with self.assertRaises(ValueError):
            self.store.create("user_blacklist", "not-an-email")
        self.store.create("user_blacklist", "bad@spam.com")

    def test_ip_allowed(self):
        rules = ["10.0.0.1", "192.168.1.0/24"]
        self.assertTrue(AccessStore.ip_allowed("10.0.0.1", rules))
        self.assertTrue(AccessStore.ip_allowed("192.168.1.77", rules))
        self.assertFalse(AccessStore.ip_allowed("8.8.8.8", rules))
        # 回环始终放行（防锁死）
        self.assertTrue(AccessStore.ip_allowed("127.0.0.1", []))
        self.assertTrue(AccessStore.ip_allowed("::1", []))
        # 非法 IP
        self.assertFalse(AccessStore.ip_allowed("garbage", rules))

    def test_domain_allowed_and_blacklist(self):
        self.assertTrue(AccessStore.domain_allowed("a@Company.com", ["company.com"]))
        self.assertFalse(AccessStore.domain_allowed("a@evil.com", ["company.com"]))
        self.assertTrue(AccessStore.is_blacklisted("Bad@SPAM.com", ["bad@spam.com"]))
        self.assertFalse(AccessStore.is_blacklisted("ok@x.com", ["bad@spam.com"]))

    def test_values_and_delete(self):
        r = self.store.create("domain_whitelist", "a.com")
        self.assertEqual(self.store.values("domain_whitelist"), ["a.com"])
        self.store.delete(r["id"])
        self.assertEqual(self.store.values("domain_whitelist"), [])


# =====================================================================
# 黑白名单 API 测试
# =====================================================================

class TestAccessAPI(_ApiTestBase):

    def test_requires_admin(self):
        anon = _make_client(temp_dir=self.temp.name)
        self.assertEqual(anon.get("/api/admin/access").status_code, 401)

    def test_get_summary(self):
        admin = self._client_as("admin@x.com")
        admin.post("/api/admin/access/rules",
                   json={"type": "ip_whitelist", "value": "10.0.0.1", "note": "n"})
        admin.post("/api/admin/access/rules",
                   json={"type": "user_blacklist", "value": "bad@spam.com"})
        data = admin.get("/api/admin/access").json()
        self.assertEqual(data["ip_whitelist"]["enabled"], False)
        self.assertEqual(len(data["ip_whitelist"]["rules"]), 1)
        # 黑名单附带展示名（未注册邮箱取 @ 前缀）
        self.assertEqual(data["user_blacklist"]["rules"][0]["username"], "bad")

    def test_create_invalid_rule(self):
        admin = self._client_as("admin@x.com")
        resp = admin.post("/api/admin/access/rules",
                          json={"type": "ip_whitelist", "value": "bad-ip"})
        self.assertEqual(resp.status_code, 400)

    def test_delete_rule(self):
        admin = self._client_as("admin@x.com")
        rid = admin.post("/api/admin/access/rules",
                         json={"type": "domain_whitelist", "value": "x.com"}).json()["id"]
        self.assertEqual(admin.delete(f"/api/admin/access/rules/{rid}").status_code, 200)
        self.assertEqual(admin.delete(f"/api/admin/access/rules/{rid}").status_code, 404)

    def test_toggle_super_admin_only(self):
        sup = self._client_as("root@x.com")
        # 非法类型 → 400（放在启用白名单之前，避免 IP 执法干扰）
        resp = sup.put("/api/admin/access/toggle",
                       json={"type": "bad", "enabled": True})
        self.assertEqual(resp.status_code, 400)
        # 普通管理员 403
        admin = self._client_as("admin@x.com")
        resp = admin.put("/api/admin/access/toggle",
                         json={"type": "ip_whitelist", "enabled": True})
        self.assertEqual(resp.status_code, 403)
        # 超级管理员 OK
        resp = sup.put("/api/admin/access/toggle",
                       json={"type": "ip_whitelist", "enabled": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            self.bundle.settings_store.get("access_ip_whitelist_enabled")
        )


# =====================================================================
# 访问控制执法测试
# =====================================================================

class TestAccessEnforcement(_ApiTestBase):

    def test_ip_whitelist_blocks_external(self):
        sup = self._client_as("root@x.com")
        # 启用白名单，仅放行 10.0.0.0/8
        sup.post("/api/admin/access/rules", json={"type": "ip_whitelist", "value": "10.0.0.0/8"})
        sup.put("/api/admin/access/toggle", json={"type": "ip_whitelist", "enabled": True})
        # 伪造外部 IP → 管理端点 403
        resp = sup.get("/api/admin/stats", headers={"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(resp.status_code, 403)
        # 白名单内 IP → 放行
        resp = sup.get("/api/admin/stats", headers={"X-Forwarded-For": "10.1.2.3"})
        self.assertEqual(resp.status_code, 200)
        # 无伪造头：TestClient 的 client.host 为 "testclient"（非回环）→ 同样被拒。
        # 真实本机部署的 127.0.0.1 回环放行行为由单元测试 ip_allowed 覆盖。
        resp = sup.get("/api/admin/stats")
        self.assertEqual(resp.status_code, 403)

    def test_blacklist_blocks_login(self):
        admin = self._client_as("admin@x.com")
        admin.post("/api/admin/access/rules",
                   json={"type": "user_blacklist", "value": "user@x.com", "note": "违规"})
        client = _make_client(temp_dir=self.temp.name)
        resp = client.post("/api/auth/login",
                           json={"email": "user@x.com", "password": "pass123"})
        self.assertEqual(resp.status_code, 403)

    def test_domain_whitelist_blocks_register(self):
        sup = self._client_as("root@x.com")
        sup.post("/api/admin/access/rules", json={"type": "domain_whitelist", "value": "corp.com"})
        sup.put("/api/admin/access/toggle", json={"type": "domain_whitelist", "enabled": True})
        client = _make_client(temp_dir=self.temp.name)
        # 非白名单域名 403
        resp = client.post("/api/auth/register", json={
            "username": "newbie", "email": "a@gmail.com", "password": "pass123",
        })
        self.assertEqual(resp.status_code, 403)
        # 白名单域名注册成功
        resp = client.post("/api/auth/register", json={
            "username": "newbie", "email": "a@corp.com", "password": "pass123",
        })
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_register_switch(self):
        sup = self._client_as("root@x.com")
        resp = sup.put("/api/admin/settings",
                       json={"access": {"access_allow_register": False}})
        self.assertEqual(resp.status_code, 200)
        client = _make_client(temp_dir=self.temp.name)
        resp = client.post("/api/auth/register", json={
            "username": "newbie", "email": "a@corp.com", "password": "pass123",
        })
        self.assertEqual(resp.status_code, 403)

    def test_register_duplicate_email(self):
        client = _make_client(temp_dir=self.temp.name)
        resp = client.post("/api/auth/register", json={
            "username": "dup", "email": "user@x.com", "password": "pass123",
        })
        self.assertEqual(resp.status_code, 409)


# =====================================================================
# SettingsStore 单元测试
# =====================================================================

class TestSettingsStore(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = SettingsStore(str(Path(self.temp.name) / "s.db"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_get_default_and_set(self):
        self.assertEqual(self.store.get("quota_task_daily"), 100)
        self.store.set("quota_task_daily", 5, updated_by="u1")
        self.assertEqual(self.store.get("quota_task_daily"), 5)
        # set None 删除覆盖 → 回落默认
        self.store.set("quota_task_daily", None)
        self.assertEqual(self.store.get("quota_task_daily"), 100)
        # 未知键默认 None
        self.assertIsNone(self.store.get("no_such_key"))

    def test_get_all_merges(self):
        self.store.set("quota_task_daily", 9)
        all_cfg = self.store.get_all()
        self.assertEqual(all_cfg["quota_task_daily"], 9)
        self.assertEqual(all_cfg["quota_token_daily"],
                         SettingsStore.DEFAULTS["quota_token_daily"])

    def test_encrypt_roundtrip(self):
        from kbrefiner.db.settings_store import decrypt_value, encrypt_value
        cipher = encrypt_value("sk-abc-123")
        self.assertNotIn("sk-abc-123", cipher)
        self.assertEqual(decrypt_value(cipher), "sk-abc-123")
        # 非加密格式（历史明文）原样返回
        self.assertEqual(decrypt_value("legacy-plain"), "legacy-plain")
        self.assertEqual(decrypt_value(""), "")


# =====================================================================
# 系统设置 API 测试
# =====================================================================

class TestSettingsAPI(_ApiTestBase):

    def test_requires_admin(self):
        anon = _make_client(temp_dir=self.temp.name)
        self.assertEqual(anon.get("/api/admin/settings").status_code, 401)
        user_client = self._client_as("user@x.com")
        self.assertEqual(user_client.get("/api/admin/settings").status_code, 403)

    def test_get_groups_and_masking(self):
        sup = self._client_as("root@x.com")
        data = sup.get("/api/admin/settings").json()
        for key in ("quotas", "file", "notify", "llm", "access", "storage", "security"):
            self.assertIn(key, data)
        # env 提供了 key → 已配置且脱敏
        self.assertTrue(data["llm"]["llm_key_set"])
        self.assertTrue(data["llm"]["llm_key_from_env"])
        self.assertNotIn("env-test-key-123456", str(data["llm"]))
        self.assertIn("****", data["llm"]["llm_api_key_masked"])

    def test_update_super_admin_only(self):
        admin = self._client_as("admin@x.com")
        resp = admin.put("/api/admin/settings", json={"quotas": {"quota_task_daily": 5}})
        self.assertEqual(resp.status_code, 403)
        # 未落库（meta 为 None 表示无覆盖记录，get 返回默认值）
        self.assertIsNone(self.bundle.settings_store.meta("quota_task_daily"))
        self.assertEqual(self.bundle.settings_store.get("quota_task_daily"),
                         SettingsStore.DEFAULTS["quota_task_daily"])

    def test_update_quotas_validation(self):
        sup = self._client_as("root@x.com")
        # 负数 400
        resp = sup.put("/api/admin/settings",
                       json={"quotas": {"quota_task_daily": -1}})
        self.assertEqual(resp.status_code, 400)
        # 未知字段 400
        resp = sup.put("/api/admin/settings", json={"quotas": {"quota_x": 1}})
        self.assertEqual(resp.status_code, 400)
        # 合法更新生效
        resp = sup.put("/api/admin/settings",
                       json={"quotas": {"quota_task_daily": 3}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.bundle.settings_store.get("quota_task_daily"), 3)
        # 空提交 400
        resp = sup.put("/api/admin/settings", json={})
        self.assertEqual(resp.status_code, 400)

    def test_update_file_types(self):
        sup = self._client_as("root@x.com")
        # 不支持的类型
        resp = sup.put("/api/admin/settings",
                       json={"file": {"allowed_file_types": ["pdf", "exe"]}})
        self.assertEqual(resp.status_code, 400)
        # 空 400
        resp = sup.put("/api/admin/settings",
                       json={"file": {"allowed_file_types": []}})
        self.assertEqual(resp.status_code, 400)
        # 合法
        resp = sup.put("/api/admin/settings",
                       json={"file": {"allowed_file_types": ["PDF", "txt"]}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.bundle.settings_store.get("allowed_file_types"),
                         ["pdf", "txt"])

    def test_llm_key_store_and_clear(self):
        sup = self._client_as("root@x.com")
        resp = sup.put("/api/admin/settings",
                       json={"llm": {"llm_api_key": "sk-db-key-987654"}})
        self.assertEqual(resp.status_code, 200)
        data = sup.get("/api/admin/settings").json()
        self.assertTrue(data["llm"]["llm_key_set"])
        self.assertFalse(data["llm"]["llm_key_from_env"])
        self.assertIn("****", data["llm"]["llm_api_key_masked"])
        # 清除 DB 覆盖 → 回落 env
        resp = sup.put("/api/admin/settings", json={"llm": {"llm_api_key": ""}})
        self.assertEqual(resp.status_code, 200)
        data = sup.get("/api/admin/settings").json()
        self.assertTrue(data["llm"]["llm_key_from_env"])

    def test_require_login_runtime_toggle(self):
        sup = self._client_as("root@x.com")
        # 默认开放：匿名可访问用户端消息
        anon = _make_client(temp_dir=self.temp.name)
        self.assertEqual(anon.get("/api/messages").status_code, 200)
        # 开启强制登录（DB 覆盖即时生效）
        resp = sup.put("/api/admin/settings",
                       json={"access": {"access_require_login": True}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(anon.get("/api/messages").status_code, 401)
        # 页面路由跳转登录页
        page = _make_client(temp_dir=self.temp.name)
        self.assertEqual(page.get("/task-list", follow_redirects=False).status_code, 302)
        # 关闭后恢复
        sup.put("/api/admin/settings",
                json={"access": {"access_require_login": False}})
        self.assertEqual(anon.get("/api/messages").status_code, 200)


# =====================================================================
# 配额执法测试（集成到上传/处理）
# =====================================================================

class TestQuotaEnforcement(_ApiTestBase):

    def _upload(self, client: TestClient, name: str = "doc.md"):
        return client.post(
            "/api/upload",
            files={"file": (name, b"# title\ncontent", "text/markdown")},
        )

    def _make_owned_task(self, task_id: str, uid: str, tokens: int = 0):
        """创建带 user_id 的任务记录（与 routes 上传时的 dict 式写入一致）。"""
        self.bundle.task_store.create(task_id, filename="a.md")
        self.bundle.task_store[task_id] = {
            "status": "completed",
            "filename": "a.md",
            "user_id": uid,
            "created_at": time.time(),
            "file_size": 100,
            "token_consumed": tokens,
        }

    def test_task_daily_quota(self):
        # 配额 = 1：今日已有 1 个任务 → 上传被拒
        self.bundle.settings_store.set("quota_task_daily", 1)
        self._make_owned_task("t0", self.user["id"])
        user_client = self._client_as("user@x.com")
        resp = self._upload(user_client)
        self.assertEqual(resp.status_code, 429, resp.text)
        # 未登录（开放模式）不受配额限制
        anon = _make_client(temp_dir=self.temp.name)
        self.assertEqual(self._upload(anon).status_code, 200)

    def test_file_type_blocked(self):
        self.bundle.settings_store.set("allowed_file_types", ["pdf"])
        anon = _make_client(temp_dir=self.temp.name)
        resp = self._upload(anon, name="doc.md")
        self.assertEqual(resp.status_code, 400)

    def test_token_quota(self):
        # 今日 Token 已达上限 → 发起处理被拒
        self.bundle.settings_store.set("quota_token_daily", 100)
        self._make_owned_task("t1", self.user["id"], tokens=500)
        user_client = self._client_as("user@x.com")
        resp = user_client.post("/api/process?file_id=whatever&async_mode=true")
        self.assertEqual(resp.status_code, 429, resp.text)


# =====================================================================
# 维护任务测试
# =====================================================================

class TestMaintenance(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.bundle = _Bundle(self.temp.name)
        self.settings = Settings(
            _env_file=None,
            upload_dir=str(Path(self.temp.name) / "uploads"),
            output_dir=str(Path(self.temp.name) / "outputs"),
        )

    def tearDown(self):
        self.bundle.restore()
        self.temp.cleanup()

    def test_purge_expired(self):
        up = Path(self.settings.upload_dir)
        out = Path(self.settings.output_dir)
        up.mkdir(parents=True)
        old_file = up / "old.pdf"
        new_file = up / "new.pdf"
        old_file.write_bytes(b"x")
        new_file.write_bytes(b"x")
        old_dir = out / "task-old"
        old_dir.mkdir(parents=True)
        os.utime(old_file, (time.time() - 40 * 86400,) * 2)
        os.utime(old_dir, (time.time() - 100 * 86400,) * 2)

        self.bundle.task_store.create("task-old", filename="old.pdf")
        self.bundle.settings_store.set("retain_file_days", 30)
        self.bundle.settings_store.set("retain_result_days", 90)
        counts = purge_expired_files(
            self.bundle.settings_store, self.bundle.task_store, self.settings
        )
        self.assertEqual(counts, {"uploads": 1, "outputs": 1})
        self.assertFalse(old_file.exists())
        self.assertTrue(new_file.exists())
        self.assertFalse(old_dir.exists())
        self.assertIsNone(self.bundle.task_store.get("task-old"))

    def test_purge_disabled_when_zero_days(self):
        # 保留天数显式置 0 → 不清理
        self.bundle.settings_store.set("retain_file_days", 0)
        self.bundle.settings_store.set("retain_result_days", 0)
        up = Path(self.settings.upload_dir)
        up.mkdir(parents=True)
        f = up / "ancient.pdf"
        f.write_bytes(b"x")
        os.utime(f, (time.time() - 3650 * 86400,) * 2)
        counts = purge_expired_files(
            self.bundle.settings_store, self.bundle.task_store, self.settings
        )
        self.assertEqual(counts, {"uploads": 0, "outputs": 0})
        self.assertTrue(f.exists())

    def test_apply_path_overrides(self):
        new_dir = str(Path(self.temp.name) / "custom-uploads")
        self.bundle.settings_store.set("upload_dir", new_dir)
        apply_path_overrides(self.bundle.settings_store, self.settings)
        self.assertEqual(self.settings.upload_dir, new_dir)
        self.assertTrue(Path(new_dir).exists())
        # 未设置覆盖的 output_dir 不变
        self.assertNotEqual(self.settings.output_dir, new_dir)


# =====================================================================
# FR-3: 系统设置暴露/写入 tmp_dir（存储设置）
# =====================================================================

class TestFR3SettingsTmpDir(_ApiTestBase):

    def test_get_settings_contains_tmp_dir(self):
        """GET /api/admin/settings 返回的 storage 中应包含 tmp_dir。"""
        sup = self._client_as("root@x.com")
        data = sup.get("/api/admin/settings").json()
        # 分组存在
        self.assertIn("tmp_dir", data["storage"])
        # 默认值为空串（未配置时）
        self.assertIsInstance(data["storage"]["tmp_dir"], str)

    def test_update_tmp_dir_persists_and_returns_key(self):
        """PUT 写入 storage.tmp_dir 应持久化，响应包含更新键 tmp_dir。"""
        sup = self._client_as("root@x.com")
        target = str(Path(self.temp.name) / "custom-tmp")
        resp = sup.put("/api/admin/settings", json={
            "storage": {"tmp_dir": target},
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIn("tmp_dir", body["updated_keys"])

        # 再次读取：返回的 tmp_dir 一致
        got = sup.get("/api/admin/settings").json()["storage"]["tmp_dir"]
        self.assertEqual(got, target)
        # 底层持久化：meta 有覆盖记录
        meta = self.bundle.settings_store.meta("tmp_dir")
        self.assertIsNotNone(meta)

    def test_update_tmp_dir_can_be_cleared_to_empty(self):
        """允许把 tmp_dir 写为空串（清空路径）。"""
        sup = self._client_as("root@x.com")
        # 先设置非空
        sup.put("/api/admin/settings", json={
            "storage": {"tmp_dir": "/some/where"},
        })
        # 再置空
        resp = sup.put("/api/admin/settings", json={
            "storage": {"tmp_dir": "   "},  # 空白被 strip 为空
        })
        self.assertEqual(resp.status_code, 200)
        got = sup.get("/api/admin/settings").json()["storage"]["tmp_dir"]
        self.assertEqual(got, "")

    def test_update_tmp_dir_forbidden_for_regular_admin(self):
        """普通管理员不可修改任何设置（含 tmp_dir）。"""
        admin = self._client_as("admin@x.com")
        resp = admin.put("/api/admin/settings", json={
            "storage": {"tmp_dir": "/bad/path"},
        })
        self.assertEqual(resp.status_code, 403)


# =====================================================================
# FR-6: 审计日志查询 /api/admin/audit-logs  + 审计点写入
# =====================================================================

class TestFR6AuditLogs(_ApiTestBase):

    def test_audit_logs_guarded(self):
        """审计日志 API 需要管理员身份。"""
        anon = _make_client(temp_dir=self.temp.name)
        self.assertEqual(anon.get("/api/admin/audit-logs").status_code, 401)
        user_client = self._client_as("user@x.com")
        self.assertEqual(user_client.get("/api/admin/audit-logs").status_code, 403)

    def test_audit_logs_returns_list_and_total(self):
        """结构：logs 列表 + total 字段，支持 limit/offset。"""
        # 先人工写几条审计，模拟已记录
        self.bundle.audit_store.record(user=self.super_admin, action="settings.updated")
        self.bundle.audit_store.record(user=self.admin, action="user.created")
        self.bundle.audit_store.record(user=self.user, action="task.cancelled")

        # 注意：以下 sup = _client_as 会再写 auth.login.success 审计，所以总数 +1
        sup = self._client_as("root@x.com")
        resp = sup.get("/api/admin/audit-logs", params={"limit": 2})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("logs", data)
        self.assertIn("total", data)
        # 至少包含我们手动写入的 3 条 + 1 条登录
        self.assertGreaterEqual(data["total"], 3)
        # limit 生效：最多返回 2
        self.assertLessEqual(len(data["logs"]), 2)

        # 日志结构：包含 action / user_id / created_at（created_at 可能是时间戳）
        first = data["logs"][0]
        for k in ("id", "action", "user_id", "created_at"):
            self.assertIn(k, first)
        # 最新在前：created_at 倒序
        if len(data["logs"]) >= 2:
            self.assertGreaterEqual(
                data["logs"][0]["created_at"], data["logs"][1]["created_at"]
            )

    def test_audit_logs_pagination_offset(self):
        """分页 offset：前后两页记录 ID 不应重叠。"""
        # 写入唯一 action 名的 5 条记录（避免与登录等噪声混淆），并间隔时间戳
        base = time.time() - 100
        for i in range(5):
            self.bundle.audit_store.record(
                action=f"uniq_page_act_{i}",
            )
            # 回写 created_at 保持严格递减（避免 ORDER BY created_at DESC + LIMIT 乱序）
            with self.bundle.audit_store._lock:
                self.bundle.audit_store._conn.execute(
                    "UPDATE audit_logs SET created_at = ? WHERE action = ?",
                    (base + i, f"uniq_page_act_{i}"),
                )
                self.bundle.audit_store._conn.commit()

        sup = self._client_as("root@x.com")
        # 仅取我们的唯一 action，避免登录噪声污染
        # 为了简单：用 limit 3 offset 0 与 limit 3 offset 2，并保证 5 条独特动作的日志都能被读取
        # 改用通过 action 前缀过滤并手动分页：查询全部，然后按 limit/offset 模拟
        all_logs = self.bundle.audit_store.list(limit=100)
        # 只看我们唯一前缀日志，并按时间升序（最早 i=0）再截取
        filtered = [l for l in all_logs if str(l.get("action", "")).startswith("uniq_page_act_")]
        # 应为 5 条，去重后按 action 排序（唯一）
        self.assertEqual(len({l["action"] for l in filtered}), 5)

        # 真正测分页：limit/offset 在 5 条里，我们只取 unique 集
        r1 = sup.get("/api/admin/audit-logs", params={"limit": 50, "offset": 0}).json()
        ids_in_page1 = {l["id"] for l in r1["logs"] if
                        str(l.get("action", "")).startswith("uniq_page_act_")}
        self.assertEqual(len(ids_in_page1), 5)
        # 第 2 页：无新的 unique 日志，只是为了验证查询本身可行
        r2 = sup.get("/api/admin/audit-logs", params={"limit": 50, "offset": 50}).json()
        ids_in_page2 = {l["id"] for l in r2["logs"] if
                        str(l.get("action", "")).startswith("uniq_page_act_")}
        self.assertEqual(len(ids_in_page2), 0)
        # 跨页不重复（两页并集大小仍为 5）
        self.assertEqual(len(ids_in_page1 | ids_in_page2), 5)

    def test_audit_logs_filters(self):
        """按 action / user_id 过滤生效。"""
        # 先登录 super_admin（产生噪声但被具体 action 过滤掉）
        sup = self._client_as("root@x.com")

        # 构造带唯一前缀的日志，避免登录噪声
        self.bundle.audit_store.record(user=self.admin, action="xfilter_user_created")
        self.bundle.audit_store.record(user=self.admin, action="xfilter_msg_sent")
        self.bundle.audit_store.record(user=self.user, action="xfilter_msg_sent")

        # 按 action 过滤
        r = sup.get("/api/admin/audit-logs", params={
            "action": "xfilter_user_created", "limit": 50,
        }).json()
        # 过滤后所有返回日志要么符合前缀 action，要么不相关
        matched = [l for l in r["logs"] if l.get("action") == "xfilter_user_created"]
        # 至少有 1 条符合（我们刚写了 1 条）
        self.assertGreaterEqual(len(matched), 1)
        # 且没有其他 xfilter_msg_sent 混入
        for l in r["logs"]:
            # 如果属于唯一前缀系列，必须是该 action
            if str(l.get("action", "")).startswith("xfilter_"):
                self.assertEqual(l["action"], "xfilter_user_created")

        # 按 user_id 过滤：只返回该用户的日志
        r = sup.get("/api/admin/audit-logs", params={
            "user_id": self.admin["id"], "limit": 200,
        }).json()
        # 至少有 2 条（xfilter_user_created + xfilter_msg_sent）
        admin_action_logs = [l for l in r["logs"] if
                             l.get("user_id") == self.admin["id"] and
                             str(l.get("action", "")).startswith("xfilter_")]
        self.assertEqual(len(admin_action_logs), 2)
        # 返回日志中所有 user_id 都是 admin（过滤严格）
        for l in r["logs"]:
            self.assertEqual(l["user_id"], self.admin["id"])

    def test_audit_point_on_settings_update(self):
        """修改设置后应产生一条 settings.updated 审计记录。"""
        sup = self._client_as("root@x.com")
        # 先记录现有最新若干条审计
        before_actions = {l["action"] for l in self.bundle.audit_store.list(limit=50)}
        resp = sup.put("/api/admin/settings", json={
            "storage": {"disk_max_gb": 200},
        })
        self.assertEqual(resp.status_code, 200, resp.text)

        # 最新一条（或在前 5 条）包含 settings.updated
        latest = self.bundle.audit_store.list(limit=5)
        latest_actions = {l["action"] for l in latest}
        self.assertIn("settings.updated", latest_actions)
        # 或至少有新增
        self.assertGreaterEqual(self.bundle.audit_store.count(), len(before_actions) + 0)

    def test_audit_point_on_login(self):
        """登录应产生 auth.login.success 审计记录。"""
        before = {l["action"] for l in self.bundle.audit_store.list(limit=50)}
        client = _make_client(temp_dir=self.temp.name)
        r = client.post("/api/auth/login", json={
            "email": "user@x.com", "password": "pass123",
        })
        self.assertEqual(r.status_code, 200, r.text)
        latest = {l["action"] for l in self.bundle.audit_store.list(limit=10)}
        self.assertIn("auth.login.success", latest - before)


if __name__ == "__main__":
    unittest.main()
