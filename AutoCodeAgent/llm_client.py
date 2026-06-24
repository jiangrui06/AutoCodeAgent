"""LLM 客户端封装 — 兼容 OpenAI 标准接口，统一复用

强制通过环境变量配置，不提供硬编码 fallback。
支持 SiliconFlow / OpenAI / 任何兼容 OpenAI 接口的服务。
"""

import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()


# ── 必需的配置键 ──
_REQUIRED_ENV_KEYS = {
    "api_key": ("SILICONFLOW_API_KEY", "API Key"),
    "base_url": ("SILICONFLOW_BASE_URL", "接口地址"),
    "model": ("SILICONFLOW_MODEL", "模型名称"),
}


def _check_config() -> dict:
    """读取并校验环境变量配置，缺少任何必需项则报错"""
    config = {
        "base_url": os.getenv("SILICONFLOW_BASE_URL", "").rstrip("/"),
        "api_key": os.getenv("SILICONFLOW_API_KEY", ""),
        "model": os.getenv("SILICONFLOW_MODEL", ""),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.1")),
        "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "8192")),
        "timeout": int(os.getenv("LLM_TIMEOUT", "300")),
    }

    missing = []
    for key, (env_name, label) in _REQUIRED_ENV_KEYS.items():
        if not config[key]:
            missing.append(f"  - {env_name}（{label}）")

    if missing:
        err = (
            "LLM 配置不完整，请在 .env 文件中设置以下环境变量：\n"
            + "\n".join(missing)
            + "\n\n参考模板：.env.example"
        )
        raise ValueError(err)

    return config


# 全局 LLM 实例（惰性加载，每次调用刷新配置）
from typing import Optional

_llm_instance: Optional[ChatOpenAI] = None


def get_deepseek_llm() -> ChatOpenAI:
    """获取 LLM 实例（缓存，单例复用）"""
    global _llm_instance
    cfg = _check_config()
    _llm_instance = ChatOpenAI(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        model=cfg["model"],
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
        timeout=cfg["timeout"],
    )
    return _llm_instance


def get_llm_with_config(**overrides) -> ChatOpenAI:
    """使用覆盖参数创建临时 LLM 实例（不缓存）"""
    cfg = _check_config()
    cfg.update(overrides)
    return ChatOpenAI(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        model=cfg["model"],
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
        timeout=cfg["timeout"],
    )
