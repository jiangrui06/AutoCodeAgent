"""OpenHands Agent SDK 的可选适配层。

本模块刻意保持顶层导入轻量：只有真正选择 OpenHands 引擎时才加载 SDK，
因此旧的 LangGraph 引擎和未安装 OpenHands 的环境仍可正常启动。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable


OPENHANDS_PERMISSION_PREFIX = "openhands-permission:"
_PERMISSION_SIGNING_KEY = secrets.token_bytes(32)
_CONVERSATION_NAMESPACE = uuid.UUID("03be4838-03a6-49f6-b571-e626c77cb47f")
_WORKER_RESULT_PREFIX = "AUTOCODE_OPENHANDS_RESULT="
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class OpenHandsLLMConfig:
    model: str
    api_key: str
    base_url: str
    temperature: float
    max_output_tokens: int
    timeout: int
    litellm_extra_body: dict[str, Any] | None = None
    force_vision: bool = False


@dataclass(frozen=True)
class OpenHandsPermissionRequest:
    conversation_id: uuid.UUID
    original_requirement: str
    action_summaries: tuple[str, ...]


@dataclass(frozen=True)
class OpenHandsRunResult:
    status: str
    markdown: str
    pending_context: str = ""
    code: str = ""
    saved_path: str = ""
    error: str = ""
    event_count: int = 0
    action_summaries: tuple[str, ...] = ()


def normalize_agent_engine(value: str) -> str:
    """未知值安全回退到现有引擎，避免配置拼写错误导致启动失败。"""
    normalized = (value or "").strip().lower()
    return "openhands" if normalized == "openhands" else "legacy"


def _openai_compatible_model(model: str) -> str:
    normalized = (model or "").strip()
    if not normalized:
        raise ValueError("LLM_MODEL 不能为空")
    if normalized.startswith("openai/"):
        return normalized
    return f"openai/{normalized}"


def build_openhands_llm_config(source: Any) -> OpenHandsLLMConfig:
    """把 AutoCodeAgent 配置转换成 OpenHands/LiteLLM 的明确参数。"""
    extra_body = None
    if bool(source.llm_disable_reasoning):
        # SenseNova 推理模型若不关闭 thinking，可能只返回 reasoning_content，
        # OpenHands 因拿不到工具调用而无法继续。
        extra_body = {"thinking": {"type": "disabled"}}
    model = _openai_compatible_model(source.llm_model)
    configured_vision_models = str(
        getattr(source, "openhands_vision_models", "sensenova-6.7-flash-lite")
    )
    vision_models = {
        item.strip().lower().removeprefix("openai/")
        for item in configured_vision_models.split(",")
        if item.strip()
    }
    return OpenHandsLLMConfig(
        model=model,
        api_key=str(source.llm_api_key),
        base_url=str(source.base_url).rstrip("/"),
        temperature=float(source.llm_temperature),
        max_output_tokens=int(source.llm_max_tokens),
        timeout=int(source.llm_timeout),
        litellm_extra_body=extra_body,
        force_vision=model.lower().removeprefix("openai/") in vision_models,
    )


def _build_terminal_environment(source: Any) -> dict[str, str]:
    """让 TerminalTool 的 Python 与项目依赖安装环境保持一致。"""
    execution_python = Path(
        getattr(source, "effective_agent_execution_python", sys.executable)
    ).expanduser().resolve()
    if not execution_python.is_file():
        raise RuntimeError(
            f"代码执行 Python 不存在：{execution_python}。"
            "请在 .env 设置 AGENT_EXECUTION_PYTHON。"
        )
    inherited_path = os.environ.get("PATH", "")
    path_value = str(execution_python.parent)
    if inherited_path:
        path_value += os.pathsep + inherited_path
    return {
        "PATH": path_value,
        "AUTOCODEAGENT_PYTHON": str(execution_python),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def _image_signature_matches(data: bytes, extension: str) -> bool:
    return {
        ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": data.startswith(b"\xff\xd8\xff"),
        ".jpeg": data.startswith(b"\xff\xd8\xff"),
        ".gif": data.startswith((b"GIF87a", b"GIF89a")),
        ".bmp": data.startswith(b"BM"),
        ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }.get(extension, False)


def image_paths_to_data_urls(
    image_paths: Iterable[str | Path],
    allowed_root: str | Path,
) -> tuple[str, ...]:
    """把已验证上传目录中的图片转换为 OpenHands 支持的 data URL。"""
    root = Path(allowed_root).expanduser().resolve()
    urls: list[str] = []
    total_size = 0
    for raw_path in image_paths:
        path = Path(raw_path).expanduser().resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("图片路径不在受信任的上传目录中") from exc
        if path.is_symlink() or not path.is_file():
            raise ValueError("图片不存在或不是普通文件")
        extension = path.suffix.lower()
        mime_type = _IMAGE_MIME_TYPES.get(extension)
        if mime_type is None:
            raise ValueError(f"不支持的图片格式：{extension or '无扩展名'}")
        size = path.stat().st_size
        if size <= 0 or size > _MAX_IMAGE_BYTES:
            raise ValueError("图片为空或超过 10 MB 上限")
        total_size += size
        if total_size > 25 * 1024 * 1024:
            raise ValueError("图片总大小超过 25 MB 上限")
        data = path.read_bytes()
        if not _image_signature_matches(data[:16], extension):
            raise ValueError("图片内容与扩展名不匹配")
        encoded = base64.b64encode(data).decode("ascii")
        urls.append(f"data:{mime_type};base64,{encoded}")
    return tuple(urls)


def _build_openhands_user_message(
    requirement: str,
    image_paths: tuple[str, ...],
    source: Any,
) -> Any:
    if not image_paths:
        return requirement
    from openhands.sdk import ImageContent, Message, TextContent
    from config import PROJECT_DIR

    allowed_root = Path(
        getattr(source, "attachment_upload_root", PROJECT_DIR / "user_uploads")
    )
    image_urls = image_paths_to_data_urls(image_paths, allowed_root)
    return Message(
        role="user",
        content=[
            TextContent(text=requirement),
            ImageContent(image_urls=list(image_urls)),
        ],
    )


def session_id_to_conversation_id(session_id: str) -> uuid.UUID:
    """复用 MemoryStore 会话 ID；非 UUID 会话名则稳定映射为 UUID5。"""
    normalized = (session_id or "").strip()
    if normalized:
        try:
            return uuid.UUID(normalized)
        except ValueError:
            pass
    return uuid.uuid5(_CONVERSATION_NAMESPACE, normalized or "anonymous-session")


def _bounded_action_detail(action: Any, limit: int = 500) -> str:
    if action is None:
        return "（无可执行参数）"
    try:
        if hasattr(action, "model_dump"):
            value = json.dumps(action.model_dump(), ensure_ascii=False, default=str)
        elif hasattr(action, "__dict__"):
            value = json.dumps(vars(action), ensure_ascii=False, default=str)
        else:
            value = str(action)
    except (TypeError, ValueError):
        value = str(action)
    value = re.sub(
        r"(?i)((?:api[_-]?key|authorization|token)\s*[:=]\s*)[^,}\s]+",
        r"\1[REDACTED]",
        value,
    )
    value = " ".join(value.replace("`", "ˋ").split())
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _action_summary(action_event: Any) -> str:
    tool_name = str(getattr(action_event, "tool_name", "unknown-tool"))
    summary = str(getattr(action_event, "summary", "") or "待执行工具操作")
    risk_value = getattr(action_event, "security_risk", "UNKNOWN")
    risk = getattr(risk_value, "value", risk_value)
    risk_text = str(risk).rsplit(".", 1)[-1].upper()
    detail = _bounded_action_detail(getattr(action_event, "action", None))
    return f"`{tool_name}` · 风险 `{risk_text}` · {summary} · 参数：{detail}"


def format_pending_actions(actions: Iterable[Any]) -> str:
    summaries = tuple(_action_summary(action) for action in actions)
    if not summaries:
        return "- SDK 未返回可显示的待执行工具操作"
    return "\n".join(f"- {summary}" for summary in summaries)


def _sign_permission_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        _PERMISSION_SIGNING_KEY,
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_openhands_permission_context(
    conversation_id: uuid.UUID,
    requirement: str,
    pending_actions: Iterable[Any],
) -> str:
    """创建只对当前进程有效的防篡改授权上下文，不保存密钥或完整命令。"""
    action_summaries = tuple(_action_summary(action) for action in pending_actions)
    return _create_openhands_permission_context_from_summaries(
        conversation_id,
        requirement,
        action_summaries,
    )


def _create_openhands_permission_context_from_summaries(
    conversation_id: uuid.UUID,
    requirement: str,
    action_summaries: Iterable[str],
) -> str:
    summaries = tuple(str(item).strip()[:1200] for item in action_summaries)
    payload: dict[str, Any] = {
        "conversation_id": str(conversation_id),
        "original_requirement": (requirement or "").strip()[:8000],
        "action_summaries": list(summaries[:20]),
    }
    if not payload["original_requirement"] or not summaries or any(not item for item in summaries):
        raise ValueError("OpenHands 授权上下文必须包含原始需求和待执行操作")
    payload["signature"] = _sign_permission_payload(payload)
    return OPENHANDS_PERMISSION_PREFIX + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_openhands_permission_context(
    value: str,
) -> OpenHandsPermissionRequest | None:
    if not value or not value.startswith(OPENHANDS_PERMISSION_PREFIX):
        return None
    try:
        payload = json.loads(value[len(OPENHANDS_PERMISSION_PREFIX) :])
        signature = str(payload.pop("signature"))
        conversation_id = uuid.UUID(str(payload.get("conversation_id", "")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
        return None
    if not hmac.compare_digest(signature, _sign_permission_payload(payload)):
        return None
    requirement = str(payload.get("original_requirement", "")).strip()
    raw_summaries = payload.get("action_summaries")
    if (
        not requirement
        or len(requirement) > 8000
        or not isinstance(raw_summaries, list)
        or not 1 <= len(raw_summaries) <= 20
    ):
        return None
    summaries = tuple(str(item).strip()[:1200] for item in raw_summaries)
    if any(not item for item in summaries):
        return None
    return OpenHandsPermissionRequest(conversation_id, requirement, summaries)


_ARTIFACT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json",
    ".toml", ".yaml", ".yml", ".md", ".txt", ".csv", ".xml", ".ini",
    ".cfg", ".sql", ".sh", ".ps1",
}
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?i)(?:^|[\s'\"=])([a-z]:[\\/][^'\"\s;|]+)"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\s'\"=])(/[A-Za-z0-9_.-]+(?:/[^'\"\s;|]+)*)")
_RISKY_TERMINAL_PATTERN = re.compile(
    r"(?i)(?:"
    r"\b(?:pip|pip3)\s+install\b|\bpython(?:3)?\s+-m\s+pip\s+install\b|"
    r"\buv\s+(?:pip\s+install|add|sync)\b|\b(?:npm|pnpm|yarn)\s+(?:install|add)\b|"
    r"\bnpx\b|\b(?:remove-item|del|erase|rmdir|rd|move-item)\b|"
    r"\b(?:python|python3|powershell|pwsh)\s+(?:-c|-command)\b|\bcmd(?:\.exe)?\s+/c\b"
    r")"
)


def path_is_within_workspace(raw_path: str | Path, workspace: str | Path) -> bool:
    """解析路径后确认其仍在工作区内；不存在的路径也按最终位置判断。"""
    root = Path(workspace).expanduser().resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def terminal_command_requires_confirmation(
    command: str,
    workspace: str | Path,
) -> bool:
    """识别可逃逸工作区或会改变依赖/删除文件的终端命令。"""
    value = str(command or "")
    if not value.strip():
        return True
    if re.search(r"(?:^|[\\/])\.\.(?:[\\/]|$)", value):
        return True
    if "\\\\" in value or re.search(r"(?i)(?:\$env:|%[A-Z_][A-Z0-9_]*%)", value):
        return True
    if _RISKY_TERMINAL_PATTERN.search(value):
        return True
    absolute_paths = [match.group(1) for match in _WINDOWS_ABSOLUTE_PATH.finditer(value)]
    absolute_paths.extend(match.group(1) for match in _POSIX_ABSOLUTE_PATH.finditer(value))
    return any(not path_is_within_workspace(path, workspace) for path in absolute_paths)


def _create_conversation(
    conversation_id: uuid.UUID,
    permission_level: str,
    source: Any,
    allow_tools: bool = True,
) -> tuple[Any, Path]:
    """延迟加载 SDK 并创建可恢复的本地会话。"""
    try:
        from pydantic import SecretStr
        from openhands.sdk import Agent, Conversation, LLM, Tool
        from openhands.sdk.security import (
            AlwaysConfirm,
            ConfirmRisky,
            EnsembleSecurityAnalyzer,
            PatternSecurityAnalyzer,
            PolicyRailSecurityAnalyzer,
            SecurityRisk,
        )
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.task_tracker import TaskTrackerTool
        from openhands.tools.terminal import TerminalTool
        from openhands_workspace_security import WorkspaceBoundarySecurityAnalyzer
    except ImportError as exc:
        raise RuntimeError(
            "OpenHands 引擎尚未安装。请安装 requirements-openhands.txt 后重试。"
        ) from exc

    from config import PROJECT_DIR

    llm_config = build_openhands_llm_config(source)
    llm_kwargs: dict[str, Any] = {
        "usage_id": "agent",
        "model": llm_config.model,
        "api_key": SecretStr(llm_config.api_key),
        "base_url": llm_config.base_url,
        "temperature": llm_config.temperature,
        "max_output_tokens": llm_config.max_output_tokens,
        "timeout": llm_config.timeout,
        "num_retries": 2,
    }
    if llm_config.litellm_extra_body:
        llm_kwargs["litellm_extra_body"] = llm_config.litellm_extra_body
    force_vision = llm_config.force_vision

    class AutoCodeAgentLLM(LLM):
        def _supports_vision(self) -> bool:
            return force_vision or super()._supports_vision()

    llm = AutoCodeAgentLLM(**llm_kwargs)
    tools = [
            Tool(
                name=TerminalTool.name,
                params={"env": _build_terminal_environment(source)},
            ),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ] if allow_tools else []
    agent = Agent(llm=llm, tools=tools)

    workspace = Path(
        getattr(source, "openhands_workspace_dir", PROJECT_DIR / "auto_generated_code")
    ).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    persistence_value = getattr(source, "openhands_persistence_dir", None)
    if persistence_value is None:
        persistence_value = getattr(
            source,
            "effective_openhands_persistence_dir",
            Path(source.memory_dir) / "OpenHands会话",
        )
    persistence_root = Path(persistence_value).expanduser().resolve()
    persistence_dir = persistence_root / str(conversation_id)
    persistence_dir.mkdir(parents=True, exist_ok=True)

    conversation = Conversation(
        agent=agent,
        workspace=workspace,
        persistence_dir=persistence_dir,
        conversation_id=conversation_id,
        max_iteration_per_run=int(getattr(source, "openhands_max_iterations", 20)),
        visualizer=None,
        delete_on_close=False,
        tags={"source": "autocodeagent"},
    )
    analyzer = EnsembleSecurityAnalyzer(
        analyzers=[
            WorkspaceBoundarySecurityAnalyzer(workspace_root=str(workspace)),
            PolicyRailSecurityAnalyzer(),
            PatternSecurityAnalyzer(),
        ]
    )
    conversation.set_security_analyzer(analyzer)
    if permission_level == "trusted":
        conversation.set_confirmation_policy(
            ConfirmRisky(threshold=SecurityRisk.HIGH, confirm_unknown=True)
        )
    else:
        # 询问模式逐次确认；受限模式也不会静默执行工具，Web 层还会阻止批准。
        conversation.set_confirmation_policy(AlwaysConfirm())
    return conversation, workspace


def _pending_actions(conversation: Any) -> tuple[Any, ...]:
    explicit = getattr(conversation, "pending", None)
    if explicit is not None:
        return tuple(explicit)
    try:
        from openhands.sdk.conversation.state import ConversationState

        return tuple(ConversationState.get_unmatched_actions(conversation.state.events))
    except (ImportError, AttributeError, TypeError):
        return ()


def _analyze_pending_actions(
    conversation: Any,
    pending_actions: Iterable[Any],
) -> tuple[Any, ...]:
    """把最终确定性风险写入只用于展示/签名的事件副本。"""
    analyzer = getattr(getattr(conversation, "state", None), "security_analyzer", None)
    if analyzer is None:
        return tuple(pending_actions)
    analyzed = []
    for action in pending_actions:
        try:
            risk = analyzer.security_risk(action)
            analyzed.append(action.model_copy(update={"security_risk": risk}))
        except (AttributeError, TypeError, ValueError):
            analyzed.append(action)
    return tuple(analyzed)


def _status_value(state: Any) -> str:
    status = getattr(state, "execution_status", "error")
    return str(getattr(status, "value", status)).lower()


def _event_preview(event: Any, limit: int = 900) -> str:
    name = event.__class__.__name__
    source_value = str(getattr(event, "source", "")).lower()
    if name == "MessageEvent" and "user" in source_value:
        # requirement 内部还会拼接附件路径、长期记忆和错误经验；这些内容
        # 供模型使用，但不应原样回显到 Web 结果区。
        return "已接收用户需求（内部上下文已隐藏）"
    if hasattr(event, "tool_name"):
        tool = str(getattr(event, "tool_name", "unknown-tool"))
        if name == "ActionEvent" or hasattr(event, "action"):
            summary = str(getattr(event, "summary", "") or "执行工具")
            return f"调用 `{tool}`：{summary}"
        error = getattr(event, "error", "")
        if error:
            return f"`{tool}` 错误：{str(error)[:limit]}"
        return f"`{tool}` 返回：{str(event)[:limit]}"
    llm_message = getattr(event, "llm_message", None)
    if getattr(event, "source", None) == "agent" and llm_message is not None:
        text_parts = [
            str(item.text)
            for item in getattr(llm_message, "content", ())
            if getattr(item, "text", None)
        ]
        if text_parts:
            return "助手：" + " ".join(text_parts)[:limit]
    return str(event)[:limit]


def _format_event_history(events: Iterable[Any]) -> str:
    visible = []
    for event in events:
        preview = _event_preview(event).strip()
        if preview and "SystemPromptEvent" not in preview:
            visible.append(preview)
    return "\n".join(f"- {item}" for item in visible[-16:])


def _workspace_snapshot(workspace: Path) -> dict[Path, tuple[int, int]]:
    """记录可展示文本产物的修改时间和大小，用于限定单次运行结果。"""
    snapshot: dict[Path, tuple[int, int]] = {}
    for path in workspace.rglob("*"):
        try:
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix.lower() in _ARTIFACT_EXTENSIONS
                and path.stat().st_size <= 500_000
            ):
                stat = path.stat()
                snapshot[path.resolve()] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
    return snapshot


def _latest_artifact(
    workspace: Path,
    before: dict[Path, tuple[int, int]],
) -> tuple[str, str]:
    """只返回本轮新增或修改的产物，禁止回退到旧任务文件。"""
    after = _workspace_snapshot(workspace)
    candidates = [
        path
        for path, fingerprint in after.items()
        if before.get(path) != fingerprint
    ]
    if not candidates:
        return "", ""
    latest = max(candidates, key=lambda item: after[item][0])
    try:
        return latest.read_text(encoding="utf-8"), str(latest)
    except (OSError, UnicodeDecodeError):
        return "", str(latest)


def _execute_openhands_in_process(
    requirement: str,
    session_id: str,
    permission_level: str,
    *,
    decision: str = "start",
    conversation_id: uuid.UUID | None = None,
    expected_action_summaries: Iterable[str] | None = None,
    source: Any | None = None,
    sign_pending_context: bool = True,
    image_paths: Iterable[str | Path] = (),
    allow_tools: bool = True,
) -> OpenHandsRunResult:
    """执行、批准或拒绝一个可持久恢复的 OpenHands 会话。"""
    if source is None:
        from config import settings as source

    conversation_id = conversation_id or session_id_to_conversation_id(session_id)
    conversation, workspace = _create_conversation(
        conversation_id,
        permission_level,
        source,
        allow_tools,
    )
    artifact_snapshot = _workspace_snapshot(workspace)
    try:
        if decision == "reject":
            conversation.reject_pending_actions(
                "用户在 AutoCodeAgent 中拒绝了本次操作"
            )
            return OpenHandsRunResult(
                status="rejected",
                markdown="## 已拒绝本次工具操作\n\n待执行操作没有运行，拒绝记录已写入会话事件。",
                event_count=len(getattr(conversation.state, "events", ())),
            )
        if decision == "start":
            normalized_image_paths = tuple(str(Path(path)) for path in image_paths)
            conversation.send_message(
                _build_openhands_user_message(
                    requirement,
                    normalized_image_paths,
                    source,
                )
            )
        elif decision != "approve":
            raise ValueError(f"未知 OpenHands 决策：{decision}")

        if decision == "approve" and expected_action_summaries is not None:
            current_pending = _analyze_pending_actions(
                conversation,
                _pending_actions(conversation),
            )
            current_summaries = tuple(
                _action_summary(action) for action in current_pending
            )
            expected_summaries = tuple(
                str(item).strip() for item in expected_action_summaries
            )
            if current_summaries != expected_summaries:
                if not current_pending:
                    return OpenHandsRunResult(
                        status="stale_permission",
                        markdown=(
                            "## 授权请求已经失效\n\n"
                            "这项操作可能已经完成或取消，没有执行新的工具调用。"
                        ),
                        event_count=len(getattr(conversation.state, "events", ())),
                    )
                pending_context = (
                    create_openhands_permission_context(
                        conversation_id,
                        requirement,
                        current_pending,
                    )
                    if sign_pending_context
                    else ""
                )
                return OpenHandsRunResult(
                    status="waiting_for_confirmation",
                    markdown=(
                        "## 待确认操作已经更新\n\n"
                        "旧按钮没有执行新的操作，请核对下面的最新工具调用后再次确认：\n\n"
                        f"{format_pending_actions(current_pending)}"
                    ),
                    pending_context=pending_context,
                    event_count=len(getattr(conversation.state, "events", ())),
                    action_summaries=current_summaries,
                )

        conversation.run()
        state = conversation.state
        status = _status_value(state)
        events = tuple(getattr(state, "events", ()))
        pending = _analyze_pending_actions(
            conversation,
            _pending_actions(conversation),
        )
        code, saved_path = (
            _latest_artifact(workspace, artifact_snapshot)
            if allow_tools
            else ("", "")
        )
        history = _format_event_history(events)
        error = ""

        if status == "waiting_for_confirmation":
            if not pending:
                raise RuntimeError("OpenHands 正在等待权限，但没有返回待确认操作")
            pending_context = create_openhands_permission_context(
                conversation_id,
                requirement,
                pending,
            ) if sign_pending_context else ""
            action_summaries = tuple(_action_summary(action) for action in pending)
            markdown = (
                "## 执行前权限确认\n\n"
                "OpenHands 已暂停，以下操作尚未执行：\n\n"
                f"{format_pending_actions(pending)}\n\n"
                "> 点击“允许本次操作”或“拒绝并停止”。授权只覆盖当前这批工具调用。"
            )
            return OpenHandsRunResult(
                status=status,
                markdown=markdown,
                pending_context=pending_context,
                code=code,
                saved_path=saved_path,
                event_count=len(events),
                action_summaries=action_summaries,
            )

        if status in {"error", "stuck"}:
            error = history or f"OpenHands 会话状态：{status}"
            heading = "OpenHands 执行未完成"
        else:
            heading = "OpenHands 任务完成"
        artifact_text = f"\n\n生成/修改文件：`{saved_path}`" if saved_path else ""
        markdown = (
            f"## {heading}\n\n"
            f"**状态：** `{status}`\n\n"
            f"### 执行记录\n\n{history or '- 会话没有返回可显示事件'}"
            f"{artifact_text}"
        )
        return OpenHandsRunResult(
            status=status,
            markdown=markdown,
            code=code,
            saved_path=saved_path,
            error=error,
            event_count=len(events),
        )
    finally:
        conversation.close()


def _result_from_worker(payload: dict[str, Any]) -> OpenHandsRunResult:
    return OpenHandsRunResult(
        status=str(payload.get("status", "error")),
        markdown=str(payload.get("markdown", "")),
        pending_context=str(payload.get("pending_context", "")),
        code=str(payload.get("code", "")),
        saved_path=str(payload.get("saved_path", "")),
        error=str(payload.get("error", "")),
        event_count=int(payload.get("event_count", 0)),
        action_summaries=tuple(str(item) for item in payload.get("action_summaries", ())),
    )


def _run_worker(
    request_payload: dict[str, Any],
    source: Any,
) -> OpenHandsRunResult:
    from config import PROJECT_DIR

    python_path = Path(source.effective_openhands_python).expanduser().resolve()
    worker_path = PROJECT_DIR / "openhands_worker.py"
    if not python_path.is_file():
        raise RuntimeError(
            f"OpenHands 独立运行时不存在：{python_path}。"
            "请在 .env 设置 OPENHANDS_PYTHON。"
        )
    completed = subprocess.run(
        [str(python_path), "-I", "-X", "utf8", str(worker_path)],
        input=json.dumps(request_payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(source.openhands_worker_timeout),
        shell=False,
        check=False,
    )
    marker_index = completed.stdout.rfind(_WORKER_RESULT_PREFIX)
    if marker_index < 0:
        detail = (completed.stderr or completed.stdout or "独立运行时没有返回结果").strip()
        raise RuntimeError(
            f"OpenHands 独立运行时失败（退出码 {completed.returncode}）：{detail[-3000:]}"
        )
    raw_result = completed.stdout[marker_index + len(_WORKER_RESULT_PREFIX) :].splitlines()[0]
    try:
        result = _result_from_worker(json.loads(raw_result))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError("OpenHands 独立运行时返回了无效 JSON") from exc
    if completed.returncode != 0 and not result.error:
        raise RuntimeError(
            f"OpenHands 独立运行时异常退出（退出码 {completed.returncode}）"
        )
    return result


def execute_openhands_task(
    requirement: str,
    session_id: str,
    permission_level: str,
    *,
    decision: str = "start",
    conversation_id: uuid.UUID | None = None,
    expected_action_summaries: Iterable[str] | None = None,
    source: Any | None = None,
    image_paths: Iterable[str | Path] = (),
    allow_tools: bool = True,
) -> OpenHandsRunResult:
    """通过隔离运行时执行任务；传入 source 时供单元测试进行进程内验证。"""
    if source is not None:
        return _execute_openhands_in_process(
            requirement,
            session_id,
            permission_level,
            decision=decision,
            conversation_id=conversation_id,
            expected_action_summaries=expected_action_summaries,
            source=source,
            image_paths=image_paths,
            allow_tools=allow_tools,
        )

    from config import settings

    effective_conversation_id = conversation_id or session_id_to_conversation_id(session_id)
    result = _run_worker(
        {
            "requirement": requirement,
            "session_id": session_id,
            "permission_level": permission_level,
            "decision": decision,
            "conversation_id": str(effective_conversation_id),
            "expected_action_summaries": (
                list(expected_action_summaries)
                if expected_action_summaries is not None
                else None
            ),
            "image_paths": [str(Path(path)) for path in image_paths],
            "allow_tools": bool(allow_tools),
        },
        settings,
    )
    if result.status == "waiting_for_confirmation":
        context = _create_openhands_permission_context_from_summaries(
            effective_conversation_id,
            requirement,
            result.action_summaries,
        )
        result = replace(result, pending_context=context)
    return result


def serialize_worker_result(result: OpenHandsRunResult) -> str:
    """供隔离 worker 输出带边界标记的单行 JSON。"""
    return _WORKER_RESULT_PREFIX + json.dumps(
        asdict(result),
        ensure_ascii=False,
        separators=(",", ":"),
    )
