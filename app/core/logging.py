"""Structured logging and request correlation.

Every log line is JSON-serialisable and passes through the same redaction used
by run events, so tokens and secret values cannot leak into log aggregation.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

from app.config import settings
from app.core.security import redact

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
user_id_var: ContextVar[str] = ContextVar("user_id", default="")
run_id_var: ContextVar[str] = ContextVar("run_id", default="")

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName", "levelname", "levelno",
    "lineno", "module", "msecs", "message", "msg", "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        request_id = request_id_var.get()
        if request_id:
            payload.setdefault("request_id", request_id)
        user_id = user_id_var.get()
        if user_id:
            payload.setdefault("user_id", user_id)
        run_id = run_id_var.get()
        if run_id:
            payload.setdefault("run_id", run_id)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str)


class ConsoleFormatter(logging.Formatter):
    """Compact human-readable output for local development."""

    COLORS = {"DEBUG": "\033[36m", "INFO": "\033[32m", "WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[35m"}

    def __init__(self, use_color: bool = True) -> None:
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "") if self.use_color else ""
        reset = "\033[0m" if color else ""
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        context = " ".join(
            f"{name}={value}"
            for name, value in (
                ("req", request_id_var.get()[:8]),
                ("run", run_id_var.get()[:8]),
            )
            if value
        )
        extras = " ".join(
            f"{key}={value}"
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        )
        head = f"{stamp} {color}{record.levelname:<7}{reset} {record.name}: {record.getMessage()}"
        tail = " ".join(part for part in (context, extras) if part)
        line = f"{head}  {tail}".rstrip()
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(ConsoleFormatter(use_color=sys.stdout.isatty()))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)
    for noisy, level in {"uvicorn.access": logging.WARNING, "uvicorn.error": logging.INFO, "sqlalchemy.engine": logging.WARNING, "multipart": logging.WARNING}.items():
        logging.getLogger(noisy).setLevel(level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_request(request_id: str, user_id: str = "", run_id: str = "") -> None:
    request_id_var.set(request_id)
    if user_id:
        user_id_var.set(user_id)
    if run_id:
        run_id_var.set(run_id)


def log_extra(**values: Any) -> dict[str, Any]:
    """Build the ``extra=`` mapping for a log call, already redacted."""
    return {"extra": redact(values)}


__all__ = [
    "JsonFormatter",
    "bind_request",
    "configure_logging",
    "get_logger",
    "log_extra",
    "new_request_id",
    "request_id_var",
    "run_id_var",
    "user_id_var",
]
