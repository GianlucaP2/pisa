"""The database's `CHECK (column IN (…))` lists say the same as the domain Literals they mirror.

A value added to a domain Literal (or the API type built from it) without a migration, or the other way round,
fails here instead of at the first insert. The migrations are read as text, in order: a later
`ALTER TABLE … ADD CONSTRAINT … CHECK (column IN (…))` replaces the earlier list. No database needed."""

import re
from pathlib import Path
from typing import Any, get_args

import pytest

from pisa.domain import approvals, autonomy, connectors, dossier, intentions, locale, vault
from pisa.planner import options

MIGRATIONS = Path(__file__).resolve().parents[1] / "pisa" / "db" / "migrations"
_TABLE = re.compile(r"\b(?:CREATE|ALTER)\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:ONLY\s+)?(\w+)", re.I)
_CHECK_IN = re.compile(r"CHECK\s*\(\s*\(?\s*(\w+)\s+IN\s*\(([^()]*)\)", re.I)
_VALUE = re.compile(r"'([^']*)'")


def check_lists() -> dict[str, frozenset[str]]:
    """`table.column` → the values its CHECK allows, after every migration."""
    lists: dict[str, frozenset[str]] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        for statement in text.split(";"):
            table = _TABLE.search(statement)
            if table is None:
                continue
            for column, values in _CHECK_IN.findall(statement):
                lists[f"{table.group(1)}.{column}"] = frozenset(_VALUE.findall(values))
    return lists


def _values(literal: Any) -> frozenset[str]:
    return frozenset(get_args(literal))


# table.column → the domain definition it mirrors
PAIRS: dict[str, frozenset[str]] = {
    "approvals.kind": _values(approvals.Kind),
    "approvals.status": _values(approvals.Status),
    "approvals.decided_auth": _values(approvals.Auth),
    "approvals.origin": _values(approvals.Origin),
    "autonomy_levels.type": _values(autonomy.ActionType),
    "autonomy_levels.level": _values(autonomy.Level),
    "autonomy_unlocks.from_level": _values(autonomy.Level),
    "connectors.family": _values(connectors.Family),
    "connectors.state": _values(connectors.State),
    "plan_adjustments.action": _values(options.Action),
    "vault_documents.kind": _values(vault.Kind),
    "intentions.kind": _values(intentions.Kind),
    "intentions.state": _values(intentions.State),
    "intentions.source": _values(intentions.Source),
    "intentions.cue": _values(intentions.Cue),
    "intentions.cue_event": _values(intentions.Event),
    "intentions.asked": _values(intentions.Missing),
    "turns.lang": _values(locale.Tag),
    "dossier_items.kind": _values(dossier.Kind),
}


def test_the_migrations_have_check_lists() -> None:
    assert len(check_lists()) > 100


@pytest.mark.parametrize("column", sorted(PAIRS))
def test_a_check_list_matches_its_domain_literal(column: str) -> None:
    lists = check_lists()
    assert column in lists, f"no CHECK (… IN (…)) on {column} in the migrations"
    assert lists[column] == PAIRS[column], (
        f"{column}: only in the database {sorted(lists[column] - PAIRS[column])}, "
        f"only in the domain {sorted(PAIRS[column] - lists[column])}"
    )
