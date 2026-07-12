"""全局配置管理 — 使用 Pydantic Settings 统一读取 .env"""

import os

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 先把 .env 加载到 os.environ，方便 LangChain/LangSmith 等第三方库自动读取
load_dotenv(override=True)


class Settings(BaseSettings):
    """应用配置，所有值均可通过 .env 覆盖"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # 允许 .env 里存在未声明的变量（如 LANGCHAIN_*）
    )

    # LLM 配置
    siliconflow_api_key: str = Field(alias="SILICONFLOW_API_KEY")
    siliconflow_base_url: str = Field(alias="SILICONFLOW_BASE_URL")
    siliconflow_model: str = Field(alias="SILICONFLOW_MODEL")

    llm_temperature: float = Field(default=0.1, alias="LLM_TEMPERATURE")
    llm_max_tokens: int = Field(default=8192, alias="LLM_MAX_TOKENS")
    llm_timeout: int = Field(default=300, alias="LLM_TIMEOUT")

    # LangSmith 可观测性（可选，自动被 LangGraph/LangChain 读取）
    langchain_tracing_v2: bool = Field(default=False, alias="LANGCHAIN_TRACING_V2")
    langchain_api_key: str = Field(default="", alias="LANGCHAIN_API_KEY")
    langchain_project: str = Field(default="autocode-agent", alias="LANGCHAIN_PROJECT")

    # 沙箱
    sandbox_timeout: int = Field(default=15, alias="SANDBOX_TIMEOUT")

    @property
    def base_url(self) -> str:
        """去除末尾斜杠的 base_url"""
        return self.siliconflow_base_url.rstrip("/")


# 全局单例
settings = Settings()
