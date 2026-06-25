"""
src/logger.py — Structured logging for the Pareeksha Bhavan Monitor.

Design
------
- Built on top of the standard `logging` module with `structlog` as the
  front-end for structured (key=value) output.
- Console output uses a human-readable format in development and a JSON
  renderer in production (controlled by `json_logs` parameter).
- File output uses a `RotatingFileHandler` so log files never grow unbounded.
- A single call to `configure_logging()` wires everything up; subsequent
  calls are idempotent.
- Callers obtain a bound logger via `get_logger(__name__)`, which is a thin
  wrapper around `structlog.get_logger` and returns a `BoundLogger` that
  carries the module name automatically.

Usage
-----
    from src.logger import configure_logging, get_logger

    configure_logging(level="INFO", log_file=Path("logs/monitor.log"))
    log = get_logger(__name__)

    log.info("monitor_started", version="1.0")
    log.warning("keyword_match", title="Special Exam notice", keyword="CBCSS")
    log.error("request_failed", url="https://...", status_code=503)
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog


# ---------------------------------------------------------------------------
# Internal state — tracks whether configure_logging() has been called
# ---------------------------------------------------------------------------
_configured: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure_logging(
    level: str = "INFO",
    log_file: Path | None = None,
    json_logs: bool = False,
    max_bytes: int = 5 * 1024 * 1024,   # 5 MB per file
    backup_count: int = 3,               # keep 3 rotated files
) -> None:
    """
    Configure the global logging pipeline.

    Parameters
    ----------
    level:
        Minimum log level for all handlers (DEBUG / INFO / WARNING / ERROR /
        CRITICAL).
    log_file:
        If given, attach a `RotatingFileHandler` writing to this path.
        The parent directory is created if it does not exist.
        Pass ``None`` to disable file logging.
    json_logs:
        If ``True``, render console output as JSON (suitable for log
        aggregators such as Datadog or CloudWatch).  If ``False`` (default),
        render as a coloured, human-readable string.
    max_bytes:
        Maximum size of each log file before rotation.  Default: 5 MB.
    backup_count:
        Number of rotated log files to retain.  Default: 3.
    """
    global _configured

    numeric_level = _parse_level(level)

    # ── stdlib root logger ─────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove any handlers added by previous configure_logging() calls
    # (important for test isolation).
    root.handlers.clear()

    # ── Console handler ────────────────────────────────────────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(_PlainFormatter())
    root.addHandler(console_handler)

    # ── File handler (optional) ────────────────────────────────────────────
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(log_file),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(_PlainFormatter())
        root.addHandler(file_handler)

    # Silence noisy third-party loggers
    for noisy in ("urllib3", "requests", "httpx", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # ── structlog ─────────────────────────────────────────────────────────
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    if json_logs:
        final_processor: Any = structlog.processors.JSONRenderer()
    else:
        final_processor = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Wire structlog output through the stdlib root logger so both share
    # the same handlers and level.
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            final_processor,
        ],
        foreign_pre_chain=shared_processors,
    )

    # Replace the plain formatter on the console handler with the structlog one
    console_handler.setFormatter(formatter)
    if log_file is not None:
        file_handler.setFormatter(formatter)  # type: ignore[possibly-undefined]

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """
    Return a structlog BoundLogger bound to *name*.

    Automatically calls ``configure_logging()`` with defaults if it has not
    been called yet, so modules can safely call ``get_logger`` at import time.

    Parameters
    ----------
    name:
        Typically ``__name__`` of the calling module.
    """
    if not _configured:
        configure_logging()

    logger = structlog.get_logger(name)
    return logger  # type: ignore[return-value]


def reset_logging() -> None:
    """
    Reset the logging configuration to its initial state.

    Intended for use in tests — clears all handlers and the ``_configured``
    flag so ``configure_logging()`` can be called again cleanly.
    """
    global _configured
    _configured = False
    root = logging.getLogger()
    root.handlers.clear()
    structlog.reset_defaults()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _PlainFormatter(logging.Formatter):
    """
    Fallback formatter used before structlog is wired up, and for log levels
    that structlog does not handle.
    """

    _FMT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    _DATE_FMT = "%Y-%m-%dT%H:%M:%S"

    def __init__(self) -> None:
        super().__init__(fmt=self._FMT, datefmt=self._DATE_FMT)


def _parse_level(level: str) -> int:
    """Convert a string log level to its numeric ``logging`` constant."""
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        raise ValueError(
            f"Invalid log level: {level!r}. "
            "Must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL."
        )
    return numeric
