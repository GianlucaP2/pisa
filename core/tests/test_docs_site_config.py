"""docs-site/configuration.md lists exactly the variables the code reads: every `Settings` field in its
first table, every other PISA_ variable the code reads from the environment in its second. Pure: reads files
only."""

import re
from pathlib import Path

from pisa.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs-site" / "configuration.md"


def _tables() -> list[set[str]]:
    """The variable names of each Markdown table on the page, in order."""
    tables: list[set[str]] = []
    inside = False
    for line in PAGE.read_text(encoding="utf-8").splitlines():
        if line.startswith("|"):
            if not inside:
                tables.append(set())
                inside = True
            if m := re.match(r"\| `([A-Z_]+)` \|", line):
                tables[-1].add(m.group(1))
        else:
            inside = False
    return tables


def test_the_settings_table_is_the_settings_class() -> None:
    fields = {f"PISA_{name.upper()}" for name in Settings.model_fields}
    assert _tables()[0] == fields


def test_the_second_table_is_every_other_variable_the_code_reads() -> None:
    read = set()
    for path in (ROOT / "core" / "pisa").rglob("*.py"):
        read |= set(
            re.findall(r"(?:environ\.get|getenv)\(\s*\"(PISA_[A-Z_]+)\"", path.read_text(encoding="utf-8"))
        )
    assert _tables()[1] == read
