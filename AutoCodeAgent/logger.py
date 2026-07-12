"""全局日志配置 — 使用 Loguru"""

import sys

from loguru import logger

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
    "logs/autocode-agent.log",
    rotation="1 day",
    retention="7 days",
    level="DEBUG",
    encoding="utf-8",
)
