"""LLM 客户端封装 — 兼容 OpenAI 标准接口，统一复用

配置统一从 config.settings 读取，支持 SiliconFlow / OpenAI / 任何兼容 OpenAI 接口的服务。
"""

from typing import Optional

from langchain_openai import ChatOpenAI

from config import settings


_llm_instance: Optional[ChatOpenAI] = None


def get_deepseek_llm() -> ChatOpenAI:
    """获取 LLM 实例（缓存，单例复用）"""
    global _llm_instance
    _llm_instance = ChatOpenAI(
        base_url=settings.base_url,
        api_key=settings.siliconflow_api_key,
        model=settings.siliconflow_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=settings.llm_timeout,
    )
    return _llm_instance


def get_llm_with_config(**overrides) -> ChatOpenAI:
    """使用覆盖参数创建临时 LLM 实例（不缓存）"""
    return ChatOpenAI(
        base_url=overrides.get("base_url", settings.base_url),
        api_key=overrides.get("api_key", settings.siliconflow_api_key),
        model=overrides.get("model", settings.siliconflow_model),
        temperature=overrides.get("temperature", settings.llm_temperature),
        max_tokens=overrides.get("max_tokens", settings.llm_max_tokens),
        timeout=overrides.get("timeout", settings.llm_timeout),
    )
