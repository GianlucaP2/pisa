"""The secret scan's allowlist (`.gitleaks.toml`) holds only the contract's published synthetic test values.

A finding is ignored only when its value contains one of the allowlist's stopwords. Each stopword must be a
value the contract publishes as a test vector, and the allowlist may hold nothing else (no paths, commits or
patterns), so a real secret can never hide behind it.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".gitleaks.toml"
VECTORS = ROOT / "protocol" / "v2" / "fixtures" / "pairing" / "vectors.json"
RFC9180_TEST = ROOT / "protocol" / "v2" / "conformance" / "tests" / "test_pairing_bytes.py"


def _allowlist() -> dict[str, object]:
    config = tomllib.loads(CONFIG.read_text())
    assert config.get("extend") == {"useDefault": True}, "the default rules stay on"
    assert "rules" not in config and "allowlists" not in config
    return config["allowlist"]


def _rfc9180_a21_values() -> str:
    """The kit's `RFC` table: RFC 9180 A.2.1, the published DHKEM X25519 / ChaCha20-Poly1305 vector, used by
    its `test_hpke_matches_rfc9180_a21`. The table's values only, nothing else of the file."""
    source = RFC9180_TEST.read_text()
    assert "def test_hpke_matches_rfc9180_a21(" in source and 'RFC["shared_secret"]' in source
    table = re.search(r"^RFC = \{.*?\}$", source, re.S | re.M)
    assert table, "the kit's RFC 9180 A.2.1 table"
    return table.group(0)


def test_the_allowlist_holds_stopwords_only() -> None:
    allowlist = _allowlist()
    assert set(allowlist) == {"description", "stopwords"}, "no paths, commits or patterns are allowed"


def test_every_stopword_is_a_published_synthetic_test_value() -> None:
    vectors, rfc = VECTORS.read_text(), _rfc9180_a21_values()
    stopwords = _allowlist()["stopwords"]
    assert isinstance(stopwords, list) and stopwords
    for word in stopwords:
        assert isinstance(word, str) and len(word) >= 16, f"{word!r}: too short, it could match by chance"
        assert word in vectors or word in rfc, f"{word!r} is not a published test value of the contract"
