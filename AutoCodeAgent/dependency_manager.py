"""经用户确认后安装受信任依赖的安全边界。"""

from __future__ import annotations

import ast
import hashlib
import hmac
import importlib
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass


INSTALL_CONTEXT_PREFIX = "dependency-install:"
EXECUTION_PERMISSION_PREFIX = "execution-permission:"
PERMISSION_LEVEL_RESTRICTED = "restricted"
PERMISSION_LEVEL_ASK = "ask"
PERMISSION_LEVEL_TRUSTED = "trusted"
PERMISSION_LEVEL_CHOICES = (
    ("受限模式 · 禁止安装与敏感工具", PERMISSION_LEVEL_RESTRICTED),
    ("询问模式 · 每次确认（推荐）", PERMISSION_LEVEL_ASK),
    ("信任模式 · 自动允许全部工具与依赖安装", PERMISSION_LEVEL_TRUSTED),
)
_MISSING_MODULE_PATTERN = re.compile(
    r"ModuleNotFoundError:\s+No module named ['\"]([A-Za-z0-9_.]+)['\"]"
)
_IMPORT_PROBE_TIMEOUT = 20
_IMPORT_PROBE_SCRIPT = """
import importlib
import sys

module_name = sys.argv[1]
names = sys.argv[2:]
try:
    module = importlib.import_module(module_name)
    missing = [name for name in names if name != "*" and not hasattr(module, name)]
    if missing:
        joined = ", ".join(missing)
        raise ImportError(f"cannot import name(s) {joined!r} from {module_name!r}")
except BaseException as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)
""".strip()

# 模型输出不可信：只允许将明确审核过的导入名映射到固定 PyPI 包名。
DEPENDENCY_PACKAGE_MAP = {
    "PyQt5": "PyQt5",
    "PyQt6": "PyQt6",
    "PySide6": "PySide6",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "numpy": "numpy",
    "pandas": "pandas",
    "requests": "requests",
}

_APPROVAL_TEXTS = {
    "允许安装",
    "同意安装",
    "确认安装",
    "可以安装",
    "yes",
    "y",
}
_DENIAL_TEXTS = {
    "取消安装",
    "不允许安装",
    "不要安装",
    "暂时不要安装",
    "no",
    "n",
}


@dataclass(frozen=True)
class MissingDependency:
    module: str
    package: str | None


@dataclass(frozen=True)
class DependencyInstallRequest:
    module: str
    package: str
    original_requirement: str


@dataclass(frozen=True)
class InstallResult:
    success: bool
    package: str
    message: str


@dataclass(frozen=True)
class CapabilityUse:
    key: str
    label: str
    detail: str


@dataclass(frozen=True)
class CodePermissionReport:
    missing_dependencies: tuple[MissingDependency, ...] = ()
    capabilities: tuple[CapabilityUse, ...] = ()


@dataclass(frozen=True)
class PermissionDecision:
    action: str
    reason: str = ""


@dataclass(frozen=True)
class ExecutionPermissionRequest:
    original_requirement: str
    code: str
    dev_plan: str
    code_hash: str


_EXECUTION_APPROVAL_TEXTS = _APPROVAL_TEXTS | {
    "允许执行", "同意执行", "确认执行", "允许本次", "批准",
}
_EXECUTION_DENIAL_TEXTS = _DENIAL_TEXTS | {
    "拒绝执行", "取消执行", "拒绝",
}
_CONTEXT_SIGNING_KEY = secrets.token_bytes(32)


_CAPABILITY_LABELS = {
    "camera_access": "访问摄像头",
    "network": "访问外部网络",
    "filesystem_write": "写入本地文件",
    "filesystem_delete": "删除本地文件",
    "system_command": "执行系统命令",
    "device_control": "控制键盘/鼠标",
}
_CAPABILITY_DEFAULT_DETAILS = {
    "camera_access": "代码会打开并读取摄像头设备",
    "network": "代码会发起 HTTP 或套接字连接",
    "filesystem_write": "代码会创建或修改文件",
    "filesystem_delete": "代码会删除文件或目录",
    "system_command": "代码会启动外部进程或命令",
    "device_control": "代码会模拟或监听输入设备",
}
_CAPABILITY_IMPORTS = {
    "requests": "network", "httpx": "network", "socket": "network",
    "urllib": "network", "selenium": "network", "playwright": "network",
    "pyautogui": "device_control", "pynput": "device_control",
    "keyboard": "device_control", "mouse": "device_control",
}


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    current = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _open_uses_write_mode(node: ast.Call) -> bool:
    mode = "r"
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        mode = str(node.args[1].value)
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            mode = str(keyword.value.value)
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _operation_detail(code: str, node: ast.AST, fallback: str) -> str:
    segment = ast.get_source_segment(code, node) or fallback
    normalized = " ".join(segment.replace("`", "ˋ").split())
    if len(normalized) > 180:
        normalized = normalized[:177] + "..."
    line_number = getattr(node, "lineno", "?")
    return f"第 {line_number} 行：`{normalized}`"


def _append_capability_detail(
    details: dict[str, list[str]],
    key: str,
    detail: str,
) -> None:
    if detail not in details[key]:
        details[key].append(detail)


def inspect_code_permissions(code: str) -> CodePermissionReport:
    """列出代码执行前需要的缺失依赖和敏感能力。"""
    dependencies = detect_code_dependencies(code)
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return CodePermissionReport(dependencies)

    capability_details = {key: [] for key in _CAPABILITY_LABELS}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                key = _CAPABILITY_IMPORTS.get(name.split(".", 1)[0])
                if key:
                    _append_capability_detail(
                        capability_details,
                        key,
                        _operation_detail(code, node, f"import {name}"),
                    )
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        detail = _operation_detail(code, node, name or "函数调用")
        if name.endswith("VideoCapture"):
            _append_capability_detail(capability_details, "camera_access", detail)
        if (name == "open" or name.endswith(".open")) and _open_uses_write_mode(node):
            _append_capability_detail(capability_details, "filesystem_write", detail)
        if name.endswith(
            ("write_text", "write_bytes", "mkdir", "rename", "replace", "imwrite")
        ):
            _append_capability_detail(capability_details, "filesystem_write", detail)
        if name.endswith(("unlink", "rmdir")) or name in {"os.remove", "shutil.rmtree"}:
            _append_capability_detail(capability_details, "filesystem_delete", detail)
        if name.startswith("subprocess.") or name in {"os.system", "os.popen"}:
            _append_capability_detail(capability_details, "system_command", detail)
        if name.startswith(("requests.", "httpx.", "socket.", "urllib.request.")):
            _append_capability_detail(capability_details, "network", detail)
        if name.startswith(("pyautogui.", "pynput.", "keyboard.", "mouse.")):
            _append_capability_detail(capability_details, "device_control", detail)

    capabilities = []
    for key in sorted(capability_details):
        entries = capability_details[key]
        if not entries:
            continue
        visible_entries = entries[:6]
        if len(entries) > len(visible_entries):
            visible_entries.append(f"另有 {len(entries) - len(visible_entries)} 项同类操作")
        capabilities.append(
            CapabilityUse(
                key,
                _CAPABILITY_LABELS[key],
                "；".join(visible_entries) or _CAPABILITY_DEFAULT_DETAILS[key],
            )
        )
    return CodePermissionReport(dependencies, tuple(capabilities))


def normalize_permission_level(value: str) -> str:
    allowed = {PERMISSION_LEVEL_RESTRICTED, PERMISSION_LEVEL_ASK, PERMISSION_LEVEL_TRUSTED}
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else PERMISSION_LEVEL_ASK


def decide_permission_action(
    report: CodePermissionReport,
    permission_level: str,
    has_security_findings: bool = False,
) -> PermissionDecision:
    """按最小权限原则决定继续、询问、阻止或自动安装。"""
    needs_permission = bool(
        report.missing_dependencies or report.capabilities or has_security_findings
    )
    if not needs_permission:
        return PermissionDecision("allow")
    level = normalize_permission_level(permission_level)
    if level == PERMISSION_LEVEL_RESTRICTED:
        return PermissionDecision("block", "当前为受限模式")
    if level == PERMISSION_LEVEL_TRUSTED:
        if report.missing_dependencies:
            return PermissionDecision(
                "auto_install",
                "信任模式自动安装缺失依赖（含未审核包）",
            )
        if has_security_findings or report.capabilities:
            return PermissionDecision(
                "allow",
                "信任模式下默认允许敏感能力与执行风险，跳过逐项确认",
            )
        return PermissionDecision("allow", "信任模式下跳过逐项确认")
    return PermissionDecision("ask", "需要用户逐项确认")


def code_fingerprint(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


def _sign_execution_payload(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.new(_CONTEXT_SIGNING_KEY, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def create_execution_permission_context(
    requirement: str,
    code: str,
    dev_plan: str = "",
) -> str:
    """创建只对当前进程有效、带完整性签名的单次授权上下文。"""
    payload = {
        "original_requirement": (requirement or "").strip()[:8000],
        "code": (code or "")[:100_000],
        "dev_plan": (dev_plan or "")[:12_000],
    }
    payload["code_hash"] = code_fingerprint(payload["code"])
    if not payload["original_requirement"] or not payload["code"]:
        raise ValueError("授权上下文必须包含原始需求和代码")
    payload["signature"] = _sign_execution_payload(payload)
    return EXECUTION_PERMISSION_PREFIX + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )


def parse_execution_permission_context(value: str) -> ExecutionPermissionRequest | None:
    if not value or not value.startswith(EXECUTION_PERMISSION_PREFIX):
        return None
    try:
        payload = json.loads(value[len(EXECUTION_PERMISSION_PREFIX) :])
        signature = str(payload.pop("signature"))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if not hmac.compare_digest(signature, _sign_execution_payload(payload)):
        return None
    requirement = str(payload.get("original_requirement", "")).strip()
    code = str(payload.get("code", ""))
    dev_plan = str(payload.get("dev_plan", ""))
    digest = str(payload.get("code_hash", ""))
    if (
        not requirement
        or not code
        or len(requirement) > 8000
        or len(code) > 100_000
        or len(dev_plan) > 12_000
        or digest != code_fingerprint(code)
    ):
        return None
    return ExecutionPermissionRequest(requirement, code, dev_plan, digest)


def is_execution_permission_approved(text: str) -> bool:
    return (text or "").strip().lower() in _EXECUTION_APPROVAL_TEXTS


def is_execution_permission_denied(text: str) -> bool:
    return (text or "").strip().lower() in _EXECUTION_DENIAL_TEXTS


def detect_missing_dependency(stderr: str) -> MissingDependency | None:
    """从执行错误中识别缺失模块；包名只能来自静态映射。"""
    match = _MISSING_MODULE_PATTERN.search(stderr or "")
    if not match:
        return None
    module = match.group(1).split(".", 1)[0]
    return MissingDependency(module=module, package=DEPENDENCY_PACKAGE_MAP.get(module))


def detect_code_dependencies(code: str) -> tuple[MissingDependency, ...]:
    """执行前扫描所有绝对导入，包括 try/except 中的可选导入。"""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return ()

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_modules.add(node.module.split(".", 1)[0])

    missing: list[MissingDependency] = []
    for module in sorted(imported_modules):
        if module in sys.stdlib_module_names:
            continue
        try:
            installed = importlib.util.find_spec(module) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            installed = False
        if not installed:
            missing.append(MissingDependency(module, DEPENDENCY_PACKAGE_MAP.get(module)))
    return tuple(missing)


def _probe_module_import(
    module: str,
    names: tuple[str, ...] = (),
    timeout: int = _IMPORT_PROBE_TIMEOUT,
) -> tuple[bool, str]:
    """在同一 Python 环境的隔离子进程中验证模块和导入名称。"""
    environment = os.environ.copy()
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    command = [
        sys.executable,
        "-I",
        "-X",
        "utf8",
        "-c",
        _IMPORT_PROBE_SCRIPT,
        module,
        *names,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=environment,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"导入验证超过 {timeout} 秒"
    except OSError as exc:
        return False, f"无法启动导入验证：{type(exc).__name__}"

    if completed.returncode == 0:
        return True, ""
    detail = (completed.stderr or completed.stdout or "未知导入错误").strip()
    return False, detail[-1200:]


def diagnose_code_imports(code: str) -> str:
    """诊断已安装白名单依赖中的无效 ``from ... import ...`` 名称。

    仅在隔离子进程里导入固定白名单包；模型生成的文本不会拼接进命令或 Shell。
    """
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return ""

    probes: list[tuple[str, tuple[str, ...]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 0 or not node.module:
            continue
        root = node.module.split(".", 1)[0]
        if root not in DEPENDENCY_PACKAGE_MAP:
            continue
        names = tuple(alias.name for alias in node.names if alias.name != "*")[:30]
        probe = (node.module, names)
        if probe not in seen:
            seen.add(probe)
            probes.append(probe)
        if len(probes) >= 20:
            break

    diagnostics: list[str] = []
    for module, names in probes:
        ok, detail = _probe_module_import(module, names)
        if ok:
            continue
        root = module.split(".", 1)[0]
        imported = ", ".join(names) or "模块本身"
        diagnostics.append(
            f"- `{module}` 的导入项 `{imported}` 验证失败：{detail}\n"
            f"  `{root}` 已通过安装预检；这不是缺少 {root} 安装包，"
            "而是导入名称、版本 API 或二进制加载问题。"
        )
    return "\n".join(diagnostics)


def create_install_context(requirement: str, dependency: MissingDependency) -> str:
    """创建隐藏的许可上下文，不保存模型生成的命令或包名。"""
    if not dependency.package:
        raise ValueError(f"模块 {dependency.module} 不在自动安装白名单中")
    payload = {
        "module": dependency.module,
        "package": dependency.package,
        "original_requirement": requirement[:8000],
    }
    return INSTALL_CONTEXT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_install_context(value: str) -> DependencyInstallRequest | None:
    """解析并重新验证客户端保存的许可上下文。"""
    if not value or not value.startswith(INSTALL_CONTEXT_PREFIX):
        return None
    try:
        payload = json.loads(value[len(INSTALL_CONTEXT_PREFIX) :])
    except (json.JSONDecodeError, TypeError):
        return None
    module = str(payload.get("module", ""))
    package = str(payload.get("package", ""))
    requirement = str(payload.get("original_requirement", "")).strip()
    if not requirement or DEPENDENCY_PACKAGE_MAP.get(module) != package:
        return None
    return DependencyInstallRequest(module, package, requirement[:8000])


def is_install_approved(text: str) -> bool:
    return (text or "").strip().lower() in _APPROVAL_TEXTS


def is_install_denied(text: str) -> bool:
    return (text or "").strip().lower() in _DENIAL_TEXTS


def install_dependency(
    package: str,
    timeout: int = 300,
    allow_unknown: bool = False,
) -> InstallResult:
    """使用当前 Python 环境安装白名单依赖；永不经过 Shell。"""
    allowed_packages = set(DEPENDENCY_PACKAGE_MAP.values())
    if package not in allowed_packages:
        if not allow_unknown:
            return InstallResult(False, package, "该依赖不在自动安装白名单中。")
        # 允许在信任模式下安装未收录的依赖名（例如实验性模块）
        # 直接将模块名当作 pip 包名尝试安装，安装后会复验是否可导入。

    module = next(
        (name for name, mapped_package in DEPENDENCY_PACKAGE_MAP.items() if mapped_package == package),
        "",
    )
    if not module:
        module = package
    if module and importlib.util.find_spec(module) is not None:
        verified, detail = _probe_module_import(module)
        if verified:
            return InstallResult(
                True,
                package,
                f"依赖 {package} 已安装，并已在当前 Python 环境完成真实导入验证。",
            )
        return InstallResult(
            False,
            package,
            f"依赖 {package} 虽已安装，但导入验证失败：{detail}",
        )

    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-input",
        "--only-binary=:all:",
        package,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return InstallResult(False, package, f"安装 {package} 超过 {timeout} 秒，已停止。")
    except OSError as exc:
        return InstallResult(False, package, f"无法启动依赖安装：{type(exc).__name__}。")

    if completed.returncode == 0:
        importlib.invalidate_caches()
        verified, detail = _probe_module_import(module)
        if verified:
            return InstallResult(
                True,
                package,
                f"依赖 {package} 安装成功，并已在当前 Python 环境通过导入验证。",
            )
        return InstallResult(
            False,
            package,
            f"依赖 {package} 的 pip 安装已完成，但导入验证失败：{detail}",
        )
    return InstallResult(
        False,
        package,
        f"依赖 {package} 安装失败（退出码 {completed.returncode}），请查看终端日志。",
    )
