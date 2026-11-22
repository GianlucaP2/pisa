"""Structured JSON logs: one event per line with `event`, `level` and ids.

Pisa's own events go through structlog. Records from the standard library (uvicorn, httpx, DBOS) are rendered
as the same JSON lines by a structlog formatter on the root logger.

Never log secrets, API keys, tokens, or email bodies. Log ids and counts instead.
"""

import logging
import re
import sys
from typing import TextIO

import structlog

_SHARED = [
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
]


_SITE_TOKEN = re.compile(r"^(/b/[^/]+/)[^/]+")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def safe_path(path: str) -> str:
    """A path fit for a log: a private site's signed token (`/b/<slug>/<token>/…`) is never written."""
    return _SITE_TOKEN.sub(r"\1…", path)


def access_path(path: str) -> str:
    """A path fit for an access-log line, which names the client's address: `safe_path`, and no id in it (a
    device id next to an address lets someone push out that device's bound session nonces)."""
    return _UUID.sub("{id}", safe_path(path))


class _NoQueryStrings(logging.Filter):
    """Access-log lines keep the path but never the query string, which can carry codes, tokens or a
    `deviceId`, nor a private site's signed token, nor an id in the path: the line also names the client's
    address."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], access_path(args[2].split("?", 1)[0]), *args[3:])
        return True


def configure_logging(level: str = "info", *, stream: TextIO | None = None) -> None:
    """JSON lines at `level` and above, on `stream` (stdout: the server processes; stderr: the interactive
    commands, whose stdout is for the person)."""
    stream = stream or sys.stdout
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[structlog.stdlib.add_logger_name, *_SHARED],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []  # propagate to the JSON root handler
        logging.getLogger(name).propagate = True
    # httpx/httpcore log every outbound URL with its query at INFO (the user's
    # coordinates to the weather service, their search words): only their warnings
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _NoQueryStrings) for f in access.filters):
        access.addFilter(_NoQueryStrings())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            *_SHARED,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(stream),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
