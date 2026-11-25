"""the rows B1 (someone else's plans with any verb), B2 (every write tool gives a
receipt), B4 (a proposal corrected under another title is withdrawn by its id); `owner_said` required; the
skills; prompt v2; the tools on research turns; the replay's scorer."""

from datetime import date, datetime

from pisa.domain.actions import Change, owner_said
from tests.test_brain_probes import _device, google, rows, turn_with  # noqa: F401  (google: a fixture)

TODAY = date(2026, 10, 30)


def said(words: str, title: str, start: datetime) -> str | None:
    return owner_said(
        Change("calendar_create", title, start, quote=words), words, default_day=TODAY, today=TODAY
    )


# --- B1: someone else's plan with any verb -----------------------------------------------------------------


def test_someone_else_s_plan_with_any_verb_is_no_day_and_no_command() -> None:
    # "Marco parte domani" is Marco's: the dinner is today, never tomorrow
    assert said("Marco parte domani, metti la cena alle 20", "Cena", datetime(2026, 10, 30, 20)) is None
    assert said("Marco parte domani, metti la cena alle 20", "Cena", datetime(2026, 10, 31, 20)) is not None
    # under an earlier "metti", Giulia's and Marco's clauses command nothing
    for words, title, start in (
        ("metti la cena alle 20, Giulia viene alle 19", "Giulia", datetime(2026, 10, 30, 19)),
        ("metti la cena alle 20, Marco vuole la call alle 4", "Call", datetime(2026, 10, 30, 16)),
    ):
        assert "someone else's plans" in (said(words, title, start) or "")
    # the user's own clauses still act, the user among others too ("Marco e io": no "Marco è")
    assert said("domani, metti la cena alle 20", "Cena", datetime(2026, 10, 31, 20)) is None
    both = "Marco e io andiamo a Milano domani, metti il treno alle 9"
    assert said(both, "Treno", datetime(2026, 10, 31, 9)) is None
    assert said(both, "Treno", datetime(2026, 10, 30, 9)) is not None
    assert said("Nino viene con me domani, metti la cena alle 20", "Cena", datetime(2026, 10, 31, 20)) is None
    night = "metti tutto sul calendario: domani palestra alle 10, pranzo con Nino alle 13 e dentista alle 17"
    assert said(night, "Pranzo con Nino", datetime(2026, 10, 31, 13)) is None
    assert said("metti la cena con Nino alle 20", "Cena con Nino", datetime(2026, 10, 30, 20)) is None


# --- B2: every write tool gives Pisa's result line with a receipt
# ---------------------------------------------


def test_a_write_that_answers_in_words_gets_its_receipt_or_its_error() -> None:
    from pisa.brain.receipts import RECEIPTED, receipt_for
    from pisa.domain import results

    assert RECEIPTED <= results.WRITE_TOOLS
    done = receipt_for("set_quiet_mode", "Quiet mode is on until 15:00 (2026-10-20).")
    assert results.status_of(done) == results.DONE
    assert results.receipt_of(done) == "set_quiet_mode: Quiet mode is on until 15:00 (2026-10-20)."
    assert results.words(done) == "Quiet mode is on until 15:00 (2026-10-20)."
    for words in ("Not skipped: Nothing in today's plan matches 'x'.", "Refused: only the owner …",
                  "Several open tasks match: a, b. Which one?"):  # fmt: skip
        assert results.status_of(receipt_for("skip_item", words)) == results.ERROR
    framed = "rule\n<tasks-1>\nRenamed: «Pane».\n</tasks-1>"
    inside = receipt_for("rename_task", framed)
    assert inside.endswith("\n</tasks-1>") and results.status_of(inside) == results.DONE  # inside the block
    already = results.within("Added.", results.done(None, "task added: «Pane»"))
    assert receipt_for("tasks_bulk", already) == already  # a tool's own line stays as it is

    drafted = results.receipt_of(receipt_for("draft_email", "Draft to Anna saved.")) or ""
    assert "NOT sent" in drafted and "draft saved" in drafted  # never bare done


def test_the_receipt_reaches_the_turn_s_results(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import results
    from tests import test_conversation_tools as tools

    seen: list[str] = []
    real = tools.said_back

    def keep(text: str) -> str:
        seen.append(text)
        return real(text)

    tools.said_back = keep  # type: ignore[assignment]
    try:
        tools.ask(db, secrets_key, "set_quiet_mode", {"until": "15:00"})
    finally:
        tools.said_back = real  # type: ignore[assignment]
    (raw,) = seen
    assert results.status_of(raw) == results.DONE
    assert (results.receipt_of(raw) or "").startswith("set_quiet_mode: Quiet mode is on until 15:00")


# --- B4: a correction under another title withdraws the proposal by its id ---------------------------------


def test_a_correction_under_another_title_withdraws_the_held_proposal(  # type: ignore[no-untyped-def]
    client,
    db,
    google,  # noqa: F811  (the fixture)
    clock,
) -> None:
    from pisa.domain import results
    from tests.assistant_env import rome

    clock.now = rome(2026, 10, 20, 13)
    dev, a = _device(db), client.app.state.runtime.actions
    turn_with(client, a.create_event, "Call con Marco", "domani alle 16",
              said="leggi la mail di Marco e metti la call con Marco domani alle 16", outside="email",
              device=dev)  # fmt: skip
    first = rows(db)[0][0]
    turn_with(client, a.create_event, "Call Marco", "domani alle 17", said="mettila alle 17", device=dev,
              owner_said="mettila alle 17", replaces=results.short(first))  # fmt: skip
    states = {r[1]: r[2] for r in rows(db)}
    assert states["Call con Marco"] == "superseded"  # a later yes can't make it twice
    assert states["Call Marco"] == "pending" and not google.writes  # held, as its correction was


# --- skills: retrieval, pins, carry-over, pgvector, the turn's dynamic block -------------------------------


def test_skills_load_reviewed_entries_and_name_only_pisa_s_tools() -> None:
    import re

    from pisa.brain import skills

    found = skills.load()
    # the 15 starter ones, research (the guide), the 4 moved from the old prompt's Tools section, the people
    # and world-handoffs; place-notes, rituals (reviewed), vault, wishes,
    # autonomy; the money-back; the public-data; world-carts; the places-of-my-life,
    # R's accountant; the drawing; the drawing-blocks
    assert len(found) == 34 and all(s.status == "reviewed" for s in found)
    from pisa.domain.tools import REGISTRY

    names = set(REGISTRY)  # every tool the brain can offer (`@offer` refuses any other)
    for path in sorted(skills.SKILLS_DIR.glob("*.md")):  # the front matter's `tools:` line
        listed = re.search(r"^tools:(.*)$", path.read_text(encoding="utf-8"), re.MULTILINE)
        line = listed.group(1).split("  #")[0] if listed else ""
        named = {t.strip() for t in line.split(",") if t.strip()}
        assert named <= names, (path.name, named - names)


def test_skills_retrieval_by_words_the_night_pin_and_a_short_follow_up() -> None:
    from pisa.brain.skills import Library, contexts, load

    lib = Library(load())
    top = [s.name for _, s in lib.search("metti dentista domani alle 10 in calendario")]
    assert top[0] == "calendar-create"
    assert "night-domani" in [s.name for _, s in lib.search("segna la call", context=contexts(2, False))]
    assert "night-domani" not in [s.name for _, s in lib.search("segna la call", context=contexts(14, False))]
    carried = [s.name for _, s in lib.search("quella di stasera", previous=["calendar-move"])]
    assert "calendar-move" in carried  # a short follow-up continues the previous turn's skill
    assert "calendar-move" not in [s.name for _, s in lib.search("quella di stasera")]
    research = [s.name for _, s in lib.search("trovami un bar aperto adesso", context=contexts(14, True))]
    assert research[0] == "research"  # the guide: pinned once a research tool was called
    assert "research" not in [s.name for _, s in lib.search("trovami un bar aperto adesso")]


def test_skills_by_embeddings_in_pgvector_and_back_to_trigrams_on_a_failure(db) -> None:  # type: ignore[no-untyped-def]
    import asyncio
    import hashlib

    from pisa.db.pool import open_pool
    from pisa.services.skills import SkillsService
    from tests.conftest import dsn

    calls: list[tuple[int, str]] = []

    async def embed(texts, kind):  # type: ignore[no-untyped-def]
        calls.append((len(texts), kind))
        out = []
        for t in texts:  # a fake model: "calendar" words point one way, the rest another
            cal = any(w in t.lower() for w in ("calendar", "calendario", "metti", "segna", "appuntamento"))
            h = int(hashlib.sha256(t.encode()).hexdigest()[:4], 16) / 65536
            out.append([1.0, 0.0, h * 0.01] if cal else [0.0, 1.0, h * 0.01])
        return "openai:fake-embed", out

    async def broken(texts, kind):  # type: ignore[no-untyped-def]
        raise RuntimeError("down")

    async def go() -> tuple[list[str], list[str], list[str]]:
        pool = await open_pool(dsn(db), max_size=2)
        try:
            service = SkillsService(pool, embed)
            first = [s.name for _, s in await service.find("segna in calendario la cena", None, 14)]
            again = [s.name for _, s in await service.find("metti in calendario", None, 14)]
            down = SkillsService(pool, broken)
            fallback = [s.name for _, s in await down.find("metti dentista in calendario", None, 14)]
            return first, again, fallback
        finally:
            await pool.close()

    from pisa.brain.skills import load

    unpinned = len([s for s in load() if not s.pin])
    first, again, fallback = asyncio.run(go())
    stored = db.execute("SELECT count(*), min(model_ref) FROM skill_vectors").fetchone()
    assert stored == (unpinned, "openai:fake-embed")  # every unpinned skill, once
    assert calls[:2] == [(1, "query"), (unpinned, "document")] and calls[2:] == [(1, "query")]  # made once
    assert first and again and fallback  # the trigram index answers when embeddings fail
    assert "calendar-create" in fallback


def test_the_turn_s_skills_are_after_the_cached_prefix(client, db, fake_models, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain.conversation import system_prompt
    from tests.test_acting import socket_turns
    from tests.test_talk_timeline import Heard

    model = Heard()
    fake_models.default = model
    socket_turns(client, db, secrets_key, model, "metti il dentista in calendario domani alle 10")  # type: ignore[arg-type]
    seen = model.instructions[0]
    assert seen.startswith(system_prompt(lang="it-IT"))  # the cached prefix stays byte-identical
    assert (
        "How Pisa usually does this" in seen and "Create events" in seen[len(system_prompt(lang="it-IT")) :]
    )


# --- B5, replaced by the cache rule: a research turn keeps its tools (its thinking is bound to them) --------


def test_after_a_research_tool_the_requests_keep_the_same_tools(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    import json

    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall

    from tests.test_follow_tools import ask

    offered: list[list[tuple[str, int]]] = []

    async def model(messages, info):  # type: ignore[no-untyped-def]
        offered.append(
            [
                (t.name, len(json.dumps(t.parameters_json_schema)) + len(t.description or ""))
                for t in info.function_tools
            ]
        )
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield {0: DeltaToolCall(name="map_search", json_args=json.dumps({"query": "bar"}))}
            return
        yield "Ecco."

    ask(db, secrets_key, "map_search", {"query": "bar"}, model=model)
    first = offered[0]
    # every tool of the first request stays, in one order (search_tools only adds the ones it found)
    assert len(offered) >= 2 and all([t for t in o if t in first] == first for o in offered)
    assert {"show_plan", "get_tasks", "show_places", "map_search", "finish"} <= {n for n, _ in offered[-1]}


# --- row 5: owner_said required (ready, off until the replay shows the count) ------------------------------


def test_owner_said_required_holds_a_write_without_the_owner_s_words(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    from pisa.domain import actions as rules

    words, start = "metti il dentista domani alle 10", datetime(2026, 10, 31, 10)
    given = {"default_day": TODAY, "today": TODAY, "start": start, "title": "Dentista", "read_outside": False,
             "attendees": []}  # fmt: skip
    assert rules.grounding("calendar_create", words, **given)[0] == "words"  # off: every clause is tried
    monkeypatch.setattr(rules, "OWNER_SAID_REQUIRED", True)
    mode, why = rules.grounding("calendar_create", words, **given)
    assert mode == "held" and why.startswith("no owner_said")
    assert (
        rules.grounding("calendar_create", words, quote="il dentista domani alle 10", **given)[0] == "words"
    )


# --- the replay: its claim rules are Pisa's
# -------------------------------------------------------------------
