"""Every external call's failure is logged with its reason, so what did not work is known at any time (a
provider's bare error code says nothing without it).

One line per failure, `external_failure`, at warning: the provider (`openai`, `xai`, `google_calendar`,
`gmail`, `google_maps`, `apple_maps`, `tavily`, `elevenlabs`, `gemini_live`, `grok_voice`, `apns`,
`open_meteo`, …), the operation, the status (HTTP or the provider's own code) and the provider's own words for
it, cut to 400 characters. A key, a token or a bearer never reaches the line: they are masked before it is
written; a URL keeps its path, never its query string. The user's words are never passed in. The `failures`
report part reads these lines from the journal (24 h, by provider).
"""

import re

from pisa.logging import get_logger

REASON_CHARS = 400
_log = get_logger("pisa.failures")
_SECRETS = re.compile(
    r"(?i)\b[\w-]*(?:api[_-]?key|key|token|secret|password|authorization)\b[\"']?(\s*[=:]\s*[\"']?)"
    r"(?:bearer\s+)?[^\s,;&\"']+"
    r"|(?i:bearer)\s+[\w\-.~+/=]{8,}"
    r"|\bAIza[\w\-]{20,}"  # Google API keys
    r"|\b(?:sk|xai|tvly|pk)-[\w\-]{10,}"  # OpenAI, xAI, Tavily keys
    r"|\bya29\.[\w\-.]+"  # Google OAuth access tokens
    r"|\b1//[\w\-]{20,}"  # Google refresh tokens
    r"|\bGOCSPX-[\w\-]{10,}"  # Google OAuth client secrets
    r"|\beyJ[\w\-]{10,}\.[\w\-]{10,}\.[\w\-]*"  # JWTs (APNs provider tokens)
)
_QUERY = re.compile(r"(https?://[^\s?#\"']+)\?[^\s\"']*")


def _mask(match: re.Match[str]) -> str:
    """`key=abc…` keeps its name (`key=****`); a bare key or token is `****`."""
    if match.group(1) is None:
        return "****"
    return match.group(0)[: match.end(1) - match.start(0)] + "****"


def _without(text: str, secrets: tuple[str | None, ...]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "****")
    return text


def redact(text: str, *secrets: str | None) -> str:
    """`text` with the known secrets (a provider may echo a key back) and anything key-like masked: the one
    redaction, for a log line, an error message or a provider's reason."""
    return _SECRETS.sub(_mask, _without(text, secrets))


def reason_of(text: object, *secrets: str | None) -> str | None:
    """The provider's own words, safe to log: the known secrets and anything key-like masked, URLs without
    their query string, whitespace folded, cut to 400 characters."""
    if text is None:
        return None
    clean = redact(_QUERY.sub(r"\1?…", _without(str(text), secrets)))
    clean = " ".join(clean.split())
    return clean[:REASON_CHARS] or None


def external_failure(
    provider: str, op: str, status: int | str | None = None, reason: object = None, *secrets: str | None
) -> None:
    """Log one external failure (never raises: logging a failure must not become one)."""
    try:
        said = reason_of(reason, *secrets)
    except Exception as exc:  # a bug in the cleaning
        said = f"(reason unreadable: {exc.__class__.__name__})"
    try:
        _log.warning("external_failure", provider=provider, op=op, status=status, reason=said)
    except (OSError, ValueError):  # pragma: no cover - the log stream is closed or broken: nowhere to say it
        pass
