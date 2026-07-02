"""代码安全扫描器 — 执行前静态分析恶意模式

检测维度：
1. 危险导入（socket / subprocess / os.system 等）
2. 文件破坏（删除/覆盖系统路径）
3. 网络外泄（连接外部主机）
4. 混淆载荷（base64 解码执行、编码字符串）
5. 持久化/自启动（注册表、启动项、计划任务）
6. 凭证窃取（读浏览器密码、SSH 密钥、环境变量）
7. 矿机/勒索特征

集成方式：
    在 executor_node 中先 scan，高风险则暂停等待用户确认。
"""

import ast
import os
import re
from dataclasses import dataclass, field
from typing import Optional

# ──────────────────────────────────────────────
# 风险等级
# ──────────────────────────────────────────────
CRITICAL = 4  # 立即阻止，必须人工确认
HIGH = 3      # 强烈建议阻止
MEDIUM = 2    # 可疑，需注意
LOW = 1       # 信息性提示
SAFE = 0      # 无风险

RISK_LABEL = {CRITICAL: "🔴 严重", HIGH: "🟠 高危", MEDIUM: "🟡 可疑", LOW: "🔵 提示", SAFE: "🟢 安全"}


@dataclass
class Finding:
    """单个检测发现"""
    risk: int
    title: str
    detail: str = ""
    line_no: Optional[int] = None
    snippet: str = ""


@dataclass
class ScanReport:
    """扫描报告"""
    findings: list[Finding] = field(default_factory=list)

    @property
    def max_risk(self) -> int:
        return max((f.risk for f in self.findings), default=SAFE)

    @property
    def is_safe(self) -> bool:
        return self.max_risk < MEDIUM

    @property
    def summary(self) -> str:
        if not self.findings:
            return "✅ 未发现安全风险"
        lines = [f"⚠️  发现 {len(self.findings)} 个安全风险项，最高等级：{RISK_LABEL.get(self.max_risk, '未知')}"]
        for f in self.findings:
            loc = f" (第 {f.line_no} 行)" if f.line_no else ""
            lines.append(f"  [{RISK_LABEL.get(f.risk, '?')}]{loc} {f.title}")
            if f.detail:
                lines.append(f"      └ {f.detail}")
        return "\n".join(lines)


# ──────────────────────────────────────────────
# 恶意模式规则
# ──────────────────────────────────────────────

_SUSPICIOUS_IMPORTS: dict[str, int] = {
    # 网络外泄
    "socket": HIGH,
    "requests": MEDIUM,
    "urllib.request": MEDIUM,
    "urllib3": MEDIUM,
    "http.client": MEDIUM,
    "ftplib": HIGH,
    "smtplib": MEDIUM,
    "telnetlib": HIGH,
    # 系统命令执行
    "subprocess": CRITICAL,
    "shutil": MEDIUM,
    "ctypes": HIGH,
    # 加密/矿机
    "hashlib": LOW,
    "cryptography": MEDIUM,
    "Crypto": MEDIUM,
    " Crypto ": MEDIUM,
    "secretstorage": MEDIUM,
    "keyring": MEDIUM,
    # 持久化
    "winreg": CRITICAL,
    "win32api": HIGH,
    "win32com": HIGH,
    "pynput": HIGH,
    "keyboard": HIGH,
    "mouse": HIGH,
    "pyautogui": MEDIUM,
    "sched": LOW,
}

_SYSTEM_PATHS = [
    "/etc", "/usr", "/bin", "/boot", "/dev",
    "C:\\Windows", "C:\\Program Files", "C:\\System32",
    "C:\\Users\\Administrator\\AppData",
    "/root", "~/.ssh", "~/.config",
]

_KNOWN_BROWSER_PATHS = [
    "browser", "chrome", "chromium", "firefox", "edge",
    "cookies", "password", "login_data", "webdata",
    "~/.mozilla", "~/.config/google-chrome",
]

_SUSPICIOUS_VAR_NAMES = [
    "payload", "shellcode", "backdoor", "trojan",
    "ransomware", "keylogger", "spy", "worm",
    "malware", "exploit", "inject", "dropper",
]


def _has_break_or_return(node: ast.AST) -> bool:
    """检查循环体中是否存在 break/return（粗略判断是否为无限循环）"""
    for child in ast.walk(node):
        if isinstance(child, (ast.Break, ast.Return)):
            return True
    return False


def _ast_scan(tree: ast.AST, source_lines: list[str]) -> list[Finding]:
    """基于 AST 的结构化扫描"""
    findings = []

    for node in ast.walk(tree):
        # ── 检测明显的无限循环 / 高 CPU 模式 ──
        if isinstance(node, ast.While):
            # while True / while 1 / while False is not False 等恒真条件
            cond = node.test
            is_always_true = (
                (isinstance(cond, ast.Constant) and cond.value is True)
                or (isinstance(cond, ast.Constant) and cond.value == 1)
                or (isinstance(cond, ast.Compare) and len(cond.ops) == 1
                    and isinstance(cond.ops[0], ast.Eq))
            )
            if is_always_true and not _has_break_or_return(node):
                findings.append(Finding(
                    risk=HIGH,
                    title="疑似无限循环",
                    detail="检测到 `while True` 且循环体内无 break/return，可能导致 CPU 100% 占用",
                    line_no=getattr(node, "lineno", None),
                ))

        if isinstance(node, ast.For):
            # for _ in range(极大数字)
            if (isinstance(node.iter, ast.Call)
                    and _get_call_name(node.iter) == "range"
                    and len(node.iter.args) >= 1
                    and isinstance(node.iter.args[0], ast.Constant)
                    and isinstance(node.iter.args[0].value, int)
                    and node.iter.args[0].value >= 100_000):
                findings.append(Finding(
                    risk=MEDIUM,
                    title="大范围循环",
                    detail=f"检测到 `range({node.iter.args[0].value})`，可能导致执行缓慢或高 CPU",
                    line_no=getattr(node, "lineno", None),
                ))

        # ── 检测危险函数调用 ──
        if isinstance(node, ast.Call):
            func = _get_call_name(node)
            line_no = getattr(node, "lineno", None)

            # eval() / exec() / compile() 动态执行
            if func in ("eval", "exec", "compile") and not _is_empty_arg(node):
                findings.append(Finding(
                    risk=CRITICAL,
                    title="动态代码执行",
                    detail=f"使用 {func}() 动态执行字符串，可能用于运行混淆载荷",
                    line_no=line_no,
                ))

            # os.system / os.popen / subprocess.* / os.spawn*
            if func in ("os.system", "os.popen", "subprocess.Popen",
                        "subprocess.call", "subprocess.run", "os.execl",
                        "os.execle", "os.execlp", "os.fork", "os.spawnl",
                        "ctypes.CDLL", "ctypes.WinDLL"):
                findings.append(Finding(
                    risk=CRITICAL,
                    title="系统命令执行",
                    detail=f"调用 {func}() 执行系统命令，可能用于运行恶意程序",
                    line_no=line_no,
                ))

            # os.remove / os.unlink / shutil.rmtree — 文件破坏
            if func in ("os.remove", "os.unlink", "os.rmdir",
                        "shutil.rmtree", "os.chmod"):
                findings.append(Finding(
                    risk=CRITICAL,
                    title="文件删除/权限修改",
                    detail=f"调用 {func}() 删除文件或修改权限",
                    line_no=line_no,
                ))

            # base64 decode + 后续 eval/exec
            if func in ("base64.b64decode", "base64.decodestring",
                        "codecs.decode", "binascii.a2b_base64"):
                findings.append(Finding(
                    risk=CRITICAL,
                    title="Base64 解码载荷",
                    detail=f"调用 {func}() 解码疑似混淆数据",
                    line_no=line_no,
                ))

            # 网络连接
            if func in ("socket.connect", "socket.create_connection",
                        "urllib.request.urlopen", "urllib3.request",
                        "requests.get", "requests.post", "requests.put",
                        "requests.delete", "http.client.HTTPConnection"):
                findings.append(Finding(
                    risk=HIGH,
                    title="网络连接",
                    detail=f"调用 {func}() 建立外部网络连接",
                    line_no=line_no,
                ))

            # 文件写入到可疑路径
            if func == "open" or func.endswith(".open"):
                _check_file_write_arg(node, findings, source_lines)

            # 环境变量读取
            if func in ("os.environ.get", "os.getenv"):
                findings.append(Finding(
                    risk=MEDIUM,
                    title="读取环境变量",
                    detail=f"调用 {func}() 读取系统环境变量，可能用于信息收集",
                    line_no=line_no,
                ))

        # ── 检测 import ──
        if isinstance(node, ast.Import):
            for alias in node.names:
                risk = _SUSPICIOUS_IMPORTS.get(alias.name)
                if risk is not None and risk >= HIGH:
                    findings.append(Finding(
                        risk=risk,
                        title=f"导入危险模块: {alias.name}",
                        detail="该模块可能被用于恶意操作",
                        line_no=getattr(node, "lineno", None),
                    ))

        if isinstance(node, ast.ImportFrom):
            if node.module:
                risk = _SUSPICIOUS_IMPORTS.get(node.module)
                if risk is not None and risk >= HIGH:
                    findings.append(Finding(
                        risk=risk,
                        title=f"导入危险模块: {node.module}",
                        detail="该模块可能被用于恶意操作",
                        line_no=getattr(node, "lineno", None),
                    ))

    return findings


def _regex_scan(code: str, source_lines: list[str]) -> list[Finding]:
    """基于正则的文本扫描（捕获 AST 无法检测的模式）"""
    findings = []

    # 混淆：长 base64 字符串 + eval/exec 组合
    b64_payloads = re.findall(
        r"(?:eval|exec|compile)\s*\(\s*(?:base64|b64|codecs)\.\w+decode\s*\(",
        code, re.IGNORECASE
    )
    if b64_payloads:
        findings.append(Finding(
            risk=CRITICAL, title="混淆载荷执行模式",
            detail="检测到 base64 解码 + eval/exec 组合，典型混淆攻击手法",
        ))

    # 混淆：异或/位移解码
    xor_patterns = re.findall(
        r"(?:lambda\s+\w+\s*:\s*\w+\s*(\^|<<|>>)\s*\d+)",
        code
    )
    if xor_patterns:
        findings.append(Finding(
            risk=CRITICAL, title="异或/位移解码操作",
            detail="检测到 XOR 或位移运算，常用于解码混淆载荷",
        ))

    # 长数字字符串（疑似编码后的 shellcode）
    long_hex = re.findall(r'["\']([0-9a-fA-F]{100,})["\']', code)
    if long_hex:
        findings.append(Finding(
            risk=CRITICAL, title="疑似 shellcode 的长十六进制字符串",
            detail=f"发现长度超过 100 的十六进制字符串（{len(long_hex[0])} 字符），可能是编码后的载荷",
            snippet=long_hex[0][:40] + "...",
        ))

    # 矿机检测：高 CPU 密集循环 + 数学运算
    miner_pattern = re.findall(
        r"(?:while\s+True|for\s+\w+\s+in\s+range\s*\(\s*\d{6,}\s*\))\s*:.*?(?:hashlib|sha256|md5)",
        code, re.DOTALL | re.IGNORECASE
    )
    if miner_pattern:
        findings.append(Finding(
            risk=CRITICAL, title="疑似挖矿代码",
            detail="检测到大循环 + 哈希运算的高 CPU 密集型代码模式",
        ))

    # 持久化：注册表/启动项/计划任务
    reg_pat = re.findall(r"(?:winreg\.|regedit|schtasks|cron|systemd|autorun|startup)", code, re.IGNORECASE)
    if reg_pat:
        findings.append(Finding(
            risk=CRITICAL, title="持久化/自启动设置",
            detail=f"检测到注册表/计划任务/自启动相关操作: {', '.join(set(reg_pat))}",
        ))

    # 可疑文件名/变量名
    for var_name in _SUSPICIOUS_VAR_NAMES:
        if var_name.lower() in code.lower():
            findings.append(Finding(
                risk=HIGH, title="发现可疑命名",
                detail=f"代码中出现疑似恶意软件命名的标识符: '{var_name}'",
            ))

    # 超长单行（混淆代码常见特征）
    for i, line in enumerate(source_lines, 1):
        stripped = line.strip()
        if len(stripped) > 500 and not stripped.startswith("#") and not stripped.startswith("//"):
            findings.append(Finding(
                risk=MEDIUM, title="超长代码行（疑似混淆）",
                detail=f"第 {i} 行长度 {len(stripped)} 字符，远超正常代码行长度",
                line_no=i,
            ))

    return findings


def _get_call_name(node: ast.Call) -> str:
    """获取函数调用的完整限定名（如 os.system）"""
    if isinstance(node.func, ast.Attribute):
        parts = []
        curr = node.func
        while isinstance(curr, ast.Attribute):
            parts.append(curr.attr)
            curr = curr.value
        if isinstance(curr, ast.Name):
            parts.append(curr.id)
        elif isinstance(curr, ast.Call):
            return "<call>." + ".".join(reversed(parts))
        return ".".join(reversed(parts))
    if isinstance(node.func, ast.Name):
        return node.func.id
    return "<unknown>"


def _is_empty_arg(node: ast.Call) -> bool:
    """eval()/exec() 是否空参（只有内置名而不是字符串）"""
    return len(node.args) == 0


def _check_file_write_arg(node: ast.Call, findings: list, source_lines: list[str]):
    """检查 open() 写入路径是否涉及系统路径"""
    try:
        if len(node.args) >= 1 and isinstance(node.args[0], ast.Constant):
            path = str(node.args[0].value)
            for sys_path in _SYSTEM_PATHS:
                if path.lower().startswith(sys_path.lower()):
                    findings.append(Finding(
                        risk=CRITICAL,
                        title="写入系统路径",
                        detail=f"尝试向系统路径写入文件: {path}",
                        line_no=getattr(node, "lineno", None),
                    ))
                    break
    except Exception:
        pass


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────

def scan_code(code: str) -> ScanReport:
    """对代码进行安全扫描，返回扫描报告"""
    if not code or not code.strip():
        return ScanReport()

    source_lines = code.splitlines()
    findings = []

    # 1. AST 扫描
    try:
        tree = ast.parse(code)
        findings.extend(_ast_scan(tree, source_lines))
    except SyntaxError:
        # 代码有语法错误，无法 AST 扫描，但后续 regex 仍可执行
        findings.append(Finding(
            risk=LOW, title="代码语法错误，AST 扫描跳过",
        ))

    # 2. 正则扫描
    findings.extend(_regex_scan(code, source_lines))

    # 去重（相同标题+行号只保留一个最高风险）
    seen = set()
    unique = []
    for f in sorted(findings, key=lambda x: -x.risk):
        key = (f.title, f.line_no)
        if key not in seen:
            seen.add(key)
            unique.append(f)

    return ScanReport(findings=unique)


def confirm_execution(report: ScanReport) -> bool:
    """交互式确认 — 在终端中使用 input() 询问用户"""
    print("\n" + "=" * 60)
    print("  🔒 代码安全扫描报告")
    print("=" * 60)
    print(report.summary)
    print("=" * 60)

    if report.is_safe:
        return True

    if report.max_risk >= CRITICAL:
        ans = input("\n⚠️  检测到严重风险！是否仍然执行？(y/N): ").strip().lower()
        return ans in ("y", "yes")
    elif report.max_risk >= HIGH:
        ans = input("\n⚠️  检测到高危操作！是否继续？(y/N): ").strip().lower()
        return ans in ("y", "yes")
    else:
        ans = input("\n⚠️  检测到可疑模式。是否继续？(Y/n): ").strip().lower()
        return ans not in ("n", "no")


def web_confirm(report: ScanReport) -> str:
    """Web 模式：返回提示消息，让前端展示"""
    if report.is_safe:
        return ""
    msg = "\n\n---\n\n"
    msg += "## 🔒 安全扫描发现风险\n\n"
    msg += "> ⚠️ 代码执行前安全检查发现以下问题，已自动阻止执行：\n\n"
    for f in report.findings:
        loc = f" (第 {f.line_no} 行)" if f.line_no else ""
        msg += f"- **[{RISK_LABEL.get(f.risk, '?')}]{loc}** {f.title}"
        if f.detail:
            msg += f"\n  _{f.detail}_"
        msg += "\n"
    msg += "\n---\n"
    msg += "如需强制运行，请在需求末尾加上 `[我已检查，强制运行]`。"
    return msg


FORCE_RUN_MARKER = "[我已检查，强制运行]"


def has_force_run_marker(code_or_req: str) -> bool:
    """检查是否包含强制运行标记"""
    return FORCE_RUN_MARKER in code_or_req
