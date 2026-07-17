"""OpenAI 兼容 LLM 客户端。"""

from functools import lru_cache

from langchain_openai import ChatOpenAI

from config import settings


def _extra_body(disable_reasoning: bool) -> dict | None:
    """为支持 thinking 开关的 OpenAI 兼容服务关闭推理输出。"""
    if disable_reasoning:
        return {"thinking": {"type": "disabled"}}
    return None


@lru_cache(maxsize=1)
def get_deepseek_llm() -> ChatOpenAI:
    """延迟创建并缓存默认 LLM 客户端。"""
    settings.validate_llm_config()
    return ChatOpenAI(
        base_url=settings.base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=settings.llm_timeout,
        extra_body=_extra_body(settings.llm_disable_reasoning),
    )


def get_llm_with_config(**overrides) -> ChatOpenAI:
    """使用覆盖参数创建临时 LLM 客户端（不缓存）。"""
    api_key = overrides.get("api_key", settings.llm_api_key)
    if not api_key:
        settings.validate_llm_config()
    return ChatOpenAI(
        base_url=overrides.get("base_url", settings.base_url),
        api_key=api_key,
        model=overrides.get("model", settings.llm_model),
        temperature=overrides.get("temperature", settings.llm_temperature),
        max_tokens=overrides.get("max_tokens", settings.llm_max_tokens),
        timeout=overrides.get("timeout", settings.llm_timeout),
        extra_body=overrides.get(
            "extra_body",
            _extra_body(overrides.get("disable_reasoning", settings.llm_disable_reasoning)),
        ),
    )
