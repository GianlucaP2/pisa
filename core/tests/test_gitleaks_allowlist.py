"""The secret scan's allowlist (`.gitleaks.toml`) holds only the contract's published synthetic test values.

A finding is ignored only when it is in the contract's vector file and its value contains one of the
allowlist's stopwords. Each stopword must be a value the contract publishes as a test vector, and the
allowlist may hold nothing else (no other paths, commits or patterns), so a real secret can never hide behind
it.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".gitleaks.toml"
VECTORS = ROOT / "protocol" / "v2" / "fixtures" / "pairing" / "vectors.json"


def _allowlist() -> dict[str, object]:
    config = tomllib.loads(CONFIG.read_text())
    assert config.get("extend") == {"useDefault": True}, "the default rules stay on"
    assert "rules" not in config and "allowlist" not in config
    allowlists = config["allowlists"]
    assert isinstance(allowlists, list) and len(allowlists) == 1, "one allowlist, for the contract's vectors"
    return allowlists[0]


def test_the_allowlist_holds_stopwords_for_the_vector_file_only() -> None:
    allowlist = _allowlist()
    assert set(allowlist) == {"description", "condition", "paths", "stopwords"}, "no commits or patterns"
    assert allowlist["condition"] == "AND", "a stopword counts only inside the vector file"
    assert allowlist["paths"] == [r"(^|/)protocol/v2/fixtures/pairing/vectors\.json$"]


def test_every_stopword_is_a_published_synthetic_test_value() -> None:
    vectors = VECTORS.read_text()
    stopwords = _allowlist()["stopwords"]
    assert isinstance(stopwords, list) and stopwords
    for word in stopwords:
        assert isinstance(word, str) and len(word) >= 16, f"{word!r}: too short, it could match by chance"
        assert word in vectors, f"{word!r} is not a published test value of the contract's vectors"
