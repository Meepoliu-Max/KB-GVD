"""全局配置管理。

基于 pydantic-settings 从环境变量 / .env 读取配置。
支持所有 OpenAI 兼容的 LLM API（DeepSeek / OpenAI / Ollama 等）。
"""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ===== LLM API（通用，兼容所有 OpenAI 格式）=====
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    # 主力模型：日常 4 阶流水线（deepseek-chat 为 DeepSeek 官方真实模型名）
    llm_model: str = "deepseek-chat"
    # 备选模型：复杂推理（deepseek-reasoner 为官方推理模型）
    llm_model_pro: str = "deepseek-chat"

    # ===== 文档解析 =====
    parser_backend: Literal["auto", "mineru", "simple"] = "auto"
    mineru_backend: Literal["pipeline", "vlm-engine"] = "pipeline"

    # ===== 数据库 =====
    database_url: str = "sqlite+aiosqlite:///./data/kbrefiner.db"

    # ===== 应用 =====
    app_env: Literal["development", "production", "test"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # ===== 认证与访问控制 =====
    # 前台是否强制登录（默认关闭：开源单机模式直接可用；
    # 开启后未登录访问前台页面/业务 API 跳转或返回 401）
    require_login: bool = False
    # 令牌签名密钥（留空自动生成并持久化到 data/.auth_secret）
    auth_secret: str = ""
    # 令牌有效期（小时）
    auth_token_expire_hours: int = 24 * 7

    # ===== 文件存储 =====
    upload_dir: str = "./data/uploads"
    output_dir: str = "./data/outputs"
    max_upload_size_mb: int = 50

    # ===== 模型切换 =====
    def get_model(self, task: Literal["default", "complex"] = "default") -> str:
        """按任务类型返回模型名。

        - default：日常 4 阶流水线
        - complex：复杂推理（跨片冲突判定等）
        """
        return self.llm_model_pro if task == "complex" else self.llm_model

    @property
    def is_dev(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    """单例配置，避免重复读取 .env。"""
    return Settings()
