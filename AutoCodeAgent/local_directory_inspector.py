"""Deterministic, read-only summaries for user-requested local directories."""

from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import re


_WINDOWS_PATH_START = re.compile(r"(?i)[a-z]:[\\/]")
_PATH_TRAILING_CHARS = " \t\r\n\"'，,。；;、"
_INSPECTION_KEYWORDS = (
    "分析",
    "查看",
    "读取",
    "列出",
    "文件夹",
    "目录",
    "有什么",
    "哪些文件",
    "什么内容",
)


def is_directory_inspection_request(requirement: str) -> bool:
    normalized = str(requirement or "").lower()
    return any(keyword in normalized for keyword in _INSPECTION_KEYWORDS)


def find_requested_directory(requirement: str) -> Path | None:
    """Return the longest existing local directory path embedded in text."""
    text = str(requirement or "")
    for match in _WINDOWS_PATH_START.finditer(text):
        remainder = text[match.start() :].splitlines()[0]
        for end in range(len(remainder), 2, -1):
            candidate = remainder[:end].rstrip(_PATH_TRAILING_CHARS)
            if len(candidate) < 3:
                continue
            try:
                path = Path(candidate)
                if path.is_dir():
                    return path.resolve()
            except (OSError, ValueError):
                continue
    return None


def _directory_purpose(paths: list[Path]) -> str:
    names = " ".join(str(path).lower() for path in paths)
    has_resume = "简历" in names or "resume" in names
    has_web_portfolio = any(
        marker in names for marker in ("index.html", "assets", "_shared", "portfolio")
    )
    if has_resume and has_web_portfolio:
        return "从文件名判断，这个文件夹以个人简历、简历备份和网页作品集资料为主。"
    if has_resume:
        return "从文件名判断，这个文件夹以个人简历、简历备份和相关处理脚本为主。"
    if has_web_portfolio:
        return "从目录结构判断，这里主要存放网页项目或作品集资源。"
    return "这是一个普通资料目录；下面按文件类型和相对路径列出了可见内容。"


def summarize_directory(directory: str | Path, *, max_items: int = 500) -> str:
    """Build a bounded metadata-only summary without reading file contents."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(root))

    item_limit = max(1, min(int(max_items), 2_000))
    directories: list[Path] = []
    files: list[tuple[Path, int]] = []
    scan_errors: list[str] = []
    pending = [root]
    truncated = False

    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name.lower())
        except OSError as exc:
            scan_errors.append(f"{current}: {type(exc).__name__}")
            continue

        for entry in entries:
            if len(directories) + len(files) >= item_limit:
                truncated = True
                pending.clear()
                break
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    directories.append(path)
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append((path, entry.stat(follow_symlinks=False).st_size))
            except OSError as exc:
                scan_errors.append(f"{path}: {type(exc).__name__}")

    total_bytes = sum(size for _, size in files)
    extensions = Counter(
        path.suffix.lower() or "（无扩展名）" for path, _ in files
    )
    relative_paths = [path.relative_to(root) for path in directories]
    relative_paths.extend(path.relative_to(root) for path, _ in files)

    lines = [
        "## 本地文件夹分析完成",
        "",
        f"路径：`{root}`",
        "",
        "### 概览",
        "",
        f"- {len(files)} 个文件",
        f"- {len(directories)} 个子目录",
        f"- 文件总大小约 {total_bytes / 1024 / 1024:.2f} MB",
        f"- {_directory_purpose(relative_paths)}",
    ]

    if extensions:
        lines.extend(["", "### 文件类型", ""])
        for extension, count in extensions.most_common():
            lines.append(f"- {extension}：{count} 个")

    lines.extend(["", "### 主要内容", ""])
    visible_items: list[tuple[str, Path]] = [
        ("目录", path.relative_to(root)) for path in directories
    ]
    visible_items.extend(("文件", path.relative_to(root)) for path, _ in files)
    visible_items.sort(key=lambda item: str(item[1]).lower())
    for kind, relative_path in visible_items[:80]:
        suffix = "/" if kind == "目录" else ""
        lines.append(f"- [{kind}] `{relative_path}{suffix}`")

    if len(visible_items) > 80:
        lines.append(f"- 其余 {len(visible_items) - 80} 项已省略显示")
    if truncated:
        lines.extend(["", f"> 为避免扫描过大目录，本次最多统计 {item_limit} 项。"])
    if scan_errors:
        lines.extend(["", f"> 有 {len(scan_errors)} 个位置因访问错误未能读取。"])

    return "\n".join(lines)
