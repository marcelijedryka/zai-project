"""Structured JSON logging with secret redaction (plan §6.3)."""

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"

# Keys whose values are never logged (HTTP headers and JSON fields alike). Compared after
# lower-casing and dropping "-" and "_", so Set-Cookie, x_api_key and refresh_token all match.
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "xapikey",
        "password",
        "currentpassword",
        "newpassword",
        "passwordhash",
        "token",
        "accesstoken",
        "refreshtoken",
        "apikey",
    }
)
# Defence in depth for secrets that end up inside free-form text.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bzai_[A-Za-z0-9_-]{20,}"),
    re.compile(r"__Host-zai_session=[^;\s\"]+"),
)

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def _is_sensitive(key: object) -> bool:
    return str(key).lower().replace("-", "").replace("_", "") in SENSITIVE_KEYS


def redact(value: Any) -> Any:
    """Return a copy of ``value`` with sensitive keys masked and secret patterns scrubbed."""
    if isinstance(value, dict):
        return {k: REDACTED if _is_sensitive(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(REDACTED, value)
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line; structured data goes in ``extra={"fields": {...}}``."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "requestId": request_id_var.get(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", error_log_file: Path | None = None) -> None:
    """Idempotently route root logging to stdout (JSON) and WARNING+ to ``error_log_file``."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()  # release file descriptors when called again
    # Uvicorn configures its own plain-text handlers and stops propagation; route its
    # startup and error lines through the root (JSON) handlers instead. ``uvicorn.access``
    # is left alone: ``--no-access-log`` disables it by cutting its propagation, and our
    # own request log replaces it.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        for handler in list(uvicorn_logger.handlers):
            uvicorn_logger.removeHandler(handler)
        uvicorn_logger.propagate = True
    formatter = JsonFormatter()
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)
    root.addHandler(stdout)
    root.setLevel(level.upper())
    if error_log_file is not None:
        error_log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(error_log_file, encoding="utf-8")
        file_handler.setLevel(logging.WARNING)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
