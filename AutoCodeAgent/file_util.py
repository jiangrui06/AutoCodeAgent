"""代码持久化工具 — 自动保存到本地并记录版本历史"""

import os
from datetime import datetime
from pathlib import Path

CODE_SAVE_DIR = Path(__file__).parent / "auto_generated_code"
CODE_SAVE_DIR.mkdir(exist_ok=True)


def save_code_to_file(code_content: str, phase: str = "") -> str:
    """保存代码到文件，返回文件路径"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"_{phase}" if phase else ""
    filename = f"code_{timestamp}{tag}.py"
    file_path = CODE_SAVE_DIR / filename
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(code_content)
    return str(file_path)


def save_iteration_snapshot(code_content: str, retry: int) -> str:
    """保存某次重试迭代的代码快照"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = CODE_SAVE_DIR / f"iter_{retry:02d}_{timestamp}.py"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(code_content)
    return str(file_path)


def get_latest_code_file() -> "str | None":
    """获取最近一次保存的代码文件路径"""
    files = sorted(CODE_SAVE_DIR.glob("code_*.py"), reverse=True)
    return str(files[0]) if files else None


def get_all_generated_files() -> list[str]:
    """列出所有已生成的代码文件"""
    return sorted(str(p) for p in CODE_SAVE_DIR.glob("*.py"))
