"""LangGraph 全局状态定义 — Pydantic v2 结构化托管"""

from pydantic import BaseModel, Field


class CodeAgentState(BaseModel):
    """全流程流转状态，所有节点共享"""

    # ── 输入 ──
    user_requirement: str

    # ── 规划 / 代码 ──
    dev_plan: str = ""
    code: str = ""

    # ── 运行时 ──
    exec_stdout: str = ""
    exec_stderr: str = ""
    exec_exit_code: int | None = None
    approved_code_hash: str = ""
    approved_capabilities: tuple[str, ...] = ()
    approved_security_findings: tuple[str, ...] = ()

    # ── 重试控制 ──
    retry_times: int = Field(default=0, ge=0)
    max_retry: int = Field(default=5, ge=1, le=20)
    no_progress: bool = Field(default=False)

    # ── 流程状态 ──
    task_finish: bool = Field(default=False)

    # ── 扩展：记录每次修复的历史，方便追踪 ──
    fix_history: list[dict] = Field(default_factory=list)
    error_experience_context: str = ""
    failure_analysis: str = ""

    class Config:
        frozen = False  # LangGraph 需要可修改状态
