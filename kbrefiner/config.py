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
    # 主力模型：日常 4 阶流水线
    llm_model: str = "deepseek-v4-flash"
    # 备选模型：复杂推理（跨片冲突判定等）
    llm_model_pro: str = "deepseek-v4-pro"

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
