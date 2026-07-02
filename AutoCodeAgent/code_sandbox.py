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


def safe_execute_code(code_str: str, timeout: int = 15) -> tuple[str, str]:
    """在子进程中执行 Python 代码并捕获输出

    Args:
        code_str: Python 代码文本
        timeout: 超时秒数（默认 15s，简单程序通常 1-3s，复杂程序可能需要 10s+）

    Returns:
        (stdout, stderr)
    """
    # 语法预检（快速失败，避免写到磁盘）
    try:
        compile(code_str, "<pre-check>", "exec")
    except SyntaxError as e:
        return "", f"[SyntaxError] 语法错误：{e}\n{traceback.format_exc()}"

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
        proc = subprocess.run(
            [sys.executable, "-I", tmp.name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **kwargs,
        )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        # 非零退出码但没有 stderr → 补充提示
        if proc.returncode != 0 and not stderr.strip():
            stderr = f"[ExitCode {proc.returncode}] 进程异常退出，可能被信号中断或代码崩溃无输出。"

        return stdout, stderr

    except subprocess.TimeoutExpired:
        return "", f"[SandboxError] 执行超时 {timeout} 秒，已强制终止（请检查代码中是否存在死循环）。"

    except Exception as e:
        return "", f"[SandboxError] 子进程执行异常：{type(e).__name__}: {e}"

    finally:
        # 清理临时文件
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
