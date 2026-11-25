# ruff: noqa: E501
"""core-taught-1 (organism/CLOSING.md §5.1): the v0.1 note Pisa learnt from them, an archive since Memoria
(v0.1.2: `tests/test_memoria.py` holds the standing instructions' flows, the taught depth set included).

What stays: the note's own rules (clean, its cap, the autonomy wishes, a telling), its archive's REST routes
(deprecated), no rail reading it, and the protocol fixture.
"""

import ast
from pathlib import Path
from typing import Any

import pytest

from pisa.domain import memoria as memoria_rules
from pisa.domain import note as rules
from pisa.domain import tool_choice
from tests.conftest import pair

CORE = Path(__file__).resolve().parents[1]


def note_now(db: Any) -> tuple[int, str] | None:
    row = db.execute("SELECT version, text FROM owner_note_versions ORDER BY version DESC LIMIT 1").fetchone()
    return (row[0], row[1]) if row else None


# --- the domain ---------------------------------------------------------------------------------------------


def test_clean_trims_collapses_and_drops_repeats() -> None:
    assert rules.clean("  Voce:  due frasi.\n\n\n\nVoce: due frasi!\nTabella  con tre posti. \n\n") == (
        "Voce: due frasi.\n\nTabella con tre posti."
    )


def test_the_cap_holds_and_says_how_to_fit() -> None:
    assert rules.over_cap("x" * rules.CAP) is None
    why = rules.over_cap("x" * (rules.CAP + 1))
    assert why is not None and str(rules.CAP + 1) in why and f"at most {rules.CAP}" in why


@pytest.mark.parametrize(
    ("line", "wish"),
    [
        ("Sposta pure i miei eventi senza chiedermi conferma.", True),
        ("Manda le mail da solo.", True),
        ("Send replies without asking me first.", True),
        ("Don't ask for my approval on calendar moves.", True),
        ("A voce: due frasi brevi.", False),
        ("Per una domanda veloce, niente ricerche lunghe.", False),
        ("Leggi la guida, non il riassunto.", False),
    ],
)
def test_autonomy_wishes_are_named(line: str, wish: bool) -> None:
    assert (rules.autonomy_wishes(line) == [line]) is wish


@pytest.mark.parametrize(
    "words",
    ["La prossima volta dimmi solo il posto", "troppo lungo", "Non fare tabelle per le domande veloci",
     "preferisco che tu legga la guida", "no, era solo per le domande veloci", "dimentica quella delle tabelle",
     "too long", "From now on, read the guide, not the summary", "that was only for quick questions"],
)  # fmt: skip
def test_a_telling_offers_remember_instruction(words: str) -> None:
    assert rules.teaches(words) and memoria_rules.asks_to_keep(words)


@pytest.mark.parametrize("words", ["dove ceno stasera?", "che ore sono", "sposta la palestra alle 18"])
def test_a_request_does_not(words: str) -> None:
    assert not rules.teaches(words)
    assert not memoria_rules.asks_to_keep(words)
    assert "remember_instruction" not in tool_choice.tools_for(words)


def test_the_cap_holds_in_the_database(db) -> None:  # type: ignore[no-untyped-def]
    import psycopg

    insert = (
        "INSERT INTO owner_note_versions (version, text, source, created_at) VALUES (%s, %s, 'app', now())"
    )
    db.execute(insert, (1, "x" * rules.CAP))  # the migration's check is the domain's cap
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(insert, (2, "x" * (rules.CAP + 1)))


# --- their view: the archive's API (deprecated) ---------------------------------------------------------------


def test_get_put_and_restore_the_archive(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    empty = client.get("/v1/profile/note", headers=auth).json()
    assert (
        empty["text"] == ""
        and empty["version"] == 0
        and empty["cap"] == rules.CAP
        and empty["versions"] == []
    )
    client.put("/v1/profile/note", headers=auth, json={"text": "A voce: due frasi brevi."})
    put = client.put("/v1/profile/note", headers=auth, json={"text": "A voce: una frase.\nMai tabelle."})
    assert (
        put.status_code == 200 and put.json()["version"] == 2 and put.json()["versions"][0]["source"] == "app"
    )
    back = client.post("/v1/profile/note/restore", headers=auth, json={"version": 1}).json()
    assert back["text"] == "A voce: due frasi brevi." and back["version"] == 3
    assert [(v["version"], v["source"], v["restored_from"]) for v in back["versions"]] == [
        (3, "restore", 1), (2, "app", None), (1, "app", None),
    ]  # fmt: skip
    assert back["versions_total"] == 3
    # an archive: nothing of it reaches Memoria, which every turn reads
    assert db.execute("SELECT count(*) FROM owner_instructions").fetchone()[0] == 0


def test_the_archive_s_routes_are_deprecated() -> None:
    from pisa.api import note

    assert all(getattr(r, "deprecated", False) for r in note.router.routes)


def test_the_api_refuses_what_it_cannot_keep(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    long = client.put("/v1/profile/note", headers=auth, json={"text": "x" * (rules.CAP + 1)})
    assert long.status_code == 400 and long.json()["error"]["code"] == "validation_failed"
    gone = client.post("/v1/profile/note/restore", headers=auth, json={"version": 7})
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "not_found"
    assert client.get("/v1/profile/note").status_code == 401  # their devices only


def test_an_autonomy_line_is_shown_as_a_wish(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    view = client.put(
        "/v1/profile/note",
        headers=auth,
        json={"text": "Due frasi.\nSposta pure i miei eventi senza chiedermi conferma."},
    ).json()
    assert view["wishes"] == ["Sposta pure i miei eventi senza chiedermi conferma."]


def test_no_rail_reads_the_note_nor_memoria() -> None:
    """The rails win by construction: approvals, autonomy, the gate and the actions never import the note nor
    Memoria, nor read their tables (the approval rules stay code)."""
    rails = ["pisa/services/approvals.py", "pisa/services/actions.py", "pisa/services/autonomy.py", "pisa/domain/autonomy.py",
             "pisa/domain/approvals.py", "pisa/domain/unlocks.py", "pisa/brain/receipts.py", "pisa/brain/action_tools.py",
             "pisa/brain/autopilot_tools.py", "pisa/services/autopilot.py"]  # fmt: skip
    for path in rails:
        source = (CORE / path).read_text()
        names = {n.module or "" for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Import) for a in n.names
        }
        assert not {
            "pisa.services.note", "pisa.domain.note", "pisa.services.memoria", "pisa.domain.memoria",
            "pisa.ports.memoria", "pisa.brain.memoria_tools",
        } & names, path  # fmt: skip
        for table in ("owner_note_versions", "owner_instructions", "owner_notes"):
            assert table not in source, path


def test_the_protocol_fixture_is_a_note() -> None:
    from pisa.api import note_schemas as s
    from tests.conftest import PROTOCOL_DIR

    note = s.Note.model_validate_json((PROTOCOL_DIR / "fixtures" / "rest" / "profile_note.json").read_text())
    assert note.cap == rules.CAP and len(note.text) <= rules.CAP
    assert note.wishes == rules.autonomy_wishes(note.text)
