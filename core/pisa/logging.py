"""Structured JSON logs: one event per line with `event`, `level` and ids.

Pisa's own events go through structlog. Records from the standard library (uvicorn, httpx, DBOS) are rendered
as the same JSON lines by a structlog formatter on the root logger.

No user data in any log line, at any level (debug included): never the user's words, Pisa's reply, a task,
note or event title, an instruction, a name, a place, an address, coordinates, an email, a phone number, a URL
the user gave, a tool's arguments or a model's output, and never a secret, an API key or a token. Log ids,
counts, lengths, hashes, kinds, flags, durations, model refs and error codes instead.

Backstops behind the call sites (which are written to that rule):
- `drop_user_text`: a field named in `DENY` (`text`, `title`, `query`…) keeps its value only when it is a
  count or a flag (an int or a bool); anything else is written as `REDACTED`, at every level, nested too.
- `redact_values` (inside `drop_user_text`): by value, in every string field (nested too) and in another
  library's record's message (its `event`), whatever the key: an email address becomes `{email}`, a phone
  number (`+` and 8 to 15 digits, or an Italian mobile `3xx…` or landline `0x…`, spaces or dashes allowed)
  `{phone}`, a coordinate pair (degrees, four decimals or more), a `lat=`/`lon=` value or a lone decimal
  of five places or more `{coords}`. The patterns are precompiled and run on strings only; a digit run
  touching a letter, `_`, `-`, `.`, `:` or `/` is never a phone, so ids, UUIDs, request ids, hashes, model
  refs, paths, ISO times, durations and counts pass unchanged.
- `safe_exc_info`: a traceback is written with its frames and exception classes only, never an exception's
  message (a database error quotes the row, a not-found quotes what was asked).
Other libraries' records (uvicorn, httpx, the model SDKs, websockets) are written from INFO up only: their
DEBUG lines dump request bodies and frames.
"""

import logging
import re
import sys
import traceback
from collections.abc import Mapping, MutableMapping
from typing import Any, TextIO

import structlog

# Field names that carry text from or for the user. Their value is kept only when it is a count or a flag.
DENY = frozenset({
    "text", "words", "utterance", "reply", "said", "title", "query", "content", "body", "prompt",
    "transcript", "message_text", "question", "summary", "subject", "snippet", "html", "markup", "note_text",
    "instruction", "description", "address", "email", "phone", "lat", "lon", "latitude", "longitude",
    "coordinates", "arguments", "tool_args", "output",
})  # fmt: skip
REDACTED = "[redacted]"
_Event = MutableMapping[str, Any]


def _scrub(value: Any) -> Any:
    """`value` with every denied field inside it (a dict in a list in a dict…) redacted."""
    if isinstance(value, Mapping):
        return {k: (_denied(v) if k in DENY else _scrub(v)) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _denied(value: Any) -> Any:
    """A denied field's value: a count or a flag (int, bool) or nothing (None) stays; anything else goes."""
    return value if value is None or isinstance(value, int) else REDACTED


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# a number touching none of these is free-standing: never part of an id, a path, a time or a decimal
_FREE_START = r"(?<![\w.:/\\-])"
_FREE_END = r"(?![\w:/\\-]|\.\d)"
_SEP = r"[ \-]?"
_PHONE = re.compile(
    _FREE_START
    + r"(?:"
    + rf"(?:\+|00)\d{{1,3}}{_SEP}\(?\d{{1,4}}\)?(?:{_SEP}\d){{6,12}}"  # +39 333 123 4567, 0039…, +1 (555) …
    + rf"|3\d{{2}}{_SEP}\d{{3}}{_SEP}\d{{3,4}}"  # an Italian mobile: 333 123 4567
    + rf"|0\d{{1,3}}(?:{_SEP}\d){{6,9}}"  # an Italian landline: 0 and its area code, 8 digits or more
    + r")"
    + _FREE_END
)
_DEGREES = r"-?(?:1[0-7]\d|\d{1,2})\.\d{3,}"
_COORDS = re.compile(
    rf"{_FREE_START}{_DEGREES}\s*[,;/ ]\s*{_DEGREES}{_FREE_END}"  # a pair of degrees, 4+ decimals each
    + rf"|{_FREE_START}-?(?:1[0-7]\d|[1-9]\d?)\.\d{{5,}}{_FREE_END}"  # a lone decimal of five places or more
)
_KEYED_COORD = re.compile(r"\b(lat|lon|lng|latitude|longitude)(\s*[=:]\s*)-?\d{1,3}(?:\.\d+)?", re.IGNORECASE)
_PLAIN = frozenset({"timestamp", "level", "logger"})  # written by the processors themselves


def redact_values(text: str) -> str:
    """`text` with an email, a phone number and coordinates written as `{email}`, `{phone}`, `{coords}`."""
    if "@" in text:
        text = _EMAIL.sub("{email}", text)
    if not any(c.isdigit() for c in text):
        return text
    text = _KEYED_COORD.sub(r"\1\2{coords}", _COORDS.sub("{coords}", text))
    return _PHONE.sub("{phone}", text)


def _values(value: Any) -> Any:
    if isinstance(value, str):
        return redact_values(value)
    if isinstance(value, Mapping):
        return {k: _values(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list | tuple):
        return [_values(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def drop_user_text(_logger: Any, _name: str, event_dict: _Event) -> _Event:
    """The processor: denied fields redacted, at every level, and emails, phone numbers and coordinates
    redacted by value in any string field and in a library's message (see the module's docstring)."""
    for key, value in list(event_dict.items()):
        if key in _PLAIN:
            continue
        if key == "event":
            event_dict[key] = redact_values(value) if isinstance(value, str) else value
            continue
        event_dict[key] = _denied(value) if key in DENY else _values(_scrub(value))
    return event_dict


def safe_traceback(exc: BaseException) -> str:
    """A traceback without any exception's message: the frames (file, line, function, the code line) and the
    exception classes of the chain."""
    out: list[str] = []
    seen: set[int] = set()
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ or (None if cur.__suppress_context__ else cur.__context__)
    for i, item in enumerate(reversed(chain)):
        if i:
            out.append("\nDuring the handling of it, or from it, another exception:\n")
        out.append("Traceback (most recent call last):\n")
        out.extend(traceback.format_list(traceback.extract_tb(item.__traceback__)))
        out.append(f"{type(item).__module__}.{type(item).__qualname__}\n")
    return "".join(out).rstrip("\n")


def safe_exc_info(_logger: Any, _name: str, event_dict: _Event) -> _Event:
    """`format_exc_info` without the messages: `exc_info` becomes `exception`, frames and classes only."""
    exc_info = event_dict.pop("exc_info", None)
    if not exc_info:
        return event_dict
    if isinstance(exc_info, BaseException):
        exc: BaseException | None = exc_info
    elif isinstance(exc_info, tuple):
        exc = exc_info[1]  # pyright: ignore[reportUnknownVariableType]
    else:
        exc = sys.exc_info()[1]
    if isinstance(exc, BaseException):
        event_dict["exception"] = safe_traceback(exc)
    return event_dict


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


class _OwnDebugOnly(logging.Filter):
    """Another library's DEBUG record (an SDK's request options, a socket's frames) is never written."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.INFO


class _NoQueryStrings(logging.Filter):
    """Access-log lines keep the path but never the query string, which can carry codes, tokens or a
    `deviceId`, nor a private site's signed token, nor an id in the path: the line also names the client's
    address."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], access_path(args[2].split("?", 1)[0]), *args[3:])
        return True


class _MessageHead(logging.Filter):
    """A library's own line keeps what happened, never the words it quotes: DBOS writes an exception into its
    message (`f"Exception encountered in workflow: {e}"`), and an exception's words can quote the user's.
    The message is cut at its first `: ` and its arguments dropped; a traceback keeps its frames
    (`safe_exc_info`)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            head = str(record.getMessage()).split(": ", 1)[0]
        except Exception:  # a message that can't be formatted: its template alone
            head = str(record.msg).split(": ", 1)[0]
        record.msg, record.args = head[:200], ()
        return True


def adopt(name: str) -> None:
    """A library logger that brings its own handler (DBOS: a StreamHandler, propagate off) goes through Pisa's
    root handler instead, so its lines are JSON, its tracebacks have no messages and its words are cut
    (`_MessageHead`). Called after the library sets itself up, and before: a `NullHandler` is left in place,
    because DBOS's `init_logger` installs its own handler (propagate off) only on a logger with none, and
    DBOS's startup recovery can log before `workers.main` adopts it again."""
    lib = logging.getLogger(name)
    lib.handlers[:] = [logging.NullHandler()]
    lib.propagate = True
    if not any(isinstance(f, _MessageHead) for f in lib.filters):
        lib.addFilter(_MessageHead())


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
                safe_exc_info,
                drop_user_text,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    handler.addFilter(_OwnDebugOnly())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []  # propagate to the JSON root handler
        logging.getLogger(name).propagate = True
    adopt("dbos")  # again after DBOS starts (`workers.main`): it puts its own handler back
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
            safe_exc_info,
            drop_user_text,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(stream),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
