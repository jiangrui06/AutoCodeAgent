"""OpenHands 工具调用的确定性工作区边界分析器。

该模块只由独立 OpenHands 运行时导入，避免把 SDK 依赖带入 Gradio 主进程。
"""

from __future__ import annotations

from openhands.sdk.event import ActionEvent
from openhands.sdk.security.analyzer import SecurityAnalyzerBase
from openhands.sdk.security.risk import SecurityRisk

from openhands_adapter import (
    path_is_within_workspace,
    terminal_command_requires_confirmation,
)


class WorkspaceBoundarySecurityAnalyzer(SecurityAnalyzerBase):
    """工作区外写入、依赖变更和破坏性命令统一提升为 HIGH。"""

    workspace_root: str

    def security_risk(self, action: ActionEvent) -> SecurityRisk:
        tool_name = str(action.tool_name).lower()
        payload = action.action.model_dump() if action.action is not None else {}

        if tool_name == "file_editor":
            path = payload.get("path")
            if not path or not path_is_within_workspace(str(path), self.workspace_root):
                return SecurityRisk.HIGH
            return SecurityRisk.LOW

        if tool_name == "terminal":
            command = str(payload.get("command", ""))
            if terminal_command_requires_confirmation(command, self.workspace_root):
                return SecurityRisk.HIGH
            return SecurityRisk.LOW

        if tool_name in {"task_tracker", "think", "finish"}:
            return SecurityRisk.LOW

        # 新增或无法识别的工具默认需要用户确认。
        return SecurityRisk.HIGH
