"""AutoCodeAgent Gradio 网页交互入口

可视化界面，输入需求一键运行 Agent，实时查看每轮调试过程。

启动：
    python app_web.py
"""

import ctypes
from html import escape
import inspect
import os
import socket
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import warnings
import webbrowser
from pathlib import Path

import gradio as gr

from attachment_manager import (
    ALLOWED_EXTENSIONS,
    AttachmentValidationError,
    build_attachment_context,
    prepare_attachments,
)
from config import ENV_FILE, settings
from code_scanner import scan_code
from dependency_manager import (
    EXECUTION_PERMISSION_PREFIX,
    INSTALL_CONTEXT_PREFIX,
    PERMISSION_LEVEL_ASK,
    PERMISSION_LEVEL_CHOICES,
    PERMISSION_LEVEL_RESTRICTED,
    code_fingerprint,
    create_execution_permission_context,
    create_install_context,
    decide_permission_action,
    detect_missing_dependency,
    inspect_code_permissions,
    install_dependency,
    is_execution_permission_approved,
    is_execution_permission_denied,
    is_install_approved,
    is_install_denied,
    normalize_permission_level,
    parse_execution_permission_context,
    parse_install_context,
)
from file_util import get_all_generated_files, save_code_to_file
from graph_nodes import (
    coder_node,
    diagnose_failure_node,
    executor_node,
    fixer_node,
    planner_node,
)
from logger import logger
from memory_store import get_memory_store
from openhands_adapter import (
    OPENHANDS_PERMISSION_PREFIX,
    execute_openhands_task,
    normalize_agent_engine,
    parse_openhands_permission_context,
)
from request_router import route_user_request
from state_model import CodeAgentState

# ── 常量 ──
SEPARATOR = "-" * 50
EMPTY_OUTPUT = """
<div class="empty-state" role="status">
  <div class="empty-state__mark" aria-hidden="true"></div>
  <p class="empty-state__eyebrow">工作台已就绪</p>
  <h2>从一句话开始</h2>
  <p>可以直接聊天，也可以描述一个明确的开发任务。我会先判断意图，需要时再向你追问。</p>
</div>
"""


def _apply_updates(state: CodeAgentState, updates: dict) -> CodeAgentState:
    """将节点返回的 dict（部分更新）应用到状态对象"""
    for key, value in updates.items():
        setattr(state, key, value)
    return state


def _execution_failed(state: CodeAgentState) -> bool:
    """兼容真实子进程结果和测试/旧会话中未提供退出码的状态。"""
    if state.exec_exit_code is not None and state.exec_exit_code != 0:
        return True
    return bool(state.exec_stderr.strip())


def _failure_evidence(state: CodeAgentState) -> str:
    """组合修复器和错误经验真正需要的退出码、stdout 与 stderr。"""
    parts = [
        f"[ExitCode {state.exec_exit_code if state.exec_exit_code is not None else 'unknown'}]"
    ]
    if state.exec_stdout.strip():
        parts.append(f"[CapturedStdout]\n{state.exec_stdout.rstrip()}")
    if state.exec_stderr.strip():
        parts.append(f"[CapturedStderr]\n{state.exec_stderr.rstrip()}")
    return "\n\n".join(parts)


def _set_windows_clipboard(text: str) -> str:
    """使用 Windows API 将 Unicode 文本写入剪贴板，避免 clip 命令 GBK 乱码。"""
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool

    if not user32.OpenClipboard(0):
        return "❌ 无法打开剪贴板，可能被其他程序占用。"
    try:
        if not user32.EmptyClipboard():
            return "❌ 无法清空剪贴板。"

        text_w = (text + "\0").encode("utf-16-le")
        size = len(text_w) + 2  # 保留 UTF-16 结束符

        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h_mem:
            return "❌ 无法分配剪贴板内存。"

        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            return "❌ 无法锁定剪贴板内存。"

        ctypes.memmove(ptr, text_w, len(text_w))
        kernel32.GlobalUnlock(h_mem)

        if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
            return "❌ 无法写入剪贴板数据。"
        return ""
    finally:
        user32.CloseClipboard()


def copy_code_to_clipboard(code: str) -> str:
    """把代码复制到系统剪贴板。Windows 使用原生 Unicode API，防止中文乱码。"""
    if not code or not code.strip():
        return "⚠️ 还没有可复制的代码。请先发送一个编码任务。"
    try:
        if sys.platform == "win32":
            error = _set_windows_clipboard(code)
            if error:
                return error
            return "✅ 代码已复制到剪贴板，现在可以直接粘贴到编辑器里。"
        # 非 Windows 回退到 clip 命令（可能不支持）
        proc = subprocess.run(
            ["clip"],
            input=code,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
        )
        if proc.returncode == 0:
            return "✅ 代码已复制到剪贴板，现在可以直接粘贴到编辑器里。"
        return f"❌ 复制失败：{proc.stderr}"
    except FileNotFoundError:
        return "❌ 复制失败：当前系统不支持 clip 命令。"
    except Exception as e:
        return f"❌ 复制失败：{type(e).__name__}: {e}"


def run_saved_code(file_path: str) -> str:
    """重新运行已经保存的生成文件，并返回输出。"""
    if not file_path:
        return "⚠️ 还没有可运行的生成文件。请先发送一个编码任务。"
    path = Path(file_path)
    if not path.exists():
        return (
            f"⚠️ 找不到生成文件：\n`{file_path}`\n\n"
            "请确认任务已经正常完成，且文件未被删除。"
        )
    if not path.is_file():
        return f"⚠️ 路径不是文件：\n`{file_path}`"
    try:
        code = path.read_text(encoding="utf-8")
        command = [sys.executable, "-I", "-X", "utf8", str(path)]
        uses_gui_self_test = "--autocode-self-test" in code
        if uses_gui_self_test:
            command.append("--autocode-self-test")
        child_env = os.environ.copy()
        if uses_gui_self_test:
            child_env.setdefault("QT_QPA_PLATFORM", "offscreen")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.sandbox_timeout,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=child_env,
        )
        lines = [
            (
                f"🧪 正在运行 GUI 非阻塞自检：`python \"{file_path}\" "
                "--autocode-self-test`"
                if uses_gui_self_test
                else f"🚀 正在运行：`python \"{file_path}\"`"
            ),
            f"🐍 解释器：`{sys.executable}`",
        ]
        if result.stdout:
            lines.append(f"### 标准输出\n```text\n{result.stdout.rstrip()}\n```")
        if result.stderr:
            lines.append(f"### 错误输出\n```text\n{result.stderr.rstrip()}\n```")
        if result.returncode == 0 and not result.stderr:
            lines.insert(1, "✅ 运行成功")
        else:
            lines.insert(1, f"⚠️ 运行结束（退出码 {result.returncode}）")
        return "\n\n".join(lines)
    except subprocess.TimeoutExpired:
        return f"❌ 运行超时：超过 {settings.sandbox_timeout} 秒"
    except Exception as e:
        return f"❌ 运行失败：{type(e).__name__}: {e}\n\n路径：`{file_path}`\n解释器：`{sys.executable}`"


def format_output(state: CodeAgentState, save_path: str = "") -> str:
    """将最终状态格式化为可读文本，并附带保存/测试提示。"""
    stdout = state.exec_stdout or "（无输出）"
    stderr = state.exec_stderr or ""
    retries = state.retry_times

    lines = ["## 执行结果"]
    if stdout:
        lines.append(f"### 程序输出\n```text\n{stdout.rstrip()}\n```")
    if stderr:
        lines.append(f"### 错误信息\n```text\n{stderr.rstrip()}\n```")
    lines += [
        f"- 重试次数：`{retries} / {state.max_retry}`",
        f"- 任务状态：**{'完成' if not stderr else '仍有未修复错误'}**",
    ]
    if save_path:
        lines.append(f"- 保存路径：`{save_path}`")
        lines.append(f"- 测试命令：在终端里运行 `python \"{save_path}\"`")
    lines += ["## 最终代码", f"```python\n{state.code.rstrip()}\n```"]
    return "\n".join(lines)


def reset_conversation() -> tuple[str, str, str, str, str, str]:
    """开始新对话并清空当前输入、输出及会话状态。"""
    return "", EMPTY_OUTPUT, "", "", "", ""


def _clear_attachments() -> None:
    """任务提交或新对话后清空浏览器中的临时附件选择。"""
    return None


def _format_permission_items(report, security_report) -> str:
    lines: list[str] = []
    for dependency in report.missing_dependencies:
        package = dependency.package or "未审核，不能自动安装"
        lines.append(f"- **依赖安装**：`{dependency.module}` → `{package}`")
    for capability in report.capabilities:
        lines.append(f"- **工具权限**：{capability.label} — {capability.detail}")
    if not security_report.is_safe:
        lines.append(
            f"- **安全扫描**：发现 {len(security_report.findings)} 项风险，"
            f"最高等级 `{security_report.max_risk}`"
        )
    return "\n".join(lines) or "- 本次代码不需要额外权限"


def _permission_block_guidance(permission_level: str, reason: str) -> str:
    """返回与当前权限等级一致的阻断说明，避免要求用户重复切换模式。"""
    if permission_level == PERMISSION_LEVEL_RESTRICTED:
        return (
            f"{reason}。受限模式不会申请安装依赖或敏感工具权限；"
            "如需继续，请切换到“询问模式”后重新提交。"
        )
    return (
        f"{reason}。系统无法安全确定未审核模块对应的官方安装包，因此不能生成安装授权；"
        "请先将导入名与官方包名加入依赖白名单后重试。"
    )


def _pending_permission_response(pending_context: str, approved: bool) -> str:
    """把可见权限按钮转换为原有的、可审计的明确授权语句。"""
    if parse_execution_permission_context(pending_context):
        return "允许执行" if approved else "拒绝执行"
    if parse_openhands_permission_context(pending_context):
        return "允许执行" if approved else "拒绝执行"
    if parse_install_context(pending_context):
        return "允许安装" if approved else "取消安装"
    return ""


def _permission_actions_update(pending_context: str):
    """仅在存在有效待确认上下文时显示批准/拒绝按钮。"""
    visible = bool(_pending_permission_response(pending_context, approved=True))
    return gr.update(visible=visible)


def _restore_browser_permission_state(pending_context: str):
    """页面刷新后恢复有效的待审批卡片，但绝不自动批准。"""
    openhands_request = parse_openhands_permission_context(pending_context)
    execution_request = parse_execution_permission_context(pending_context)
    install_request = parse_install_context(pending_context)
    if openhands_request:
        details = "\n".join(
            f"- {item}" for item in openhands_request.action_summaries
        )
        output = (
            "## 已恢复待确认的工具操作\n\n"
            f"{details}\n\n"
            "> 页面刷新没有批准任何操作；请核对后点击允许或拒绝。"
        )
    elif execution_request:
        report = inspect_code_permissions(execution_request.code)
        security_report = scan_code(execution_request.code)
        output = (
            "## 已恢复待确认的代码执行\n\n"
            f"{_format_permission_items(report, security_report)}\n\n"
            "> 页面刷新没有批准代码；请核对后点击允许或拒绝。"
        )
    elif install_request:
        output = (
            "## 已恢复待确认的依赖安装\n\n"
            f"- 模块：`{install_request.module}`\n"
            f"- 安装包：`{install_request.package}`\n"
            f"- 环境：`{settings.effective_agent_execution_python}`\n\n"
            "> 页面刷新没有安装任何内容；请核对后点击允许或拒绝。"
        )
    else:
        output = EMPTY_OUTPUT
    return output, _permission_actions_update(pending_context)


def _trusted_approval_covers(state, permission_report, security_report) -> bool:
    """信任模式下复用用户已批准的权限范围，但拒绝任何能力升级。"""
    if not state.approved_code_hash or permission_report.missing_dependencies:
        return False
    requested_capabilities = {item.key for item in permission_report.capabilities}
    requested_findings = {item.title for item in security_report.findings}
    return requested_capabilities.issubset(set(state.approved_capabilities)) and (
        requested_findings.issubset(set(state.approved_security_findings))
    )


def _respond_to_pending_permission(
    pending_context: str,
    session_id: str,
    permission_level: str,
    approved: bool,
    current_output: str = EMPTY_OUTPUT,
    current_code: str = "",
    current_path: str = "",
):
    response = _pending_permission_response(pending_context, approved)
    if not response:
        logger.info("忽略过期的权限按钮点击: session_id={}", session_id)
        yield (
            current_output or EMPTY_OUTPUT,
            "",
            session_id,
            "",
            current_code,
            current_path,
        )
        return
    yield from run_agent(response, pending_context, session_id, permission_level)


def approve_pending_permission(
    pending_context: str,
    session_id: str,
    permission_level: str,
    current_output: str = EMPTY_OUTPUT,
    current_code: str = "",
    current_path: str = "",
):
    yield from _respond_to_pending_permission(
        pending_context,
        session_id,
        permission_level,
        approved=True,
        current_output=current_output,
        current_code=current_code,
        current_path=current_path,
    )


def deny_pending_permission(
    pending_context: str,
    session_id: str,
    permission_level: str,
    current_output: str = EMPTY_OUTPUT,
    current_code: str = "",
    current_path: str = "",
):
    yield from _respond_to_pending_permission(
        pending_context,
        session_id,
        permission_level,
        approved=False,
        current_output=current_output,
        current_code=current_code,
        current_path=current_path,
    )


def _is_read_only_attachment_request(requirement: str) -> bool:
    """Return whether an attachment request only needs content analysis.

    Read-only attachment analysis must not inherit tool permissions from a
    previous coding task.  Explicit negative phrases are removed before the
    mutation-word check so that requests such as ``不要修改文件`` stay read-only.
    """
    normalized = requirement.casefold()
    for phrase in (
        "不要修改",
        "无需修改",
        "不修改",
        "请勿修改",
        "不要编辑",
        "无需编辑",
        "不要写入",
        "只读",
        "do not modify",
        "don't modify",
        "without modifying",
        "do not edit",
        "read-only",
    ):
        normalized = normalized.replace(phrase, "")

    read_markers = (
        "读取",
        "阅读",
        "概括",
        "总结",
        "分析",
        "检查",
        "解释",
        "查看",
        "看看",
        "内容",
        "是什么",
        "识别",
        "提取",
        "read",
        "summar",
        "analy",
        "review",
        "inspect",
        "explain",
        "what is",
    )
    mutation_markers = (
        "修改",
        "编辑",
        "改写",
        "润色",
        "优化",
        "保存",
        "写入",
        "删除",
        "移动",
        "重命名",
        "转换",
        "生成文件",
        "创建",
        "编写",
        "实现",
        "修复",
        "运行",
        "执行",
        "安装",
        "开发",
        "更新",
        "rewrite",
        "edit",
        "modify",
        "save",
        "write",
        "delete",
        "move",
        "rename",
        "convert",
        "create",
        "implement",
        "fix",
        "run",
        "execute",
        "install",
        "develop",
        "update",
    )
    return any(marker in normalized for marker in read_markers) and not any(
        marker in normalized for marker in mutation_markers
    )


def run_agent(
    requirement: str,
    pending_context: str = "",
    session_id: str = "",
    permission_level: str = PERMISSION_LEVEL_ASK,
    attachments: list[str] | None = None,
):
    """主执行入口 — 逐步骤产生中间结果用于 Streaming 显示

    不使用 gr.Progress()（其 DOM overlay 会遮盖输出文字），
    改用 yield 消息头部内嵌进度指示。
    """
    try:
        if not requirement or not requirement.strip():
            yield "请输入有效内容。", pending_context, session_id, "", "", ""
            return

        user_message = requirement.strip()
        permission_level = normalize_permission_level(permission_level)
        install_request = parse_install_context(pending_context)
        execution_request = parse_execution_permission_context(pending_context)
        openhands_request = parse_openhands_permission_context(pending_context)
        stale_openhands_request = openhands_request if attachments else None
        if attachments:
            # 上传新附件代表一个新的明确请求。旧的待授权操作必须先拒绝，
            # 否则普通文档问题会被误判为上一任务的权限回复。
            install_request = None
            execution_request = None
            openhands_request = None
            pending_context = ""
        invalid_permission_context = (
            pending_context.startswith(INSTALL_CONTEXT_PREFIX) and install_request is None
        ) or (
            pending_context.startswith(EXECUTION_PERMISSION_PREFIX)
            and execution_request is None
        ) or (
            pending_context.startswith(OPENHANDS_PERMISSION_PREFIX)
            and openhands_request is None
        )
        if invalid_permission_context:
            yield (
                "## 权限确认已失效\n\n请重新提交原始开发需求，我会再次执行权限预检。",
                "",
                session_id,
                "",
                "",
                "",
            )
            return

        settings.validate_llm_config()
        if stale_openhands_request:
            try:
                execute_openhands_task(
                    stale_openhands_request.original_requirement,
                    session_id,
                    permission_level,
                    decision="reject",
                    conversation_id=stale_openhands_request.conversation_id,
                )
            except Exception as exc:
                logger.exception("取消旧 OpenHands 待授权操作失败")
                yield (
                    "## 无法切换到新附件任务\n\n"
                    f"旧操作未能安全取消（{type(exc).__name__}）。"
                    "请点击“新对话”后重新上传，旧操作不会被自动批准。",
                    "",
                    session_id,
                    "",
                    "",
                    "",
                )
                return
        memory = get_memory_store()
        if memory and not session_id:
            if openhands_request:
                session_title = openhands_request.original_requirement
            elif execution_request:
                session_title = execution_request.original_requirement
            elif install_request:
                session_title = install_request.original_requirement
            else:
                session_title = user_message
            session_id = memory.create_session(session_title)
        if memory:
            memory.add_entry(session_id, "user", user_message)
        attachment_context = ""
        prepared_attachments = ()
        if (
            attachments
            and not openhands_request
            and not execution_request
            and not install_request
        ):
            try:
                prepared_attachments = prepare_attachments(
                    attachments,
                    session_id=session_id,
                )
                attachment_context = build_attachment_context(prepared_attachments)
                if memory:
                    names = "、".join(item.original_name for item in prepared_attachments)
                    memory.add_entry(
                        session_id,
                        "system",
                        f"已安全接收用户附件：{names}",
                        "status",
                    )
            except AttachmentValidationError as exc:
                message = str(exc)
                if memory:
                    memory.add_entry(session_id, "assistant", message, "clarify")
                yield f"## 附件未接受\n\n{message}", "", session_id, "", "", ""
                return
        image_paths = [
            str(item.stored_path)
            for item in prepared_attachments
            if item.kind == "图片"
        ]
        encountered_error_signatures: list[str] = []

        if openhands_request:
            if is_execution_permission_denied(user_message):
                openhands_decision = "reject"
            elif is_execution_permission_approved(user_message):
                if permission_level == PERMISSION_LEVEL_RESTRICTED:
                    message = (
                        "当前为受限模式，不能批准工具执行。"
                        "请切换到询问模式后重新提交原始需求。"
                    )
                    if memory:
                        memory.add_entry(session_id, "assistant", message, "clarify")
                    yield f"## 权限策略已阻止执行\n\n{message}", "", session_id, "", "", ""
                    return
                openhands_decision = "approve"
            else:
                actions = "\n".join(
                    f"- {item}" for item in openhands_request.action_summaries
                )
                yield (
                    "## 等待 OpenHands 权限确认\n\n"
                    f"{actions}\n\n> 请点击下方允许或拒绝按钮。",
                    pending_context,
                    session_id,
                    "",
                    "",
                    "",
                )
                return

            result = execute_openhands_task(
                openhands_request.original_requirement,
                session_id,
                permission_level,
                decision=openhands_decision,
                conversation_id=openhands_request.conversation_id,
                expected_action_summaries=openhands_request.action_summaries,
            )
            if memory:
                memory.add_entry(
                    session_id,
                    "assistant" if result.status != "rejected" else "system",
                    result.markdown,
                    "status" if result.status != "error" else "stderr",
                    {"engine": "openhands", "status": result.status},
                )
            yield (
                result.markdown,
                result.pending_context,
                session_id,
                "",
                result.code,
                result.saved_path,
            )
            return

        resume_state = None
        if execution_request:
            permission_report = inspect_code_permissions(execution_request.code)
            security_report = scan_code(execution_request.code)
            permission_items = _format_permission_items(permission_report, security_report)
            if is_execution_permission_denied(user_message):
                message = "已拒绝本次依赖/工具权限，代码没有执行。"
                if memory:
                    memory.add_entry(session_id, "assistant", message, "clarify")
                yield f"## 已拒绝执行权限\n\n{message}", "", session_id, "", "", ""
                return
            if not is_execution_permission_approved(user_message):
                yield (
                    f"## 等待权限确认\n\n{permission_items}\n\n"
                    "> 请明确回复“允许执行”或“拒绝执行”。",
                    pending_context,
                    session_id,
                    "",
                    execution_request.code,
                    "",
                )
                return

            unknown_dependencies = [
                item.module
                for item in permission_report.missing_dependencies
                if item.package is None
            ]
            if unknown_dependencies:
                modules = "、".join(f"`{name}`" for name in unknown_dependencies)
                yield (
                    "## 未审核依赖已阻止\n\n"
                    f"以下导入没有可信的包名映射：{modules}。"
                    "为避免供应链攻击，不会根据模型输出直接执行 pip 安装。",
                    "",
                    session_id,
                    "",
                    execution_request.code,
                    "",
                )
                return

            for dependency in permission_report.missing_dependencies:
                yield (
                    f"## 正在安装 {dependency.package}\n\n"
                    "已收到本次明确授权，正在当前虚拟环境安装白名单依赖。",
                    pending_context,
                    session_id,
                    "",
                    execution_request.code,
                    "",
                )
                result = install_dependency(dependency.package)
                if memory:
                    memory.add_entry(
                        session_id,
                        "system",
                        result.message,
                        "status" if result.success else "stderr",
                    )
                if not result.success:
                    yield (
                        f"## 依赖安装失败\n\n{result.message}",
                        pending_context,
                        session_id,
                        "",
                        execution_request.code,
                        "",
                    )
                    return

            effective_requirement = (
                f"{execution_request.original_requirement}\n\n"
                "用户已明确批准本次代码所列依赖和工具权限；授权只绑定当前代码指纹。"
            )
            resume_state = CodeAgentState(
                user_requirement=effective_requirement,
                dev_plan=execution_request.dev_plan,
                code=execution_request.code,
                approved_code_hash=execution_request.code_hash,
                approved_capabilities=tuple(
                    sorted(item.key for item in permission_report.capabilities)
                ),
                approved_security_findings=tuple(
                    sorted({item.title for item in security_report.findings})
                ),
                max_retry=settings.agent_max_retry,
                error_experience_context=(
                    memory.recall_error_experiences(
                        effective_requirement,
                        execution_request.code,
                    )
                    if memory
                    else ""
                ),
            )
            pending_context = ""
        elif install_request:
            if is_install_denied(user_message):
                message = (
                    f"已取消安装 `{install_request.package}`，任务没有继续执行。"
                    "你可以重新描述需求，并指定使用已安装的标准库方案。"
                )
                if memory:
                    memory.add_entry(session_id, "assistant", message, "clarify")
                yield f"## 已取消依赖安装\n\n{message}", "", session_id, "", "", ""
                return
            if not is_install_approved(user_message):
                yield (
                    f"## 等待安装确认\n\n是否允许安装 `{install_request.package}`？\n\n"
                    "> 请明确回复“允许安装”或“取消安装”。",
                    pending_context,
                    session_id,
                    "",
                    "",
                    "",
                )
                return

            yield (
                f"## 正在安装 {install_request.package}\n\n"
                "已收到你的明确许可，正在当前项目的虚拟环境中安装二进制包。",
                pending_context,
                session_id,
                "",
                "",
                "",
            )
            install_result = install_dependency(install_request.package)
            if memory:
                memory.add_entry(
                    session_id,
                    "system",
                    install_result.message,
                    "status" if install_result.success else "stderr",
                )
            if not install_result.success:
                yield (
                    f"## 依赖安装失败\n\n{install_result.message}\n\n"
                    "> 你可以修复网络或 Python 环境后，再回复“允许安装”重试。",
                    pending_context,
                    session_id,
                    "",
                    "",
                    "",
                )
                return
            effective_requirement = (
                f"{install_request.original_requirement}\n\n"
                f"用户已明确许可安装依赖 {install_request.package}，并且安装成功。"
                "必须保持用户原始需求指定的框架，不得为了通过测试改成其他界面库或命令行实现。"
            )
            pending_context = ""
        else:
            effective_requirement = user_message
            if pending_context:
                effective_requirement = f"{pending_context}\n用户补充：{effective_requirement}"
            if attachment_context:
                effective_requirement = f"{effective_requirement}\n\n{attachment_context}"

        request_preview = " ".join(user_message.split())[:500]
        logger.info(
            "Web 收到需求: {} | 附件数={}",
            request_preview,
            len(prepared_attachments),
        )
        memory_context = memory.recall(session_id) if memory else ""
        if resume_state is None:
            decision = route_user_request(effective_requirement, memory_context)
            if memory:
                memory.remember(decision.memories, session_id)
            logger.info(f"意图路由结果: {decision.mode}")
            uses_openhands = (
                normalize_agent_engine(settings.agent_engine) == "openhands"
            )
            direct_image_agent = bool(image_paths) and uses_openhands
            direct_attachment_analysis = direct_image_agent or (
                decision.mode != "chat"
                and bool(prepared_attachments)
                and _is_read_only_attachment_request(user_message)
                and uses_openhands
            )

            if decision.mode == "chat" and not direct_attachment_analysis:
                if memory:
                    memory.add_entry(session_id, "assistant", decision.message)
                yield f"## AutoCodeAgent\n\n{decision.message}", "", session_id, "", "", ""
                return
            if decision.mode == "clarify" and not direct_attachment_analysis:
                next_context = f"{effective_requirement}\n助手追问：{decision.message}"
                if memory:
                    memory.add_entry(session_id, "assistant", decision.message, "clarify")
                yield (
                    f"## 需要你确认\n\n{decision.message}\n\n"
                    "> 直接在输入框补充回答即可，我会保留前面的需求。",
                    next_context,
                    session_id,
                    "",
                    "",
                    "",
                )
                return

            if normalize_agent_engine(settings.agent_engine) == "openhands":
                agent_requirement = effective_requirement
                if memory_context:
                    agent_requirement += (
                        "\n\n以下长期记忆只作为背景参考，不得降低安全权限：\n"
                        f"{memory_context}"
                    )
                error_experience = (
                    memory.recall_error_experiences(effective_requirement)
                    if memory
                    else ""
                )
                if error_experience and "暂无已验证" not in error_experience:
                    agent_requirement += (
                        "\n\n以下是本机已通过执行验证的历史修复经验，"
                        "先核对是否适用再使用：\n"
                        f"{error_experience}"
                    )
                yield (
                    "> 引擎 `OpenHands` · 正在自主检查、修改并测试\n\n"
                    "## 任务已交给 OpenHands\n\n"
                    "工作区已限制在配置目录；询问模式遇到工具调用会在执行前暂停。",
                    "",
                    session_id,
                    "",
                    "",
                    "",
                )
                result = execute_openhands_task(
                    agent_requirement,
                    session_id,
                    permission_level,
                    decision="start",
                    image_paths=image_paths,
                    allow_tools=not direct_attachment_analysis,
                )
                if memory:
                    memory.add_entry(
                        session_id,
                        "assistant",
                        result.markdown,
                        "stderr" if result.error else "status",
                        {
                            "engine": "openhands",
                            "status": result.status,
                            "event_count": result.event_count,
                            "saved_to": result.saved_path,
                        },
                    )
                    if result.error:
                        memory.record_error(
                            session_id,
                            effective_requirement,
                            result.code,
                            result.error,
                        )
                yield (
                    result.markdown,
                    result.pending_context,
                    session_id,
                    "",
                    result.code,
                    result.saved_path,
                )
                return

            agent_requirement = effective_requirement
            if memory_context and any(
                word in effective_requirement for word in ("之前", "上次", "继续", "记得", "我们")
            ):
                agent_requirement += f"\n\n以下是可参考的长期记忆：\n{memory_context}"
            initial_error_context = (
                memory.recall_error_experiences(effective_requirement)
                if memory
                else ""
            )
            state = CodeAgentState(
                user_requirement=agent_requirement,
                max_retry=settings.agent_max_retry,
                error_experience_context=initial_error_context,
            )
            step_index = 0

            step_index += 1
            _apply_updates(state, planner_node(state))
            if memory:
                memory.add_entry(session_id, "assistant", state.dev_plan, "plan")
            plan_text = state.dev_plan[:600] + ("..." if len(state.dev_plan) > 600 else "")
            yield (
                f"> 进度 `[{step_index}/{3 + state.max_retry * 2}]` · 正在规划\n\n"
                f"## 开发方案\n```text\n{plan_text}\n```\n\n---\n"
            ), "", session_id, "", state.code, ""

            step_index += 1
            _apply_updates(state, coder_node(state))
            if memory:
                memory.add_entry(session_id, "assistant", state.code, "code", {"retry": 0})
            code_snippet = state.code[:800] + ("..." if len(state.code) > 800 else "")
            yield (
                f"> 进度 `[{step_index}/{3 + state.max_retry * 2}]` · 正在生成代码\n\n"
                f"## 生成代码（首版）\n```python\n{code_snippet}\n```\n\n---\n"
            ), "", session_id, "", state.code, ""
        else:
            state = resume_state
            step_index = 2
            yield (
                "## 本次权限已确认\n\n"
                f"授权代码指纹：`{state.approved_code_hash[:12]}`。现在继续执行同一份代码。",
                "",
                session_id,
                "",
                state.code,
                "",
            )

        steps_total = 3 + state.max_retry * 2  # plan + code + (exec+fix)×N

        # ── Step 3-5: Executor → Judge → Fixer 循环 ──
        iteration = 0
        while iteration <= state.max_retry:
            recalled_experience_count = 0
            if memory:
                state.error_experience_context = memory.recall_error_experiences(
                    effective_requirement,
                    state.code,
                )
                recalled_experience_count = state.error_experience_context.count(
                    "[已验证经验 "
                )
                logger.info(
                    "错误经验检索完成: iteration={}, matched={}",
                    iteration,
                    recalled_experience_count,
                )
            exact_code_is_approved = bool(
                state.approved_code_hash
                and state.approved_code_hash == code_fingerprint(state.code)
            )
            if not exact_code_is_approved:
                permission_report = inspect_code_permissions(state.code)
                security_report = scan_code(state.code)
                permission_decision = decide_permission_action(
                    permission_report,
                    permission_level,
                    has_security_findings=not security_report.is_safe,
                )
                trusted_scope_reused = (
                    permission_level == "trusted"
                    and _trusted_approval_covers(
                        state,
                        permission_report,
                        security_report,
                    )
                )
                if trusted_scope_reused:
                    permission_decision = type(permission_decision)(
                        "allow",
                        "信任模式复用本任务已批准的同等或更小权限范围",
                    )
                permission_items = _format_permission_items(
                    permission_report,
                    security_report,
                )
                logger.info(
                    "权限预检: session_id={} iteration={} level={} action={} "
                    "code_hash={} dependencies={} capabilities={} security_findings={}",
                    session_id,
                    iteration,
                    permission_level,
                    permission_decision.action,
                    code_fingerprint(state.code)[:12],
                    len(permission_report.missing_dependencies),
                    len(permission_report.capabilities),
                    len(security_report.findings),
                )
                if trusted_scope_reused:
                    state.approved_code_hash = code_fingerprint(state.code)
                    logger.info(
                        "信任模式复用权限范围: session_id={} iteration={} code_hash={}",
                        session_id,
                        iteration,
                        code_fingerprint(state.code)[:12],
                    )

                if permission_decision.action == "block":
                    yield (
                        "## 权限策略已阻止执行\n\n"
                        f"{permission_items}\n\n"
                        f"> {_permission_block_guidance(permission_level, permission_decision.reason)}",
                        "",
                        session_id,
                        "",
                        state.code,
                        "",
                    )
                    return

                if permission_decision.action == "ask":
                    permission_context = create_execution_permission_context(
                        effective_requirement,
                        state.code,
                        state.dev_plan,
                    )
                    message = (
                        "代码尚未执行，检测到以下依赖或工具权限：\n\n"
                        f"{permission_items}\n\n"
                        "> 请点击下方“允许本次操作”或“拒绝并停止”；也可以回复“允许执行”或"
                        "“拒绝执行”。授权仅绑定当前代码，代码经自动修复发生变化后会再次询问。"
                    )
                    if memory:
                        memory.add_entry(session_id, "assistant", message, "clarify")
                    yield (
                        f"## 执行前权限确认\n\n{message}",
                        permission_context,
                        session_id,
                        "",
                        state.code,
                        "",
                    )
                    return

                if permission_decision.action == "auto_install":
                    for dependency in permission_report.missing_dependencies:
                        yield (
                            f"## 自动安装白名单依赖 {dependency.package}\n\n"
                            "当前权限等级为“信任模式”，正在安装已审核的二进制依赖。",
                            "",
                            session_id,
                            "",
                            state.code,
                            "",
                        )
                        result = install_dependency(dependency.package)
                        if memory:
                            memory.add_entry(
                                session_id,
                                "system",
                                result.message,
                                "status" if result.success else "stderr",
                            )
                        if not result.success:
                            yield (
                                f"## 依赖安装失败\n\n{result.message}",
                                "",
                                session_id,
                                "",
                                state.code,
                                "",
                            )
                            return

            # Executor
            step_index += 1
            _apply_updates(state, executor_node(state))
            code_hash = code_fingerprint(state.code)[:12]
            attempt_metadata = {
                "iteration": iteration,
                "exit_code": state.exec_exit_code,
                "code_hash": code_hash,
            }
            logger.info(
                "执行完成: session_id={} iteration={} exit_code={} code_hash={} "
                "stdout_chars={} stderr_chars={}",
                session_id,
                iteration,
                state.exec_exit_code,
                code_hash,
                len(state.exec_stdout),
                len(state.exec_stderr),
            )
            if memory:
                memory.add_entry(
                    session_id, "system", state.exec_stdout, "stdout", attempt_metadata
                )
                memory.add_entry(
                    session_id, "system", state.exec_stderr, "stderr", attempt_metadata
                )

            has_error = _execution_failed(state)

            if has_error and memory:
                failure_evidence = _failure_evidence(state)
                error_signature = memory.record_error(
                    session_id,
                    effective_requirement,
                    state.code,
                    failure_evidence,
                )
                if error_signature:
                    encountered_error_signatures.append(error_signature)
                    logger.warning(
                        "执行错误已写入经验库: iteration={}, signature={}",
                        iteration,
                        error_signature[:12],
                    )

            if has_error:
                msg = (
                    f"> 进度 `[{step_index}/{steps_total}]` · 第 {iteration + 1} 次执行\n\n"
                    f"## 第 {iteration + 1} 次执行结果\n"
                    f"**状态：执行失败**\n"
                    f"**经验检索：** 已读取 {recalled_experience_count} 条已验证记录\n"
                    f"**输出：**\n```\n{state.exec_stdout[:500]}\n```\n"
                    f"**错误：**\n```\n{state.exec_stderr[:500]}\n```\n\n---\n"
                )
            else:
                msg = (
                    f"> 进度 `[{step_index}/{steps_total}]` · 第 {iteration + 1} 次执行完成\n\n"
                    f"## 第 {iteration + 1} 次执行结果\n"
                    f"**状态：执行成功**\n"
                    f"**经验检索：** 已读取 {recalled_experience_count} 条已验证记录\n"
                    f"**输出：**\n```\n{state.exec_stdout[:500]}\n```\n\n---\n"
                )
            yield msg, "", session_id, "", state.code, ""

            missing_dependency = detect_missing_dependency(state.exec_stderr)
            if has_error and missing_dependency:
                if missing_dependency.package:
                    install_context = create_install_context(
                        effective_requirement,
                        missing_dependency,
                    )
                    permission_message = (
                        f"检测到当前虚拟环境缺少 `{missing_dependency.module}`。"
                        f"是否允许安装受信任包 `{missing_dependency.package}`？"
                    )
                    if memory:
                        memory.add_entry(
                            session_id,
                            "assistant",
                            permission_message,
                            "clarify",
                        )
                    yield (
                        f"## 需要安装依赖\n\n{permission_message}\n\n"
                        "> 回复“允许安装”后，我会安装依赖并从原始需求重新开始；"
                        "回复“取消安装”则停止本次任务。",
                        install_context,
                        session_id,
                        "",
                        "",
                        "",
                    )
                else:
                    message = (
                        f"检测到缺少模块 `{missing_dependency.module}`，但它不在自动安装白名单中。"
                        "为避免安装由模型输出伪造的包名，任务已暂停。"
                    )
                    if memory:
                        memory.add_entry(session_id, "assistant", message, "clarify")
                    yield f"## 无法自动安装依赖\n\n{message}", "", session_id, "", "", ""
                return

            if not has_error:
                if memory and encountered_error_signatures:
                    resolved_signatures = tuple(
                        dict.fromkeys(encountered_error_signatures)
                    )
                    memory.resolve_errors(resolved_signatures, state.code)
                    logger.info(
                        "错误经验已由成功执行验证: count={}",
                        len(resolved_signatures),
                    )
                break

            if state.retry_times >= state.max_retry:
                break

            # Fixer
            iteration += 1
            step_index += 1
            failed_code_hash = code_fingerprint(state.code)[:12]
            logger.info(
                "修复器开始: session_id={} iteration={} failed_code_hash={} "
                "exit_code={} stdout_chars={} stderr_chars={}",
                session_id,
                iteration,
                failed_code_hash,
                state.exec_exit_code,
                len(state.exec_stdout),
                len(state.exec_stderr),
            )
            yield (
                f"> 进度 `[{step_index}/{steps_total}]` · 正在分析第 {iteration} 次失败\n\n"
                "## 自动诊断中\n\n正在根据完整退出码、stdout、stderr、重点代码和历史经验定位根因……",
                "",
                session_id,
                "",
                state.code,
                "",
            )
            _apply_updates(state, diagnose_failure_node(state))
            failure_analysis = state.failure_analysis
            analysis_hash = code_fingerprint(failure_analysis)[:12]
            logger.info(
                "失败诊断完成: session_id={} iteration={} analysis_hash={} chars={}",
                session_id,
                iteration,
                analysis_hash,
                len(failure_analysis),
            )
            if memory:
                memory.add_entry(
                    session_id,
                    "assistant",
                    failure_analysis,
                    "diagnosis",
                    {
                        "iteration": iteration,
                        "failed_code_hash": failed_code_hash,
                        "analysis_hash": analysis_hash,
                    },
                )
            analysis_preview = failure_analysis[:900] + (
                "..." if len(failure_analysis) > 900 else ""
            )
            yield (
                f"> 进度 `[{step_index}/{steps_total}]` · 根因分析完成，正在生成修复\n\n"
                f"## 第 {iteration} 次失败诊断\n```text\n{analysis_preview}\n```\n\n"
                "修复器正在生成下一版完整代码……",
                "",
                session_id,
                "",
                state.code,
                "",
            )
            _apply_updates(state, fixer_node(state))
            fixed_code_hash = code_fingerprint(state.code)[:12]
            logger.info(
                "修复器结束: session_id={} iteration={} changed={} no_progress={} "
                "fixed_code_hash={}",
                session_id,
                iteration,
                fixed_code_hash != failed_code_hash,
                state.no_progress,
                fixed_code_hash,
            )
            if memory:
                memory.add_entry(
                    session_id,
                    "assistant",
                    state.code,
                    "code",
                    {
                        "retry": iteration,
                        "based_on_hash": failed_code_hash,
                        "code_hash": fixed_code_hash,
                        "no_progress": state.no_progress,
                    },
                )
            if state.no_progress:
                message = (
                    "## 已停止重复修复\n\n"
                    "修复器返回的代码与失败版本完全相同，继续执行只会重复同一错误。"
                    "本轮已提前熔断；真实错误和本次无效尝试均已保留，供后续经验检索。"
                )
                if memory:
                    memory.add_entry(session_id, "system", message, "status")
                yield message, "", session_id, "", state.code, ""
                break
            fix_snippet = state.code[:500] + ("..." if len(state.code) > 500 else "")
            yield (
                f"> 进度 `[{step_index}/{steps_total}]` · 第 {iteration} 次自动修复\n\n"
                f"## 第 {iteration} 次修复\n"
                f"**根因分析：**\n```text\n{failure_analysis[:700]}\n```\n"
                f"修复后代码：\n```python\n{fix_snippet}\n```\n\n---\n"
            ), "", session_id, "", state.code, ""

        # ── 结束 ──
        save_path = ""
        if state.code:
            save_path = save_code_to_file(state.code, phase="final")

        final = "> 状态 `完成` · 任务流程已结束\n\n# 任务完成\n\n"
        final += format_output(state, save_path)
        if save_path:
            final += f"\n\n代码已保存至：`{save_path}`"
        logger.info(f"Web 任务完成，代码保存至: {save_path}")
        if memory:
            memory.add_entry(
                session_id,
                "system",
                "任务完成" if not state.exec_stderr else "任务结束但仍有错误",
                "status",
                {"retry_times": state.retry_times, "saved_to": save_path},
            )
        yield final, "", session_id, "", state.code, save_path

    except Exception as e:
        logger.exception("Web 执行异常")
        try:
            error_memory = get_memory_store()
            if error_memory and session_id:
                error_memory.add_entry(
                    session_id, "system", f"{type(e).__name__}: {e}", "stderr"
                )
        except Exception:
            logger.exception("写入长期记忆失败")
        yield (
            f"## 执行出错\n\n"
            f"**错误类型：** `{type(e).__name__}`\n\n"
            f"**错误信息：**\n```\n{e}\n```\n\n"
            f"请查看终端日志（`logs/autocode-agent.log`）获取详细堆栈。"
        ), pending_context, session_id, "", "", ""


def list_generated_files() -> str:
    """列出所有已生成的文件"""
    files = get_all_generated_files()
    if not files:
        return "暂时还没有生成文件"
    return "\n".join(f"{i + 1}. {f}" for i, f in enumerate(files))


# ── 构建 Gradio 界面 ──
def _header_html() -> str:
    api_ready = settings.is_llm_configured
    status_class = "is-ready" if api_ready else "is-warning"
    status_text = "模型已连接" if api_ready else "等待配置"
    return f"""
    <header class="app-header">
      <div class="brand-lockup">
        <div class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 32 32" focusable="false">
            <path d="M10 8 4 16l6 8M22 8l6 8-6 8M19 5l-6 22" />
          </svg>
        </div>
        <div>
          <p class="brand-kicker">LOCAL AI DEVELOPMENT WORKSPACE</p>
          <h1>AutoCodeAgent</h1>
          <p class="brand-subtitle">先理解，再规划；能聊天，也能完成代码闭环。</p>
        </div>
      </div>
      <div class="header-status" aria-label="系统状态">
        <span class="status-badge {status_class}">
          <span class="status-dot" aria-hidden="true"></span>{status_text}
        </span>
        <span class="mode-badge">本地工作台</span>
      </div>
    </header>
    """


def _runtime_panel_html() -> str:
    model = escape(settings.llm_model)
    engine = normalize_agent_engine(settings.agent_engine)
    engine_label = "OpenHands SDK" if engine == "openhands" else "LangGraph（兼容）"
    retry_label = (
        f"{settings.openhands_max_iterations} 步"
        if engine == "openhands"
        else f"{settings.agent_max_retry} 次"
    )
    timeout_label = (
        f"Worker {settings.openhands_worker_timeout} 秒"
        if engine == "openhands"
        else f"沙箱 {settings.sandbox_timeout} 秒"
    )
    memory_state = "已开启" if settings.memory_enabled else "已关闭"
    memory_class = "is-ready" if settings.memory_enabled else "is-muted"
    return f"""
    <section class="side-card" aria-labelledby="runtime-title">
      <div class="side-card__heading">
        <p class="section-kicker">RUNTIME</p>
        <h2 id="runtime-title">运行环境</h2>
      </div>
      <dl class="runtime-list">
        <div><dt>模型</dt><dd title="{model}">{model}</dd></div>
        <div><dt>Agent 引擎</dt><dd>{engine_label}</dd></div>
        <div><dt>单轮上限</dt><dd>{retry_label}</dd></div>
        <div><dt>执行超时</dt><dd>{timeout_label}</dd></div>
        <div><dt>代码执行</dt><dd>隔离运行时</dd></div>
      </dl>
      <div class="memory-status {memory_class}">
        <span class="memory-status__dot" aria-hidden="true"></span>
        <div><strong>长期记忆 {memory_state}</strong><span>SQLite + Obsidian 同步</span></div>
      </div>
    </section>
    """


GUIDE_HTML = """
<section class="side-card guide-card" aria-labelledby="guide-title">
  <div class="side-card__heading">
    <p class="section-kicker">HOW IT WORKS</p>
    <h2 id="guide-title">使用方式</h2>
  </div>
  <ol class="guide-list">
    <li><span>01</span><div><strong>自然描述</strong><p>问候会直接回复，明确开发任务才进入编码流程。</p></div></li>
    <li><span>02</span><div><strong>逐步执行</strong><p>规划、生成、检查、运行和修复会实时显示。</p></div></li>
    <li><span>03</span><div><strong>持续记忆</strong><p>重要偏好与对话日志会同步到长期记忆库。</p></div></li>
  </ol>
</section>
"""


CUSTOM_CSS = """
:root {
    --color-bg: #020617;
    --color-surface: #0b1220;
    --color-surface-raised: #111a2c;
    --color-surface-soft: #151f32;
    --color-border: #27364e;
    --color-border-strong: #3b4d68;
    --color-text: #f8fafc;
    --color-text-soft: #cbd5e1;
    --color-text-muted: #8290a8;
    --color-accent: #22c55e;
    --color-accent-strong: #4ade80;
    --color-accent-ink: #03150a;
    --color-warning: #f59e0b;
    --color-danger: #f87171;
    --radius-sm: 8px;
    --radius-md: 12px;
    --radius-lg: 16px;
    --shadow-panel: 0 18px 48px rgba(0, 0, 0, 0.26);
}

html,
body {
    background: var(--color-bg) !important;
}

body,
.gradio-container {
    color: var(--color-text) !important;
    font-family: Inter, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif !important;
}

.gradio-container {
    color-scheme: dark;
    --background-fill-primary: var(--color-bg);
    --background-fill-secondary: var(--color-surface);
    --body-background-fill: var(--color-bg);
    --body-text-color: var(--color-text);
    --body-text-color-subdued: var(--color-text-muted);
    --block-background-fill: var(--color-surface);
    --block-border-color: var(--color-border);
    --block-label-background-fill: var(--color-surface-raised);
    --block-label-border-color: var(--color-border);
    --block-label-text-color: var(--color-text-soft);
    --block-title-text-color: var(--color-text-soft);
    --border-color-primary: var(--color-border);
    --input-background-fill: var(--color-bg);
    --input-border-color: var(--color-border);
    --panel-background-fill: var(--color-surface);
    --panel-border-color: var(--color-border);
    --code-background-fill: #050a13;
    --button-secondary-background-fill: var(--color-surface-soft);
    --button-secondary-border-color: var(--color-border);
    --button-secondary-text-color: var(--color-text-soft);
    min-height: 100vh !important;
    max-width: none !important;
    margin: 0 !important;
    padding: 0 !important;
    background:
        radial-gradient(circle at 12% -8%, rgba(34, 197, 94, 0.10), transparent 31rem),
        radial-gradient(circle at 92% 10%, rgba(56, 189, 248, 0.07), transparent 27rem),
        var(--color-bg) !important;
}

#autocode-shell {
    width: min(100%, 1480px);
    margin: 0 auto;
    padding: 28px clamp(18px, 3vw, 44px) 34px;
    gap: 22px;
}

.app-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 24px;
    padding: 4px 2px 2px;
}

.brand-lockup {
    display: flex;
    align-items: center;
    gap: 16px;
}

.brand-mark {
    display: grid;
    flex: 0 0 50px;
    width: 50px;
    height: 50px;
    place-items: center;
    color: var(--color-accent-strong);
    border: 1px solid rgba(74, 222, 128, 0.38);
    border-radius: var(--radius-md);
    background: linear-gradient(145deg, rgba(34, 197, 94, 0.16), rgba(15, 23, 42, 0.78));
    box-shadow: inset 0 1px rgba(255, 255, 255, 0.06), 0 12px 30px rgba(0, 0, 0, 0.22);
}

.brand-mark svg {
    width: 27px;
    height: 27px;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 2;
}

.brand-kicker,
.section-kicker,
.panel-kicker,
.empty-state__eyebrow {
    margin: 0 0 5px !important;
    color: var(--color-accent-strong) !important;
    font-size: 0.68rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.14em !important;
    line-height: 1.3 !important;
}

.app-header h1 {
    margin: 0 !important;
    color: var(--color-text) !important;
    font-size: clamp(1.72rem, 3vw, 2.25rem) !important;
    font-weight: 720 !important;
    letter-spacing: -0.035em !important;
    line-height: 1.08 !important;
}

.brand-subtitle {
    margin: 7px 0 0 !important;
    color: var(--color-text-muted) !important;
    font-size: 0.93rem !important;
    line-height: 1.5 !important;
}

.header-status {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
    padding-top: 4px;
}

.status-badge,
.mode-badge,
.stream-badge {
    display: inline-flex;
    min-height: 30px;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    color: var(--color-text-soft);
    border: 1px solid var(--color-border);
    border-radius: 999px;
    background: rgba(11, 18, 32, 0.82);
    font-size: 0.75rem;
    font-weight: 650;
    white-space: nowrap;
}

.status-badge.is-ready {
    color: #bbf7d0;
    border-color: rgba(34, 197, 94, 0.34);
    background: rgba(20, 83, 45, 0.25);
}

.status-badge.is-warning {
    color: #fde68a;
    border-color: rgba(245, 158, 11, 0.38);
    background: rgba(120, 53, 15, 0.24);
}

.status-dot,
.memory-status__dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
    box-shadow: 0 0 0 4px rgba(34, 197, 94, 0.09);
}

.workbench-row {
    align-items: flex-start !important;
    gap: 18px !important;
}

.main-workspace,
.side-rail {
    gap: 16px !important;
    min-width: 0 !important;
}

.surface,
.side-card,
.files-panel {
    border: 1px solid var(--color-border) !important;
    border-radius: var(--radius-lg) !important;
    background: var(--color-surface) !important;
    box-shadow: var(--shadow-panel) !important;
}

.output-surface {
    overflow: hidden;
    padding: 0 !important;
}

.panel-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 17px 20px;
    border-bottom: 1px solid var(--color-border);
    background: linear-gradient(180deg, var(--color-surface-raised), var(--color-surface));
}

.panel-heading h2,
.side-card h2 {
    margin: 0 !important;
    color: var(--color-text) !important;
    font-size: 1rem !important;
    font-weight: 680 !important;
    letter-spacing: -0.01em !important;
    line-height: 1.3 !important;
}

.output-box {
    position: relative;
    z-index: 2;
    min-height: 515px;
    max-height: 66vh;
    overflow-y: auto !important;
    padding: 24px 26px 30px !important;
    color: var(--color-text-soft) !important;
    border: 0 !important;
    background: #060b15 !important;
    scrollbar-color: var(--color-border-strong) transparent;
    scrollbar-width: thin;
}

.output-box h1,
.output-box h2,
.output-box h3 {
    color: var(--color-text) !important;
    letter-spacing: -0.02em;
}

.output-box h1 { font-size: 1.42rem !important; }
.output-box h2 { margin-top: 1.55rem !important; font-size: 1.12rem !important; }
.output-box h3 { font-size: 0.96rem !important; }

.output-box blockquote {
    margin: 0 0 18px !important;
    padding: 10px 13px !important;
    color: #bbf7d0 !important;
    border-left: 3px solid var(--color-accent) !important;
    border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
    background: rgba(20, 83, 45, 0.20) !important;
}

.output-box pre {
    border: 1px solid var(--color-border) !important;
    border-radius: var(--radius-md) !important;
    background: #050a13 !important;
    box-shadow: inset 0 1px rgba(255, 255, 255, 0.025);
}

.output-box :not(pre) > code {
    color: #86efac !important;
    border: 1px solid rgba(34, 197, 94, 0.20);
    border-radius: 5px;
    background: rgba(20, 83, 45, 0.22) !important;
}

.empty-state {
    display: grid;
    min-height: 430px;
    place-content: center;
    justify-items: center;
    max-width: 610px;
    margin: 0 auto;
    padding: 36px 24px;
    text-align: center;
}

.empty-state__mark {
    width: 48px;
    height: 48px;
    margin-bottom: 18px;
    border: 1px solid rgba(74, 222, 128, 0.30);
    border-radius: 50%;
    background:
        linear-gradient(var(--color-accent), var(--color-accent)) center / 18px 2px no-repeat,
        linear-gradient(90deg, var(--color-accent), var(--color-accent)) center / 2px 18px no-repeat,
        rgba(34, 197, 94, 0.08);
}

.empty-state h2 {
    margin: 0 0 10px !important;
    color: var(--color-text) !important;
    font-size: clamp(1.45rem, 3vw, 1.9rem) !important;
}

.empty-state > p:last-child {
    max-width: 520px;
    margin: 0 !important;
    color: var(--color-text-muted) !important;
    line-height: 1.75 !important;
}

.composer-surface {
    gap: 12px !important;
    padding: 18px !important;
}

/* Gradio Group 用 .styler/.form 的边框色填充组件间隙；在自定义卡片中会形成整块灰蓝底。 */
.composer-surface > .styler,
.composer-surface > .styler > .form {
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

#agent-input {
    border: 0 !important;
    background: transparent !important;
}

#agent-input > label > span,
#agent-input .block-info {
    margin-bottom: 8px !important;
    color: var(--color-text-soft) !important;
    font-size: 0.84rem !important;
    font-weight: 650 !important;
}

#agent-input textarea {
    min-height: 112px !important;
    padding: 15px 16px !important;
    color: var(--color-text) !important;
    caret-color: var(--color-accent-strong);
    border: 1px solid var(--color-border) !important;
    border-radius: var(--radius-md) !important;
    background: rgba(2, 6, 23, 0.66) !important;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.025) !important;
    font-size: 1rem !important;
    line-height: 1.65 !important;
    resize: vertical !important;
    transition: border-color 180ms ease, box-shadow 180ms ease, background 180ms ease;
}

#agent-input textarea::placeholder {
    color: #64748b !important;
}

#agent-input textarea:focus {
    border-color: rgba(74, 222, 128, 0.70) !important;
    background: rgba(2, 6, 23, 0.90) !important;
    box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.12) !important;
}

.composer-note {
    margin: 0 !important;
    color: var(--color-text-muted) !important;
    font-size: 0.77rem !important;
    line-height: 1.5 !important;
}

#attachment-input {
    border: 1px dashed rgba(100, 116, 139, 0.45) !important;
    border-radius: var(--radius-md) !important;
    background: rgba(8, 15, 28, 0.55) !important;
}

#attachment-input .block-info {
    color: var(--color-text-soft) !important;
    font-weight: 650 !important;
}

.action-row {
    gap: 10px !important;
}

.permission-action-row {
    margin: 0 28px 24px !important;
    padding: 14px !important;
    gap: 10px !important;
    border: 1px solid rgba(74, 222, 128, 0.30) !important;
    border-radius: 12px !important;
    background: rgba(22, 163, 74, 0.08) !important;
}

button,
.gr-button {
    min-height: 44px;
    border-radius: 10px !important;
    cursor: pointer;
    font-weight: 680 !important;
    touch-action: manipulation;
    transition: transform 160ms ease, border-color 160ms ease, background 160ms ease, box-shadow 160ms ease !important;
}

button:hover,
.gr-button:hover {
    transform: translateY(-1px);
}

button:active,
.gr-button:active {
    transform: translateY(0);
    filter: brightness(0.94);
}

button:disabled,
.gr-button:disabled {
    cursor: not-allowed;
    opacity: 0.48;
    transform: none !important;
}

#send-button {
    color: var(--color-accent-ink) !important;
    border-color: var(--color-accent) !important;
    background: var(--color-accent) !important;
    box-shadow: 0 8px 20px rgba(34, 197, 94, 0.16) !important;
}

#send-button:hover {
    border-color: var(--color-accent-strong) !important;
    background: var(--color-accent-strong) !important;
}

#new-chat-button,
#refresh-button {
    color: var(--color-text-soft) !important;
    border-color: var(--color-border) !important;
    background: var(--color-surface-soft) !important;
}

#new-chat-button:hover,
#refresh-button:hover {
    color: var(--color-text) !important;
    border-color: var(--color-border-strong) !important;
    background: #1c2940 !important;
}

button:focus-visible,
textarea:focus-visible,
[role="button"]:focus-visible {
    outline: 2px solid var(--color-accent-strong) !important;
    outline-offset: 3px !important;
}

.side-card {
    padding: 18px;
}

.side-card__heading {
    padding-bottom: 14px;
    border-bottom: 1px solid var(--color-border);
}

.runtime-list {
    display: grid;
    gap: 0;
    margin: 8px 0 14px;
}

.runtime-list > div {
    display: grid;
    grid-template-columns: minmax(76px, 0.72fr) minmax(0, 1.35fr);
    align-items: center;
    gap: 10px;
    min-height: 43px;
    border-bottom: 1px solid rgba(39, 54, 78, 0.62);
}

.runtime-list dt {
    color: var(--color-text-muted);
    font-size: 0.76rem;
}

.runtime-list dd {
    overflow: hidden;
    margin: 0;
    color: var(--color-text-soft);
    font-family: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
    font-size: 0.76rem;
    font-weight: 560;
    text-align: right;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.memory-status {
    display: flex;
    align-items: center;
    gap: 11px;
    padding: 11px 12px;
    border: 1px solid rgba(34, 197, 94, 0.25);
    border-radius: var(--radius-md);
    color: #86efac;
    background: rgba(20, 83, 45, 0.17);
}

.memory-status.is-muted {
    color: var(--color-text-muted);
    border-color: var(--color-border);
    background: rgba(15, 23, 42, 0.62);
}

.memory-status div {
    display: grid;
    gap: 2px;
}

.memory-status strong {
    color: inherit;
    font-size: 0.78rem;
}

.memory-status span:last-child {
    color: var(--color-text-muted);
    font-size: 0.69rem;
}

.permission-card {
    gap: 12px !important;
    padding: 18px !important;
}

.permission-card > .styler,
.permission-card > .styler > .form {
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}

.permission-card__intro p {
    margin: 4px 0 0 !important;
    color: var(--color-text-muted) !important;
    font-size: 0.72rem !important;
    line-height: 1.55 !important;
}

#permission-level {
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
}

#permission-level .block-info {
    color: var(--color-text-soft) !important;
    font-size: 0.78rem !important;
    font-weight: 650 !important;
}

#permission-level label {
    min-height: 44px;
    padding: 8px 10px !important;
    color: var(--color-text-soft) !important;
    border: 1px solid var(--color-border) !important;
    border-radius: 9px !important;
    background: var(--color-surface-soft) !important;
    font-size: 0.72rem !important;
    line-height: 1.35 !important;
    transition: border-color 160ms ease, background 160ms ease !important;
}

#permission-level label:hover {
    border-color: var(--color-border-strong) !important;
    background: #1c2940 !important;
}

#permission-level label.selected {
    color: #bbf7d0 !important;
    border-color: rgba(34, 197, 94, 0.42) !important;
    background: rgba(20, 83, 45, 0.24) !important;
}

#permission-level label span { color: inherit !important; }

#permission-level input {
    border-color: var(--color-border-strong) !important;
    background: var(--color-bg) !important;
    accent-color: var(--color-accent);
}

#permission-level input:checked {
    border-color: var(--color-accent) !important;
    background: var(--color-accent) !important;
}

.permission-hint {
    margin: 0 !important;
    padding-top: 10px;
    color: var(--color-text-muted) !important;
    border-top: 1px solid var(--color-border);
    font-size: 0.68rem !important;
    line-height: 1.55 !important;
}

.guide-list {
    display: grid;
    gap: 15px;
    margin: 16px 0 2px;
    padding: 0;
    list-style: none;
}

.guide-list li {
    display: grid;
    grid-template-columns: 28px 1fr;
    gap: 10px;
}

.guide-list li > span {
    padding-top: 2px;
    color: var(--color-accent-strong);
    font-family: "Cascadia Code", Consolas, monospace;
    font-size: 0.68rem;
    font-weight: 700;
}

.guide-list strong {
    color: var(--color-text-soft);
    font-size: 0.79rem;
}

.guide-list p {
    margin: 3px 0 0 !important;
    color: var(--color-text-muted) !important;
    font-size: 0.74rem !important;
    line-height: 1.55 !important;
}

.files-panel {
    overflow: hidden !important;
}

.files-panel > div:first-child {
    color: var(--color-text-soft) !important;
    background: rgba(17, 26, 44, 0.72) !important;
}

#file-list textarea {
    color: var(--color-text-soft) !important;
    border-color: var(--color-border) !important;
    background: rgba(2, 6, 23, 0.60) !important;
    font-family: "Cascadia Code", Consolas, monospace !important;
    font-size: 0.76rem !important;
}

.app-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 2px 3px 0;
    color: var(--color-text-muted);
    font-size: 0.72rem;
}

.app-footer p { margin: 0 !important; }

/* 隐藏 Gradio 内置进度 overlay，输出区域使用流式文本展示状态。 */
.progress-bar,
.progress-text,
.gr-progress,
.wrap .svelte-[class*=progress],
.progress-container {
    display: none !important;
    position: absolute !important;
    width: 0 !important;
    height: 0 !important;
    opacity: 0 !important;
    pointer-events: none !important;
    z-index: -1 !important;
}

footer { display: none !important; }

@media (max-width: 980px) {
    .workbench-row { flex-direction: column !important; }
    .main-workspace,
    .side-rail { width: 100% !important; }
    .side-rail {
        display: grid !important;
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .files-panel { grid-column: 1 / -1; }
}

@media (max-width: 768px) {
    #autocode-shell {
        width: calc(100% + 64px);
        max-width: calc(100% + 64px);
        margin-inline: -32px;
        padding: 18px 14px 26px;
        gap: 16px;
    }
    .app-header {
        align-items: stretch;
        flex-direction: column;
        gap: 16px;
    }
    .header-status { justify-content: flex-start; }
    .brand-mark {
        flex-basis: 44px;
        width: 44px;
        height: 44px;
    }
    .side-rail {
        display: flex !important;
        flex-direction: column !important;
    }
    .panel-heading { padding: 15px 16px; }
    .output-box {
        min-height: 430px;
        max-height: 58vh;
        padding: 19px 17px 24px !important;
    }
    .composer-surface { padding: 14px !important; }
    .action-row { flex-direction: column !important; }
    #send-button,
    #new-chat-button { width: 100% !important; }
    .app-footer {
        align-items: flex-start;
        flex-direction: column;
        gap: 4px;
    }
}

@media (max-width: 420px) {
    .brand-subtitle { font-size: 0.84rem !important; }
    .mode-badge { display: none; }
    .output-box { min-height: 390px; }
}

@media (prefers-reduced-motion: reduce) {
    *,
    *::before,
    *::after {
        scroll-behavior: auto !important;
        transition-duration: 0.01ms !important;
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
    }
    button:hover,
    .gr-button:hover { transform: none !important; }
}
"""

APP_THEME = gr.themes.Base()


def _launch_accepts_app_styling() -> bool:
    """Gradio 6 把 theme/css 从 Blocks 构造器迁移到了 launch。"""
    parameters = inspect.signature(gr.Blocks.launch).parameters
    return "theme" in parameters and "css" in parameters


def _create_blocks() -> gr.Blocks:
    """同时兼容 Gradio 5.50 和 6.x，且只隐藏已知迁移警告。"""
    options = {
        "title": "AutoCodeAgent — AI 开发工作台",
        "fill_width": True,
    }
    if not _launch_accepts_app_styling():
        options.update(theme=APP_THEME, css=CUSTOM_CSS)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"The 'theme' parameter in the Blocks constructor will be removed.*",
            category=DeprecationWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=r"The 'css' parameter in the Blocks constructor will be removed.*",
            category=DeprecationWarning,
        )
        return gr.Blocks(**options)


def _display_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::", "[::]"} else host


def _port_is_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    bind_host = "::" if family == socket.AF_INET6 else host
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((bind_host, port))
        return True
    except OSError:
        return False


def _is_autocodeagent_server(host: str, port: int) -> bool:
    url = f"http://{_display_host(host)}:{port}/"
    try:
        request = Request(url, headers={"User-Agent": "AutoCodeAgent-startup-probe"})
        with urlopen(request, timeout=1.5) as response:
            body = response.read(200_000)
        return response.status == 200 and b"AutoCodeAgent" in body
    except (HTTPError, URLError, OSError, TimeoutError):
        return False


def _resolve_web_launch(
    host: str,
    preferred_port: int,
    fallback_count: int = 20,
) -> tuple[int, bool]:
    """返回 ``(端口, 是否复用已运行实例)``。"""
    if _port_is_available(host, preferred_port):
        return preferred_port, False
    if _is_autocodeagent_server(host, preferred_port):
        return preferred_port, True
    for port in range(preferred_port + 1, preferred_port + fallback_count + 1):
        if _port_is_available(host, port):
            return port, False
    raise OSError(
        f"端口 {preferred_port}-{preferred_port + fallback_count} 均被占用，"
        "请修改 WEB_SERVER_PORT 后重试。"
    )


def _launch_options(port: int) -> dict:
    options = {
        "inbrowser": settings.web_inbrowser,
        "server_name": settings.web_server_name,
        "server_port": port,
        "share": False,
        "max_file_size": f"{settings.web_max_upload_mb}mb",
        "blocked_paths": [str(ENV_FILE.resolve())],
    }
    if _launch_accepts_app_styling():
        options.update(theme=APP_THEME, css=CUSTOM_CSS)
    return options


def build_demo() -> gr.Blocks:
    """构建可复用、可测试的 Gradio Web 工作台。"""
    with _create_blocks() as app:
        with gr.Column(elem_id="autocode-shell"):
            gr.HTML(_header_html(), padding=False, elem_classes="header-component")

            with gr.Row(elem_classes="workbench-row"):
                with gr.Column(scale=4, min_width=560, elem_classes="main-workspace"):
                    with gr.Group(elem_classes=["surface", "output-surface"]):
                        gr.HTML(
                            """
                            <div class="panel-heading">
                              <div><p class="panel-kicker">AGENT STREAM</p><h2>运行过程与结果</h2></div>
                              <span class="stream-badge">实时输出</span>
                            </div>
                            """,
                            padding=False,
                        )
                        output_box = gr.Markdown(
                            value=EMPTY_OUTPUT,
                            show_label=False,
                            sanitize_html=True,
                            elem_classes="output-box",
                            elem_id="agent-output",
                        )
                        with gr.Row(
                            visible=False,
                            elem_classes="permission-action-row",
                            elem_id="permission-actions",
                        ) as permission_action_row:
                            approve_permission_btn = gr.Button(
                                "允许本次操作",
                                variant="primary",
                                elem_id="approve-permission-button",
                            )
                            deny_permission_btn = gr.Button(
                                "拒绝并停止",
                                variant="secondary",
                                elem_id="deny-permission-button",
                            )

                    with gr.Group(elem_classes=["surface", "composer-surface"]):
                        req_input = gr.Textbox(
                            label="告诉 AutoCodeAgent 你想做什么",
                            lines=4,
                            max_lines=10,
                            placeholder="例如：你好；或“写一个可以批量整理 CSV 的 Python 工具”",
                            autofocus=True,
                            elem_id="agent-input",
                            show_copy_button=False,
                        )
                        gr.HTML(
                            '<p class="composer-note">普通聊天不会触发写代码；需求不明确时，我会先向你确认。按 Enter 即可发送。</p>',
                            padding=False,
                        )
                        attachment_input = gr.File(
                            label="上传图片或文件",
                            file_count="multiple",
                            file_types=sorted(ALLOWED_EXTENSIONS),
                            type="filepath",
                            height=116,
                            elem_id="attachment-input",
                        )
                        gr.HTML(
                            '<p class="composer-note">最多 5 个文件；单个不超过 10 MB。支持常见图片、文本、PDF、DOCX 和 XLSX，附件会复制到项目专用目录。</p>',
                            padding=False,
                        )
                        with gr.Row(elem_classes="action-row"):
                            submit_btn = gr.Button(
                                "发送",
                                variant="primary",
                                size="lg",
                                scale=2,
                                elem_id="send-button",
                            )
                            clear_btn = gr.Button(
                                "新对话",
                                variant="secondary",
                                size="lg",
                                scale=1,
                                elem_id="new-chat-button",
                            )
                        with gr.Row(elem_classes="action-row"):
                            copy_btn = gr.Button(
                                "复制最终代码",
                                variant="secondary",
                                size="lg",
                                scale=1,
                                elem_id="copy-button",
                            )
                            run_btn = gr.Button(
                                "运行生成的代码",
                                variant="secondary",
                                size="lg",
                                scale=1,
                                elem_id="run-button",
                            )
                        status_box = gr.Textbox(
                            label="操作反馈",
                            lines=4,
                            max_lines=6,
                            interactive=False,
                            placeholder="复制/运行代码的结果会显示在这里",
                            elem_id="status-box",
                            show_copy_button=True,
                        )

                with gr.Column(scale=1, min_width=260, elem_classes="side-rail"):
                    with gr.Group(elem_classes=["side-card", "permission-card"]):
                        gr.HTML(
                            """
                            <div class="side-card__heading permission-card__intro">
                              <p class="panel-kicker">PERMISSIONS</p>
                              <h2>权限控制</h2>
                              <p>依赖安装和敏感工具会在代码执行前完成预检。</p>
                            </div>
                            """,
                            padding=False,
                        )
                        permission_level = gr.Radio(
                            choices=list(PERMISSION_LEVEL_CHOICES),
                            value=PERMISSION_LEVEL_ASK,
                            label="权限等级",
                            elem_id="permission-level",
                        )
                        gr.HTML(
                            '<p class="permission-hint">信任模式首次确认后可在同等或更小权限范围内自主修复；新增依赖、工具或风险类型仍会再次询问。</p>',
                            padding=False,
                        )
                    gr.HTML(_runtime_panel_html(), padding=False)
                    gr.HTML(GUIDE_HTML, padding=False)
                    with gr.Accordion(
                        "生成文件",
                        open=False,
                        elem_classes="files-panel",
                    ):
                        file_list = gr.Textbox(
                            value=list_generated_files,
                            label="生成文件列表",
                            lines=7,
                            interactive=False,
                            elem_id="file-list",
                        )
                        refresh_btn = gr.Button(
                            "刷新文件",
                            variant="secondary",
                            elem_id="refresh-button",
                        )

            gr.HTML(
                """
                <div class="app-footer">
                  <p>AutoCodeAgent · Local-first development agent</p>
                  <p>聊天 · 澄清 · 规划 · 编码 · 执行 · 自动修复</p>
                </div>
                """,
                padding=False,
            )

        pending_context = gr.BrowserState(
            "",
            storage_key="autocodeagent-pending-permission-v1",
        )
        session_id = gr.BrowserState(
            "",
            storage_key="autocodeagent-session-v1",
        )
        code_state = gr.State("")
        path_state = gr.State("")

        app.load(
            fn=_restore_browser_permission_state,
            inputs=[pending_context],
            outputs=[output_box, permission_action_row],
            queue=False,
            show_progress="hidden",
        )

        submit_event = submit_btn.click(
            fn=run_agent,
            inputs=[req_input, pending_context, session_id, permission_level, attachment_input],
            outputs=[output_box, pending_context, session_id, req_input, code_state, path_state],
            concurrency_limit=1,
        )
        submit_event.then(
            fn=_clear_attachments,
            inputs=[],
            outputs=[attachment_input],
            queue=False,
            show_progress="hidden",
        )
        submit_event.then(
            fn=_permission_actions_update,
            inputs=[pending_context],
            outputs=[permission_action_row],
            queue=False,
            show_progress="hidden",
        )
        enter_event = req_input.submit(
            fn=run_agent,
            inputs=[req_input, pending_context, session_id, permission_level, attachment_input],
            outputs=[output_box, pending_context, session_id, req_input, code_state, path_state],
            concurrency_limit=1,
        )
        enter_event.then(
            fn=_clear_attachments,
            inputs=[],
            outputs=[attachment_input],
            queue=False,
            show_progress="hidden",
        )
        enter_event.then(
            fn=_permission_actions_update,
            inputs=[pending_context],
            outputs=[permission_action_row],
            queue=False,
            show_progress="hidden",
        )
        pending_context.change(
            fn=_permission_actions_update,
            inputs=[pending_context],
            outputs=[permission_action_row],
            queue=False,
            show_progress="hidden",
        )
        approve_event = approve_permission_btn.click(
            fn=approve_pending_permission,
            inputs=[
                pending_context,
                session_id,
                permission_level,
                output_box,
                code_state,
                path_state,
            ],
            outputs=[output_box, pending_context, session_id, req_input, code_state, path_state],
            concurrency_limit=1,
            show_progress="hidden",
        )
        approve_event.then(
            fn=_permission_actions_update,
            inputs=[pending_context],
            outputs=[permission_action_row],
            queue=False,
            show_progress="hidden",
        )
        deny_event = deny_permission_btn.click(
            fn=deny_pending_permission,
            inputs=[
                pending_context,
                session_id,
                permission_level,
                output_box,
                code_state,
                path_state,
            ],
            outputs=[output_box, pending_context, session_id, req_input, code_state, path_state],
            concurrency_limit=1,
            show_progress="hidden",
        )
        deny_event.then(
            fn=_permission_actions_update,
            inputs=[pending_context],
            outputs=[permission_action_row],
            queue=False,
            show_progress="hidden",
        )
        clear_event = clear_btn.click(
            fn=reset_conversation,
            inputs=[],
            outputs=[req_input, output_box, pending_context, session_id, code_state, path_state],
            queue=False,
            show_progress="hidden",
        )
        clear_event.then(
            fn=_clear_attachments,
            inputs=[],
            outputs=[attachment_input],
            queue=False,
            show_progress="hidden",
        )
        clear_event.then(
            fn=_permission_actions_update,
            inputs=[pending_context],
            outputs=[permission_action_row],
            queue=False,
            show_progress="hidden",
        )
        refresh_btn.click(
            fn=list_generated_files,
            inputs=[],
            outputs=[file_list],
        )
        copy_btn.click(
            fn=copy_code_to_clipboard,
            inputs=[code_state],
            outputs=[status_box],
        )
        run_btn.click(
            fn=run_saved_code,
            inputs=[path_state],
            outputs=[status_box],
        )

    return app


demo = build_demo()


def launch_web_app() -> None:
    port, reuse_existing = _resolve_web_launch(
        settings.web_server_name,
        settings.web_server_port,
    )
    url = f"http://{_display_host(settings.web_server_name)}:{port}"
    if reuse_existing:
        print(f"  AutoCodeAgent 已经在运行：{url}")
        print("  已复用现有实例，无需重复启动。")
        if settings.web_inbrowser:
            webbrowser.open(url)
        return
    if port != settings.web_server_port:
        print(
            f"  端口 {settings.web_server_port} 已被其他程序占用，"
            f"本次自动改用 {port}。"
        )
    print(f"  访问地址：{url}")
    demo.launch(**_launch_options(port))


if __name__ == "__main__":
    print("=" * 50)
    print("  AutoCodeAgent — 自动编码调试智能体")
    print("  Web 界面启动中...")
    print("=" * 50)
    launch_web_app()
