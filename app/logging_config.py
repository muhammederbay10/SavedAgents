"""Structured logging: every log record is written as one JSON object per line.

Usage:
    logger = logging.getLogger(__name__)
    logger.info("item_indexed", extra={"item_id": str(item.id), "step": "extract"})

Never log secrets, tokens, raw webhook bodies or personal data. Uvicorn's access
log is switched off for that reason: it holds client addresses and query strings.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

from app.config.base import LogLevel

# Attributes every LogRecord has. Anything else on a record came from `extra=`.
# `message` and `asctime` are only set while a record is being formatted.
_STANDARD_RECORD_ATTRIBUTES = frozenset({*logging.makeLogRecord({}).__dict__, "message", "asctime"})

# Uvicorn gives these loggers their own plain-text handlers and their own level.
# They are routed through the root logger instead, at the configured level, so
# server logs are JSON too and obey `LOG_LEVEL`.
_UVICORN_SERVER_LOGGER_NAMES = ("uvicorn", "uvicorn.error")

# Access records carry the client's address and the full request line, including
# query strings. Both can be personal data or secrets, so they are never written.
_UVICORN_ACCESS_LOGGER_NAME = "uvicorn.access"

# httpx logs every request line, with the full URL, at INFO, and httpcore adds
# connection details at DEBUG. A media link from a webhook is a signed, private
# URL, so these loggers never write below WARNING.
_HTTP_CLIENT_LOGGER_NAMES = ("httpx", "httpcore")
_HTTP_CLIENT_MIN_LEVEL = logging.WARNING


class JsonLogFormatter(logging.Formatter):
    """Formats a log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a record, including any fields passed through `extra=`.

        Args:
            record: The log record to format.

        Returns:
            A JSON string with `timestamp`, `level`, `logger` and `message`,
            the extra fields, and `exception` when the record carries one.
        """
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            (name, value)
            for name, value in record.__dict__.items()
            if name not in _STANDARD_RECORD_ATTRIBUTES
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: LogLevel, stream: TextIO = sys.stdout) -> None:
    """Send logs to one JSON handler on the root logger, at one minimum severity.

    The level is enforced on the handler, so it also holds for libraries that set
    a level on their own loggers. Uvicorn's server logs go through the same
    handler, and its access log is switched off. The HTTP client's request
    lines, which hold full URLs, are only written from WARNING up.

    Calling it again replaces the previous handler, so it is safe to call once
    per application start.

    Args:
        level: Minimum severity to write.
        stream: Where log lines are written.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    handler.setLevel(level)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level)

    for name in _UVICORN_SERVER_LOGGER_NAMES:
        server_logger = logging.getLogger(name)
        server_logger.handlers = []
        server_logger.propagate = True
        server_logger.setLevel(level)

    for name in _HTTP_CLIENT_LOGGER_NAMES:
        logging.getLogger(name).setLevel(
            max(logging.getLevelNamesMapping()[level], _HTTP_CLIENT_MIN_LEVEL)
        )

    _disable_uvicorn_access_log()


def _disable_uvicorn_access_log() -> None:
    """Leave the access logger with no handler to reach.

    This is the state uvicorn's own `--no-access-log` option produces, and
    uvicorn then skips building access records at all.
    """
    access_logger = logging.getLogger(_UVICORN_ACCESS_LOGGER_NAME)
    access_logger.handlers = []
    access_logger.propagate = False
