"""全局日志配置 — 使用 Loguru"""

import sys
from pathlib import Path

from loguru import logger

from config import settings

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# 移除默认的 stderr 输出，统一格式
logger.remove()

# 标准输出：带时间、级别、模块、行号
logger.add(
    sys.stdout,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
           "<level>{level: <8}</level> | "
           "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)

# 可选：写入日志文件（按天轮转，保留 7 天）
logger.add(
    LOG_DIR / "autocode-agent.log",
    rotation="1 day",
    retention="7 days",
    level="DEBUG",
    encoding="utf-8",
)

# 同步一份运行日志到 Obsidian 仓库，便于直接查看和搜索。
if settings.memory_enabled:
    memory_log_dir = settings.memory_dir.expanduser() / "运行日志"
    memory_log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        memory_log_dir / "AutoCodeAgent 运行日志.md",
        rotation="10 MB",
        retention=10,
        level="DEBUG",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{line} | {message}",
    )
