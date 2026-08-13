"""全局配置管理。

基于 pydantic-settings 从环境变量 / .env 读取配置。
含 DeepSeek V4 模型切换逻辑：日常 4 阶流水线用 v4-flash，
复杂推理任务（如跨片冲突判定）用 v4-pro。
"""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.llm import Model


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ===== DeepSeek V4 =====
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    # 主力：日常 4 阶流水线（快、便宜、并发高）
    deepseek_model: str = Model.V4_FLASH.value
    # 备选：复杂推理（跨片冲突判定等）
    deepseek_model_pro: str = Model.V4_PRO.value

    # ===== MinerU =====
    mineru_backend: Literal["pipeline", "vlm-engine"] = "pipeline"

    # ===== 数据库 =====
    database_url: str = "sqlite+aiosqlite:///./data/kbrefiner.db"

    # ===== Redis / Celery =====
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # ===== 应用 =====
    app_env: Literal["development", "production", "test"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # ===== 文件存储 =====
    upload_dir: str = "./data/uploads"
    output_dir: str = "./data/outputs"
    max_upload_size_mb: int = 50

    # ===== DeepSeek 模型切换 =====
    def get_model(self, task: Literal["default", "complex"] = "default") -> str:
        """按任务类型返回模型名。

        - default：日常 4 阶流水线，走 v4-flash
        - complex：复杂推理（跨片冲突判定等），走 v4-pro
        """
        return self.deepseek_model_pro if task == "complex" else self.deepseek_model

    def get_model_enum(self, task: Literal["default", "complex"] = "default") -> Model:
        """返回 Model 枚举值，与 deepseek_client.py 的 Model 枚举对齐。"""
        return Model.V4_PRO if task == "complex" else Model.V4_FLASH

    @property
    def is_dev(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    """单例配置，避免重复读取 .env。"""
    return Settings()
