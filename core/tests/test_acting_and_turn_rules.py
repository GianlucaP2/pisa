"""Pisa acts, Pisa talks like a person, and the turn rules as one algorithm. Each with a test that fails
without its fix."""

import asyncio
import json

from pisa.services.session_messages import Utterance
from tests.conftest import pair


def _tool_calls(messages) -> list[str]:  # type: ignore[no-untyped-def]
    from pydantic_ai.messages import ToolReturnPart

    return [p.tool_name for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


# --- A continued turn keeps what it did -----------------------------------------------


def test_a_continued_turn_never_adds_a_task_twice(client, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai.messages import UserPromptPart
    from pydantic_ai.models.function import DeltaToolCall

    from tests.conftest import set_key
    from tests.live_app import live

    device_id, token = pair(db)
    set_key(db, secrets_key)
    instructions: list[str] = []

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        said = [str(p.content) for m in messages for p in m.parts if isinstance(p, UserPromptPart)][-1]
        instructions.append(str(getattr(messages[-1], "instructions", "") or ""))
        done = _tool_calls(messages)
        if said.startswith("Compra il latte") and "e il pane" not in said:
            if not done:
                yield {
                    0: DeltaToolCall(name="add_task", json_args=json.dumps({"title": "Comprare il latte"}))
                }
            else:
                yield "Aggiunto. "
                await asyncio.sleep(30)  # the user goes on before this reply ends
                yield "mai."
        elif not done:  # the joined turn: a model that adds both again
            yield {0: DeltaToolCall(name="add_task", json_args=json.dumps({"title": "Comprare il latte"}))}
            yield {1: DeltaToolCall(name="add_task", json_args=json.dumps({"title": "Comprare il pane"}))}
        else:
            yield "Latte e pane in lista."

    fake_models.default = reply
    with live(client, token, device_id) as app:
        first = Utterance(text="Compra il latte", lang="it-IT", final=True, source="phone_mic")
        app.utterance(first)
        assert app.next()["reply_to"] == first.id  # "Aggiunto." said: the task is there
        more = Utterance(
            text="Compra il latte e il pane", lang="it-IT", final=True, source="phone_mic", continues=first.id
        )
        app.utterance(more)
        list(app.reply())
    titles = sorted(r[0] for r in db.execute("SELECT title FROM tasks").fetchall())
    assert titles == ["Comprare il latte", "Comprare il pane"]  # not "latte" twice
    assert "Already done in this turn" in instructions[-1] and "«Comprare il latte»" in instructions[-1]


# --- The LOW rows ---------------------------------------------------------------------------


def test_a_quote_is_whole_words_long_enough_never_negated_and_never_from_a_gist() -> None:
    from pisa.domain.reading import checked, found, rates_itself

    email = "Ciao, questo non è URGENTE ma serve il preventivo per favore entro venerdì."
    assert not found("URGENTE", email)  # lifted from "non è URGENTE"
    negated = "Il preventivo non è una richiesta molto urgente, puoi farlo con comodo."
    assert not found("una richiesta molto urgente", negated)  # long enough, but right after "non è"
    assert found("una richiesta molto urgente", "Ecco una richiesta molto urgente da gestire.")
    assert not found("e", email) and not found("ore", email)  # one letter; inside "favore"
    assert found("il preventivo per favore", email)
    gist_line = "2026-10-20, from a@b.example: «Preventivo cucina» — una richiesta urgente"
    kept = checked(
        {
            "stakes": {"value": "urgente", "quote": "una richiesta urgente"},  # Pisa's own gist: no evidence
            "history": {"value": "segue un preventivo", "quote": "Preventivo cucina"},  # the subject: yes
        },
        email,
        ["Preventivo cucina"],
    )
    assert kept is not None and kept["stakes"]["value"] == "unknown" and kept["history"]["quote"]
    # (the line Pisa gives the model has it, the evidence doesn't)
    assert "una richiesta urgente" in gist_line
    for paraphrase in ("Nessuna fretta, davvero", "no rush at all", "handle it whenever", "low importance"):
        assert rates_itself(paraphrase), paraphrase


def test_undoing_a_rule_reopens_what_it_dismissed_that_day(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from tests.assistant_env import add_task
    from tests.test_email_precision import add_email

    auth = {"Authorization": f"Bearer {pair(db)[1]}"}

    def email_task(mid: str, title: str) -> str:
        add_email(db, mid, title, "billing@ovh.example")
        task = add_task(db, title)
        db.execute("UPDATE tasks SET source_kind = 'email', source_ref = %s WHERE id = %s", (mid, task))
        return task

    def status(task: str) -> str:
        return db.execute("SELECT status FROM tasks WHERE id = %s", (task,)).fetchone()[0]

    tapped, other = email_task("b1", "Rinnova il VPS"), email_task("b2", "Fattura OVH")
    rule_id = client.post(f"/v1/tasks/{tapped}/dismiss", headers=auth, json={"never": "sender"}).json()[
        "rule_id"
    ]
    assert status(other) == "dismissed"  # the rule swept it
    assert client.delete(f"/v1/rules/{rule_id}", headers=auth).status_code == 204
    assert status(other) == "open" and status(tapped) == "dismissed"  # the user dismissed that one by hand
    assert client.delete(f"/v1/rules/{rule_id}", headers=auth).status_code == 204  # again: done, not 404
    # Made more than 24 h ago: the rule goes, what it dismissed stays dismissed.
    third = email_task("b3", "Avviso OVH")
    rule_id = client.post(f"/v1/tasks/{other}/dismiss", headers=auth, json={"never": "sender"}).json()[
        "rule_id"
    ]
    assert status(third) == "dismissed"
    clock.now += timedelta(hours=25)
    assert client.delete(f"/v1/rules/{rule_id}", headers=auth).status_code == 204
    assert status(third) == "dismissed"


def test_nearby_applies_open_now_and_wraps_the_box() -> None:
    from pisa.services.places import nearby_box

    assert nearby_box(45.0, 9.0) == ((44.95, 45.05), [(8.95, 9.05)])
    lats, lons = nearby_box(89.99, 179.98)
    assert lats[1] == 90.0
    assert [(round(a, 2), round(b, 2)) for a, b in lons] == [(179.93, 180.0), (-180.0, -179.97)]
    lons = nearby_box(0.0, -179.99)[1]
    assert [(round(a, 2), round(b, 2)) for a, b in lons] == [(-180.0, -179.94), (179.96, 180.0)]


def test_nearby_open_now_is_applied(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.test_planner import place

    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    for n in range(3):
        place(db, f"Posto {n}", (45.418 + 0.0001 * n, 9.232))
    near = {"near": "45.418,9.2320"}
    assert len(client.get("/v1/places/search", headers=auth, params=near).json()["results"]) == 3
    # Pisa knows no opening hours for them: with open_now none is said to be open (it was ignored).
    shut = client.get("/v1/places/search", headers=auth, params=near | {"open_now": "true"})
    assert shut.status_code == 200 and shut.json()["results"] == []


# --- the turn rules as one algorithm ------------------------------------------------


def test_turn_rules_cases_are_the_shared_algorithm() -> None:
    from pathlib import Path

    from pisa.domain import turn

    cases = json.loads(
        (
            Path(__file__).resolve().parents[2] / "protocol" / "fixtures" / "turn-rules" / "cases.json"
        ).read_text()
    )["cases"]
    assert len(cases) >= 40
    wrong = [(c["text"], c["lang"], turn.kind(c["text"], c["lang"]), c["kind"]) for c in cases]
    assert [w for w in wrong if w[2] != w[3]] == []
    # The parts beyond the word lists, each on its own:
    assert (
        turn.kind("prenota un tavolo per l\u2019", "it-IT") == "unfinished"
    )  # the curly apostrophe read as '
    assert turn.kind("devo comprare il latte,", "it-IT") == "unfinished"  # a comma
    assert turn.kind("domani piove?", "it-IT") == "complete"  # "?"
    assert turn.kind("ok, chiama Marco", "it-IT") == "complete"  # an opener skipped
    assert turn.kind("dove sei", "it-IT") == "other" and turn.kind("chiama", "it-IT") == "other"  # too short
    assert turn.WAITS_MS["complete"] == 1200
