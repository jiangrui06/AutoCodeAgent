"""应用配置中心。

配置优先级：系统环境变量 > 项目目录下的 .env > 代码默认值。
通用的 LLM_* 变量优先，同时兼容旧版 SILICONFLOW_* 变量。
"""

from pathlib import Path
import sys

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"

# LangChain/LangSmith 直接读取 os.environ。override=False 确保外部注入的变量优先。
load_dotenv(dotenv_path=ENV_FILE, override=False)


class Settings(BaseSettings):
    """应用配置；未配置 API 时也允许导入模块，调用 LLM 前再明确报错。"""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # OpenAI 兼容接口配置
    llm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_API_KEY", "SILICONFLOW_API_KEY"),
    )
    llm_base_url: str = Field(
        default="https://api.siliconflow.cn/v1",
        validation_alias=AliasChoices("LLM_BASE_URL", "SILICONFLOW_BASE_URL"),
    )
    llm_model: str = Field(
        default="deepseek-ai/DeepSeek-V4-Pro",
        validation_alias=AliasChoices("LLM_MODEL", "SILICONFLOW_MODEL"),
    )
    llm_temperature: float = Field(default=0.1, alias="LLM_TEMPERATURE")
    llm_max_tokens: int = Field(default=8192, alias="LLM_MAX_TOKENS")
    llm_timeout: int = Field(default=300, alias="LLM_TIMEOUT")
    llm_disable_reasoning: bool = Field(default=False, alias="LLM_DISABLE_REASONING")

    # Agent / 执行配置
    agent_max_retry: int = Field(default=5, ge=1, le=20, alias="AGENT_MAX_RETRY")
    sandbox_timeout: int = Field(default=15, ge=1, le=600, alias="SANDBOX_TIMEOUT")
    agent_engine: str = Field(default="legacy", alias="AGENT_ENGINE")
    openhands_max_iterations: int = Field(
        default=20,
        ge=1,
        le=200,
        alias="OPENHANDS_MAX_ITERATIONS",
    )
    openhands_workspace_dir: Path = Field(
        default=PROJECT_DIR / "auto_generated_code",
        alias="OPENHANDS_WORKSPACE_DIR",
    )
    openhands_persistence_dir: Path | None = Field(
        default=None,
        alias="OPENHANDS_PERSISTENCE_DIR",
    )
    openhands_python: Path | None = Field(default=None, alias="OPENHANDS_PYTHON")
    agent_execution_python: Path | None = Field(
        default=None,
        alias="AGENT_EXECUTION_PYTHON",
    )
    openhands_worker_timeout: int = Field(
        default=1800,
        ge=30,
        le=7200,
        alias="OPENHANDS_WORKER_TIMEOUT",
    )
    openhands_vision_models: str = Field(
        default="sensenova-6.7-flash-lite",
        alias="OPENHANDS_VISION_MODELS",
    )

    # Web 配置
    web_server_name: str = Field(default="127.0.0.1", alias="WEB_SERVER_NAME")
    web_server_port: int = Field(default=7870, ge=1, le=65535, alias="WEB_SERVER_PORT")
    web_inbrowser: bool = Field(default=True, alias="WEB_INBROWSER")
    web_max_upload_mb: int = Field(default=10, ge=1, le=100, alias="WEB_MAX_UPLOAD_MB")

    # 长期记忆 / Obsidian
    memory_enabled: bool = Field(default=True, alias="MEMORY_ENABLED")
    memory_dir: Path = Field(
        default=Path.home() / "Documents" / "AutoCodeAgent-Memory",
        alias="MEMORY_DIR",
    )
    memory_recall_limit: int = Field(default=12, ge=1, le=50, alias="MEMORY_RECALL_LIMIT")
    error_memory_recall_limit: int = Field(
        default=3,
        ge=1,
        le=10,
        alias="ERROR_MEMORY_RECALL_LIMIT",
    )

    # LangSmith 可观测性
    langchain_tracing_v2: bool = Field(default=False, alias="LANGCHAIN_TRACING_V2")
    langchain_api_key: str = Field(default="", alias="LANGCHAIN_API_KEY")
    langchain_project: str = Field(default="autocode-agent", alias="LANGCHAIN_PROJECT")

    @property
    def base_url(self) -> str:
        return self.llm_base_url.rstrip("/")

    @property
    def is_llm_configured(self) -> bool:
        placeholders = ("your_", "sk-xxx", "here")
        key = self.llm_api_key.strip().lower()
        return bool(key) and not any(value in key for value in placeholders)

    @property
    def effective_openhands_persistence_dir(self) -> Path:
        return self.openhands_persistence_dir or self.memory_dir / "OpenHands会话"

    @property
    def effective_openhands_python(self) -> Path:
        if self.openhands_python:
            return self.openhands_python.expanduser()
        executable = "python.exe" if sys.platform == "win32" else "python"
        sibling_runtime = (
            PROJECT_DIR.parent.parent
            / "software-agent-sdk"
            / ".venv"
            / ("Scripts" if sys.platform == "win32" else "bin")
            / executable
        )
        return sibling_runtime if sibling_runtime.exists() else Path(sys.executable)

    @property
    def effective_agent_execution_python(self) -> Path:
        """OpenHands TerminalTool 运行和安装项目依赖时使用的解释器。"""
        if self.agent_execution_python:
            return self.agent_execution_python.expanduser()
        executable = "python.exe" if sys.platform == "win32" else "python"
        project_runtime = (
            PROJECT_DIR.parent
            / ".venv"
            / ("Scripts" if sys.platform == "win32" else "bin")
            / executable
        )
        return project_runtime if project_runtime.exists() else Path(sys.executable)

    def validate_llm_config(self) -> None:
        missing = []
        if not self.is_llm_configured:
            missing.append("LLM_API_KEY")
        if not self.base_url:
            missing.append("LLM_BASE_URL")
        if not self.llm_model.strip():
            missing.append("LLM_MODEL")
        if missing:
            names = ", ".join(missing)
            raise RuntimeError(
                f"LLM 配置不完整：{names}。请复制 .env.example 为 .env，"
                "填入 OpenAI 兼容服务的真实配置后重试。"
            )


settings = Settings()
