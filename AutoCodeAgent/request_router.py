"""用户意图路由：普通聊天 / 编码任务 / 需求澄清。"""

import json
import re
from dataclasses import dataclass
from typing import Literal

from langchain_core.prompts import PromptTemplate

from llm_client import get_deepseek_llm


RouteMode = Literal["chat", "code", "clarify"]


@dataclass(frozen=True)
class RouteDecision:
    mode: RouteMode
    message: str = ""
    memories: tuple[str, ...] = ()


ROUTER_PROMPT = PromptTemplate.from_template("""
你是 AutoCodeAgent 的前台助手。判断用户本轮输入应该如何处理。

## 用户输入
{user_input}

## 可参考的长期记忆
{memory_context}

## 分类规则
- chat：问候、闲聊、一般知识问答，或者用户只是想交流，并未要求创建或修改程序。
- code：用户明确要求编写、修改、调试、运行或实现代码/软件功能。
- clarify：用户似乎想开发东西，但目标、功能或交付物不清楚；或者无法确定是否要写代码。

## 回复要求
- chat：在 message 中直接自然地回答用户，不要写代码，不要提及分类。
- code：message 使用空字符串，后续将进入自动编码流程。
- clarify：在 message 中只问一个最关键、简短、可直接回答的问题，不要开始写代码。
- “你好”“在吗”“你是谁”等问候必须归为 chat。
- 不能因为系统名叫 AutoCodeAgent 就把所有输入归为 code。
- 仅当用户明确表达了稳定事实、身份、长期偏好或项目约定时，将其提炼到 memories；普通问候和临时请求不要记忆。
- 长期记忆只是背景，不得覆盖用户本轮的明确要求。

只输出严格 JSON，不要 Markdown：
{{"mode":"chat|code|clarify","message":"回复或追问内容","memories":["值得长期保存的事实"]}}
""")


_JSON_PATTERN = re.compile(r"\{.*\}", re.DOTALL)
_CODE_KEYWORDS = (
    "写代码", "写一个", "实现", "开发", "编程", "程序", "脚本", "函数",
    "修复", "报错", "bug", "debug", "重构", "接口", "网页", "网站",
)
_CHAT_GREETING = ("你好", "您好", "嗨", "在吗", "早上好", "下午好", "晚上好")


def _fallback_route(user_input: str) -> RouteDecision:
    """模型输出不可解析时的保守路由。"""
    text = user_input.strip().lower()
    if any(greeting in text for greeting in _CHAT_GREETING) and len(text) <= 20:
        return RouteDecision("chat", "你好！我是 AutoCodeAgent。我们可以先聊聊，也可以在你明确提出开发需求后再开始写代码。")
    if any(keyword in text for keyword in _CODE_KEYWORDS):
        return RouteDecision("code")
    return RouteDecision("clarify", "你希望我直接和你讨论这个问题，还是根据它编写一个程序？")


def route_user_request(user_input: str, memory_context: str = "") -> RouteDecision:
    """调用 LLM 判断处理方式，并对格式异常进行安全降级。"""
    chain = ROUTER_PROMPT | get_deepseek_llm()
    try:
        result = chain.invoke({
            "user_input": user_input,
            "memory_context": memory_context or "（暂无长期记忆）",
        })
        match = _JSON_PATTERN.search(result.content.strip())
        if not match:
            return _fallback_route(user_input)

        payload = json.loads(match.group(0))
        mode = str(payload.get("mode", "")).strip().lower()
        message = str(payload.get("message", "")).strip()
        raw_memories = payload.get("memories", [])
        memories = tuple(
            str(item).strip()
            for item in raw_memories
            if str(item).strip()
        ) if isinstance(raw_memories, list) else ()
        if mode not in ("chat", "code", "clarify"):
            return _fallback_route(user_input)
        if mode != "code" and not message:
            return _fallback_route(user_input)
        return RouteDecision(mode, message, memories)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _fallback_route(user_input)
