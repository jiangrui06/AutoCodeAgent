"""LangGraph 流程节点逻辑 — 规划 / 编码 / 执行 / 修复 / 路由"""

import re
import sys

from langchain_core.prompts import PromptTemplate

from code_linter import lint_code
from code_sandbox import safe_execute_code
from code_scanner import (
    FORCE_RUN_MARKER,
    ScanReport,
    confirm_execution,
    has_force_run_marker,
    scan_code,
    web_confirm,
)
from file_util import save_iteration_snapshot
from llm_client import get_deepseek_llm

llm = get_deepseek_llm()

# ════════════════════════════════════════════════════
# Prompt 模板
# ════════════════════════════════════════════════════

PLANNER_PROMPT = PromptTemplate.from_template("""
你是一位专业的 Python 后端开发规划师。请根据用户需求输出详细的开发方案。

**禁止编写任何代码**，仅输出方案描述。

## 用户需求
{user_requirement}

## 输出规范 — 包含以下 4 个部分（Markdown 格式）
1. **功能模块拆分**：将系统拆分为若干功能模块，说明各模块职责
2. **数据模型设计**：描述数据结构、类、属性、方法签名（不写实现）
3. **程序入口逻辑**：main() 函数的设计思路、交互流程
4. **测试用例**：设计至少 2 个测试场景验证功能正确性

## 约束
- 只使用 Python 内置标准库（不允许 requests / numpy / pandas 等第三方库）
- 方案要具体，模块/函数命名要有意义
""")

CODER_PROMPT = PromptTemplate.from_template("""
根据下面的开发方案，生成**完整、可独立运行**的 Python 代码。

## 开发方案
{dev_plan}

## 要求
1. 代码结构清晰，包含完整的类/函数定义
2. 包含 `def main():` 入口函数，并在文件末尾调用 `if __name__ == "__main__": main()`
3. 添加基本的异常捕获（try/except），保证程序健壮性
4. **仅使用 Python 内置标准库**，不使用任何第三方包
5. 代码要有实际交互逻辑（输入/输出/演示数据），不能只定义类
6. main() 函数中应运行演示用例，让用户看到代码的实际运行效果
7. **不要使用 `input()`** — 程序在非交互环境下运行，`input()` 会直接报错。请用内置测试数据或命令行参数替代交互输入。

## 输出格式
- 仅输出一个 ```python 代码块，不要额外文字说明
- 代码块内包含完整的可运行代码
""")

FIXER_PROMPT = PromptTemplate.from_template("""
以下 Python 代码运行报错，请定位 BUG 并修复。

## 原始开发需求
{user_requirement}

## 旧代码（含 BUG）
```python
{code}
```

## 运行报错堆栈
{exec_stderr}

## 之前的修复历史（供参考，避免重复无效尝试）
{fix_history_summary}

## 任务
1. 分析报错堆栈，定位错误行和原因
2. 参考修复历史，**不要重复已失败过的修复策略**
3. 修复逻辑错误，产生完整可运行的 Python 代码
4. **不修改原有业务需求逻辑**，仅修复报错

## ⚠️ 注意
- 代码在独立子进程中执行，标准库全部可用（包括 `open()` 文件读写）
- 但 **不要使用 `input()`** — 程序非交互运行，`input()` 会立刻报错

## 约束
- 仅使用 Python 内置标准库
- 包含 main() 入口函数
- 修复后先自行在脑中"模拟运行"一遍，确保修复正确

## 输出格式
- 仅输出一个 ```python 代码块，不要额外文字说明
- 代码块内包含完整的可运行代码
""")


# ════════════════════════════════════════════════════
# 提取代码块工具
# ════════════════════════════════════════════════════

_CODE_BLOCK_PATTERN = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_GENERIC_BLOCK_PATTERN = re.compile(r"```\s*\n(.*?)```", re.DOTALL)


def _looks_like_python(code: str) -> bool:
    """粗略判断文本是否像 Python 代码"""
    indicators = [
        "def ", "class ", "import ", "from ",
        "if __name__", "print(", "return ", "for ", "while ",
    ]
    code_lower = code.lower()
    return any(ind in code_lower for ind in indicators)


def _extract_code(text: str) -> str:
    """从 LLM 回复中提取 Python 代码块

    支持 ```python、```py、```（无语言标记）以及裸代码文本。
    """
    # 1. 主匹配：```python / ```py
    match = _CODE_BLOCK_PATTERN.search(text)
    if match:
        return match.group(1).strip()

    # 2. fallback：``` ... ```（无语言标记），取第一个看起来像 Python 的
    for block in _GENERIC_BLOCK_PATTERN.findall(text):
        block = block.strip()
        if _looks_like_python(block):
            return block

    # 3. 兜底：没有代码块，但整体看起来像 Python 裸代码
    stripped = text.strip()
    if _looks_like_python(stripped):
        return stripped

    # 4. 完全不是代码，报错
    raise ValueError(
        "LLM 输出中未找到有效的 Python 代码块，无法提取代码。"
        f"原始输出前 200 字：\n{text[:200]}"
    )


# ════════════════════════════════════════════════════
# 节点函数
# ════════════════════════════════════════════════════

def planner_node(state) -> dict:
    """1. 需求规划节点：生成开发方案"""
    chain = PLANNER_PROMPT | llm
    result = chain.invoke({"user_requirement": state.user_requirement})
    return {"dev_plan": result.content}


def coder_node(state) -> dict:
    """2. 代码生成节点：根据方案生成代码"""
    chain = CODER_PROMPT | llm
    result = chain.invoke({"dev_plan": state.dev_plan})
    try:
        raw_code = _extract_code(result.content)
    except ValueError as e:
        return {"code": "", "exec_stdout": "", "exec_stderr": str(e)}

    # 保存首版代码快照
    save_iteration_snapshot(raw_code, retry=0)

    return {"code": raw_code, "exec_stdout": "", "exec_stderr": ""}


def executor_node(state) -> dict:
    """3. 子进程执行节点：静态检查 → 安全扫描 → 隔离执行并捕获输出"""
    code = state.code

    # ── 静态检查：先抓语法/未定义名，避免直接跑子进程浪费 Token ──
    lint_ok, lint_msg = lint_code(code)
    if not lint_ok:
        return {
            "exec_stdout": "",
            "exec_stderr": f"[Lint] {lint_msg}\n\n已阻止子进程执行，请让 Agent 修复后再试。",
        }

    # ── 安全检查（除非用户标记强制运行） ──
    if not has_force_run_marker(state.user_requirement + code):
        report = scan_code(code)
        if not report.is_safe:
            if sys.stdin.isatty():
                # CLI 模式：交互确认
                if not confirm_execution(report):
                    return {
                        "exec_stdout": "",
                        "exec_stderr": f"[Scanner] 用户取消了执行。\n{report.summary}",
                    }
            else:
                # Web 模式：自动拦截，生成报告
                return {
                    "exec_stdout": "",
                    "exec_stderr": f"[Scanner] 安全扫描未通过，已阻止执行。\n{report.summary}\n\n如需强制运行，请在需求中加上「{FORCE_RUN_MARKER}」",
                }

    # ── 执行 ──
    out, err = safe_execute_code(code)

    updates = {
        "exec_stdout": out,
        "exec_stderr": err,
    }

    # 把非阻塞的 lint 警告追加到 stderr 末尾，便于查看
    if lint_msg:
        updates["exec_stderr"] = (err + "\n" + lint_msg).strip()

    return updates


def fixer_node(state) -> dict:
    """4. 代码修复节点：根据报错修复代码"""
    chain = FIXER_PROMPT | llm

    # 构建修复历史摘要（上次报错 & 修复轮次）
    fix_history_summary = "无（首次修复）"
    if state.fix_history:
        entries = []
        for h in state.fix_history:
            err_snippet = h.get("fix_based_on", "")[:200]
            entries.append(f"  第 {h['retry']} 次 → 报错：{err_snippet}")
        fix_history_summary = "\n" + "\n".join(entries)

    result = chain.invoke({
        "user_requirement": state.user_requirement,
        "code": state.code,
        "exec_stderr": state.exec_stderr,
        "fix_history_summary": fix_history_summary,
    })
    try:
        fixed_code = _extract_code(result.content)
    except ValueError as e:
        return {
            "code": state.code,  # 保留旧代码，不覆盖
            "retry_times": state.retry_times + 1,
            "exec_stdout": "",
            "exec_stderr": f"[ExtractError] 提取修复后代码失败：{e}",
            "fix_history": state.fix_history,
        }

    new_retry = state.retry_times + 1

    # 保存修复后代码快照
    save_iteration_snapshot(fixed_code, retry=new_retry)

    # 记录修复历史
    fix_record = {
        "retry": new_retry,
        "fix_based_on": state.exec_stderr[:300],  # 截取报错前 300 字符
    }

    return {
        "code": fixed_code,
        "retry_times": new_retry,
        "exec_stdout": "",
        "exec_stderr": "",
        "fix_history": state.fix_history + [fix_record],
    }


def judge_route(state) -> str:
    """5. 分支路由判断：决定流程走向"""
    has_error = bool(state.exec_stderr.strip())

    if not has_error:
        # 分支1：无报错，任务完成
        return "end_task"

    if state.retry_times >= state.max_retry:
        # 分支3：达到最大重试次数，强制结束
        return "end_task"

    # 分支2：有报错且可重试，进入修复
    return "fixer"
