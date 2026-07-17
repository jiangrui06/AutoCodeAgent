"""安全代码隔离执行器 — 子进程 + 超时强杀

用独立 Python 子进程运行代码，替代 RestrictedPython。

优势对比：
- open() / input() / 第三方库 — 全部可用（RestrictedPython 禁止）
- 超时强杀 — OS 级 SIGTERM，比 threading.join 可靠
- 进程级隔离 — 死循环不阻塞主程序，崩溃不影响主进程

接口：
    safe_execute_code(code_str, timeout=5) -> (stdout, stderr)
"""

import os
import sys
import tempfile
import subprocess
import traceback
from dataclasses import dataclass
from typing import Iterator

from config import settings


@dataclass(frozen=True)
class ExecutionResult:
    """子进程原始结果；迭代时保持兼容旧的 ``stdout, stderr`` 解包。"""

    stdout: str
    stderr: str
    returncode: int

    def __iter__(self) -> Iterator[str]:
        yield self.stdout
        yield self.stderr


def _get_console_encoding() -> str:
    """返回隔离子进程显式启用的 UTF-8 输出编码。"""
    return "utf-8"


def safe_execute_code(code_str: str, timeout: int = None) -> ExecutionResult:
    """在子进程中执行 Python 代码并捕获输出

    Args:
        code_str: Python 代码文本
        timeout: 超时秒数，默认从 .env 的 SANDBOX_TIMEOUT 读取（默认 15s）

    Returns:
        ExecutionResult；仍可使用 ``stdout, stderr = result`` 解包
    """
    if timeout is None:
        timeout = settings.sandbox_timeout

    # 语法预检（快速失败，避免写到磁盘）
    try:
        compile(code_str, "<pre-check>", "exec")
    except SyntaxError as e:
        return ExecutionResult(
            "",
            f"[SyntaxError] 语法错误：{e}\n{traceback.format_exc()}",
            -1,
        )

    # 写入临时文件
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        prefix="sandbox_",
        delete=False,
        encoding="utf-8",
    )
    try:
        tmp.write(code_str)
        tmp.close()  # 确保刷入磁盘

        # Windows 下降低子进程 CPU 优先级，避免生成代码占满整机
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS

        # 子进程执行
        # -I 隔离模式：忽略 PYTHON* 环境变量，不加载用户 site-packages
        # -I 会忽略 PYTHONIOENCODING 等 PYTHON* 环境变量，因此使用 -X utf8
        # 显式统一 Windows/Linux 的标准流编码，避免 ✓、emoji 等字符在 GBK
        # 控制台上触发 UnicodeEncodeError。
        command = [sys.executable, "-I", "-X", "utf8", tmp.name]
        if "--autocode-self-test" in code_str:
            command.append("--autocode-self-test")

        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding=_get_console_encoding(),
            errors="replace",
            timeout=timeout,
            **kwargs,
        )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        return ExecutionResult(stdout, stderr, proc.returncode)

    except subprocess.TimeoutExpired:
        return ExecutionResult(
            "",
            f"[SandboxError] 执行超时 {timeout} 秒，已强制终止（请检查代码中是否存在死循环）。",
            -1,
        )

    except Exception as e:
        return ExecutionResult(
            "",
            f"[SandboxError] 子进程执行异常：{type(e).__name__}: {e}",
            -1,
        )

    finally:
        # 清理临时文件
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
