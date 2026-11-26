"""No literal in the exported tree has the shape of a real credential.

GitHub's secret scanning, gitleaks and trufflehog read the source. A synthetic test value of a real token's
shape (a Tailscale auth key, a Google access token, a JWT, a PEM private key …) is flagged as a leak all the
same, so the tree never holds one as a literal: a test that needs that shape (to check redaction, masking
or parsing) assembles it at run time, from `tests/support/fake_secrets.py`. This test scans every text file of
the export's paths (the repository kit included) for the shapes below, from the scanners' own rules (their
lengths a little shorter, so a near miss is caught too).

`ALLOWED` names the only exceptions: a scanner's or a redactor's own pattern text, never a value.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# the export's allow-list (these and the root's files) and the kit laid over it
EXPORTED = (
    "core", "protocol", "relay", "scripts/ci", "scripts/smoke", "deploy", "docs-site",
    "scripts/pisa-export/kit",
)  # fmt: skip
ROOT_FILES = (".env.example", ".gitignore", ".gitleaks.toml", ".gitleaksignore")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"}

SHAPES = {
    "tailscale-key": r"tskey-[a-z]+-[A-Za-z0-9]",
    "google-access-token": r"ya29\.[A-Za-z0-9_-]",
    "google-refresh-token": r"\b1//[A-Za-z0-9_-]{10,}",
    "google-api-key": r"AIza[0-9A-Za-z_-]{16,}",
    "google-client-secret": r"GOCSPX-[A-Za-z0-9_-]{8,}",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}",  # base64url of '{"': a JWT's header (or any encoded JSON object)
    "openai-key": r"\bsk-(?:proj|svcacct|admin|ant)-[A-Za-z0-9_-]{12,}|\bsk-[A-Za-z0-9]{20,}",
    "xai-key": r"\bxai-[A-Za-z0-9]{20,}",
    "tavily-key": r"\btvly-(?:dev-|prod-)?[A-Za-z0-9]{12,}",
    "github-token": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "slack-token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "aws-key-id": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "stripe-key": r"\b[rs]k_live_[A-Za-z0-9]{10,}",
    "private-key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:\\n|\s)*[A-Za-z0-9+/]{16,}",
}
SHAPE = re.compile("|".join(f"(?P<{name.replace('-', '_')}>{rx})" for name, rx in SHAPES.items()))

# (file, the matched text): a scanner's or a redactor's own pattern, never a value
ALLOWED: dict[tuple[str, str], str] = {}


def _files() -> list[Path]:
    files = [ROOT / name for name in ROOT_FILES if (ROOT / name).is_file()]
    for top in EXPORTED:
        for path in (ROOT / top).rglob("*"):
            if path.is_file() and not SKIP_DIRS & set(path.relative_to(ROOT).parts):
                files.append(path)
    return files


def literal_secrets(text: str) -> list[tuple[int, str, str]]:
    """(line, shape, matched text) for each credential-shaped literal in `text`."""
    return [
        (text.count("\n", 0, m.start()) + 1, str(m.lastgroup).replace("_", "-"), m.group(0))
        for m in SHAPE.finditer(text)
    ]


def test_the_shapes_catch_what_the_scanners_flag() -> None:
    from tests.support import fake_secrets as fake

    flagged = [
        fake.tailscale_key("auth", "synthetic0000", "-", "0" * 16),
        fake.google_access_token("a0AfH6SMBxyz"),
        fake.google_refresh_token("test-refresh-token"),
        fake.google_api_key("SyA1234567890abcdefghijklmn"),
        fake.google_client_secret("test-planted-secret"),
        fake.jwt({"alg": "RS256", "kid": "test"}, {"sub": "1234567890"}),
        fake.openai_project_key("abcdefghijk12345"),
        fake.tavily_key("KEYKEYKEYKEY"),
        fake.pem_private_key("MC4CAQAwBQYDK2VwBCIEIJ+DYvh6SEqVTm50DFtMDoQikTmiCqirVv9mWG9qfSnF").decode(),
    ]
    for value in flagged:
        assert literal_secrets(f'x = "{value}"'), value
    for placeholder in (
        "tskey-...",
        "TS_AUTHKEY=tskey-",
        "sk-test-xxxx",
        "tvly-test",
        "Bearer ya29.",
        "AIza-test-1234",
    ):
        assert literal_secrets(placeholder) == [], placeholder


def test_no_credential_shaped_literal_in_the_exported_tree() -> None:
    found: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary
        rel = path.relative_to(ROOT).as_posix()
        for line, shape, match in literal_secrets(text):
            if (rel, match) in ALLOWED:
                used.add((rel, match))
            else:
                found.append(f"{rel}:{line}: {shape}: {match[:24]}…")
    assert not found, (
        "credential-shaped literals (build them with tests/support/fake_secrets.py):\n" + "\n".join(found)
    )
    assert used == set(ALLOWED), f"stale exceptions: {sorted(set(ALLOWED) - used)}"
