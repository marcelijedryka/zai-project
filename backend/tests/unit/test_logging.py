import json
import logging
from pathlib import Path

from app.core.logging import REDACTED, JsonFormatter, configure_logging, redact, request_id_var

BEARER = "Bearer abcDEF123.payload.signature"
API_KEY = "zai_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


def test_redact_masks_sensitive_keys_case_insensitively() -> None:
    headers = {"Authorization": BEARER, "COOKIE": "__Host-zai_session=s3cr3t", "X-API-Key": API_KEY}
    assert redact(headers) == dict.fromkeys(headers, REDACTED)


def test_redact_is_recursive_and_keeps_harmless_values() -> None:
    data = {"user": "admin", "body": {"password": "hunter2", "n": 1}, "list": [{"token": "x"}]}
    assert redact(data) == {
        "user": "admin",
        "body": {"password": REDACTED, "n": 1},
        "list": [{"token": REDACTED}],
    }


def test_redact_matches_snake_case_and_kebab_case_keys() -> None:
    data = {"refresh_token": "a", "current_password": "b", "api_key": "c", "Set-Cookie": "d"}
    assert redact(data) == dict.fromkeys(data, REDACTED)


def test_redact_scrubs_secrets_inside_free_text() -> None:
    text = f"failed with {BEARER} and key {API_KEY} cookie __Host-zai_session=abc123; Path=/"
    cleaned = redact(text)
    assert "abcDEF123" not in cleaned
    assert API_KEY not in cleaned
    assert "abc123" not in cleaned


def test_json_formatter_emits_one_redacted_json_line() -> None:
    token = request_id_var.set("req-1")
    try:
        record = logging.LogRecord(
            "zai.test", logging.WARNING, "f.py", 1, "rejected %s", ("x",), None
        )
        record.fields = {"headers": {"Authorization": BEARER}, "seriesId": 3}
        line = JsonFormatter().format(record)
    finally:
        request_id_var.reset(token)
    parsed = json.loads(line)
    assert "\n" not in line
    assert parsed["event"] == "rejected x"
    assert parsed["level"] == "WARNING"
    assert parsed["requestId"] == "req-1"
    assert parsed["seriesId"] == 3
    assert parsed["headers"]["Authorization"] == REDACTED
    assert "abcDEF123" not in line


def test_configure_logging_writes_warnings_to_the_error_log_file(tmp_path: Path) -> None:
    error_log = tmp_path / "logs" / "errors.log"
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        configure_logging("INFO", error_log)
        logger = logging.getLogger("zai.test")
        logger.info("just info")
        logger.warning(
            "measurement.rejected",
            extra={"fields": {"seriesId": 3, "value": 612.0, "headers": {"X-API-Key": API_KEY}}},
        )
        for handler in root.handlers:
            handler.flush()
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
    lines = error_log.read_text().splitlines()
    assert len(lines) == 1  # INFO stays out of the error log
    entry = json.loads(lines[0])
    assert entry["event"] == "measurement.rejected"
    assert (entry["seriesId"], entry["level"]) == (3, "WARNING")
    assert API_KEY not in lines[0]


def test_configure_logging_is_idempotent_and_closes_old_handlers(tmp_path: Path) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        configure_logging("INFO", tmp_path / "errors.log")
        first = next(h for h in root.handlers if isinstance(h, logging.FileHandler))
        configure_logging("INFO", tmp_path / "errors.log")
        assert first.stream is None  # closed, not leaked
        assert sum(isinstance(h, logging.FileHandler) for h in root.handlers) == 1
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


def test_configure_logging_routes_uvicorn_loggers_through_root(tmp_path: Path) -> None:
    root = logging.getLogger()
    saved = {
        n: (list(logging.getLogger(n).handlers), logging.getLogger(n).propagate)
        for n in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    saved_handlers, saved_level = list(root.handlers), root.level
    plain = logging.StreamHandler()
    logging.getLogger("uvicorn").addHandler(plain)
    logging.getLogger("uvicorn").propagate = False
    try:
        logging.getLogger("uvicorn.access").propagate = False  # what --no-access-log does
        configure_logging("INFO")
        for name in ("uvicorn", "uvicorn.error"):
            logger = logging.getLogger(name)
            assert logger.propagate is True
            assert plain not in logger.handlers
        assert logging.getLogger("uvicorn.access").propagate is False  # stays disabled
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        for name, (handlers, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.propagate = propagate
