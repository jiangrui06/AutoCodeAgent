"""代码静态检查器 — 在沙箱执行前快速发现语法和语义错误

使用 pyflakes 做语义检查（未定义名、未使用导入等），
使用 compile() 做语法检查。比直接跑子进程更快、更省 Token。
"""

import traceback
from io import StringIO

from pyflakes import api as pyflakes_api
from pyflakes import reporter as pyflakes_reporter


def lint_code(code: str) -> tuple[bool, str]:
    """对代码进行静态检查

    Args:
        code: Python 代码文本

    Returns:
        (是否通过, 错误信息)
    """
    # 1. 语法检查（快速失败）
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as e:
        return False, f"[SyntaxError] {e}\n{traceback.format_exc()}"

    # 2. 语义检查（未定义名、未使用导入等）
    output = StringIO()
    rep = pyflakes_reporter.Reporter(output, output)
    try:
        pyflakes_api.check(code, "<generated>", reporter=rep)
    except Exception as e:
        return False, f"[LintError] 静态检查异常：{type(e).__name__}: {e}"

    warnings = output.getvalue().strip()
    if warnings:
        # 仅把 undefined name 视为失败，其他警告放行
        if "undefined name" in warnings.lower():
            return False, f"[LintError] 发现未定义变量：\n{warnings}"
        # 其他警告只记录，不阻止执行
        return True, f"[LintWarning] 静态检查警告（非阻塞）：\n{warnings}"

    return True, ""
