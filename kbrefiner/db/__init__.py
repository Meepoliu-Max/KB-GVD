"""数据持久化层。

TaskStore: 任务状态 SQLite 存储（重启不丢失）。
UserStore: 用户存储（管理员/普通用户统一表，配合 auth 模块）。
SettingsStore: 系统设置 key-value 存储（运行时动态配置源）。
MessageStore: 系统消息存储（管理后台消息列表 + 用户端展示）。
AccessStore: 黑白名单访问控制规则存储。
"""
from kbrefiner.db.access_store import AccessStore, client_ip
from kbrefiner.db.message_store import MessageStore
from kbrefiner.db.settings_store import SettingsStore
from kbrefiner.db.task_store import TaskStore
from kbrefiner.db.user_store import UserStore

__all__ = [
    "TaskStore",
    "UserStore",
    "SettingsStore",
    "MessageStore",
    "AccessStore",
    "client_ip",
]
