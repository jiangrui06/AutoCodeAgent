"""Global logging configuration used across AutoCodeAgent."""

import logging
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    from loguru import logger
except Exception:  # pragma: no cover - fallback when loguru not installed in runtime python

    class _FallbackLogger:
        def __init__(self) -> None:
            self._logger = logging.getLogger("AutoCodeAgent")
            if not self._logger.handlers:
                self._logger.setLevel(logging.INFO)
            self._handlers: list[logging.Handler] = []

        @staticmethod
        def _fmt(message: Any, args: Iterable[Any], kwargs: dict[str, Any]) -> str:
            msg = str(message)
            try:
                if args or kwargs:
                    return msg.format(*args, **kwargs)
            except Exception:
                pass
            return msg

        def remove(self, _id: int | None = None) -> None:  # noqa: ARG002
            for handler in list(self._handlers):
                self._logger.removeHandler(handler)
                handler.close()
            self._handlers.clear()

        def add(
            self,
            sink: Any,
            level: str = "INFO",
            encoding: str | None = None,
            **kwargs: Any,
        ) -> int:
            log_level = getattr(logging, str(level).upper(), logging.INFO)
            format_template = str(
                kwargs.get(
                    "format",
                    "{asctime} | {levelname} | {name}:{lineno} | {message}",
                )
            )
            if "{time:" in format_template:
                format_template = format_template.replace("{time:YYYY-MM-DD HH:mm:ss}", "{asctime}")
                if "{time:" in format_template:
                    format_template = format_template.replace(
                        "{time:YYYY-MM-DD HH:mm:ss} | ",
                        "{asctime} | ",
                    )
            formatter = logging.Formatter(format_template, style="{")
            if isinstance(sink, (str, Path)):
                handler = logging.FileHandler(
                    sink,
                    encoding=encoding or "utf-8",
                )
            elif sink in (sys.stdout, sys.stderr):
                handler = logging.StreamHandler(sink)
            else:
                return 0
            handler.setLevel(log_level)
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)
            self._handlers.append(handler)
            return len(self._handlers)

        def _log(self, level: int, message: Any, *args: Any, **kwargs: Any) -> None:
            self._logger.log(level, self._fmt(message, args, kwargs))

        def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
            self._log(logging.INFO, message, *args, **kwargs)

        def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
            self._log(logging.WARNING, message, *args, **kwargs)

        def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
            self._log(logging.ERROR, message, *args, **kwargs)

        def exception(self, message: Any, *args: Any, **kwargs: Any) -> None:
            self._logger.exception(self._fmt(message, args, kwargs))

    logger = _FallbackLogger()

from config import settings

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger.remove()

logger.add(
    sys.stdout,
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} - {message}",
)

logger.add(
    LOG_DIR / "autocode-agent.log",
    rotation="1 day",
    retention="7 days",
    level="DEBUG",
    encoding="utf-8",
)

if settings.memory_enabled:
    memory_log_dir = settings.memory_dir.expanduser() / "执行日志"
    memory_log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        memory_log_dir / "AutoCodeAgent 执行日志.md",
        rotation="10 MB",
        retention=10,
        level="DEBUG",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{line} | {message}",
    )
