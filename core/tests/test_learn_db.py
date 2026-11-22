"""Pisa Learn with the test Postgres: import (idempotent, background, candidates, needs their copy, a study
plan never
cited), the index (chunks, prefixes, tsvector, vectors or lexical only), retrieval (Italian question on an
English corpus, the exact-number boost, appendices, one hop along citations), the cited answer with the judge
and the three tiers, the answer log, the erase family, the REST API, the course-turn tools and the evaluation
metrics on synthetic papers. Every word is synthetic; no network, no paid call."""

import asyncio
import base64
import uuid
from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest

from pisa.brain.conversation import TurnContext, build_agent
from pisa.brain.profile_tools import ProfileTurn
from pisa.domain import learn as rules
from pisa.infra import hard_limit
from pisa.ports.learn import Candidate, Meta
from pisa.services import erase
from pisa.services.learn import LearnService
from pisa.services.learn_ask import GatewayLearnModels, LearnAsk
from pisa.services.learn_search import LearnSearch
from pisa_tools import learn_eval as ev
from tests.conftest import dsn, pair, seed_catalog
from tests.learn_fakes import INJECTED, PAPER_A, FakeFetch, FakeModels, fake_embed, find
from tests.owner import owner_at

NOW = datetime(2026, 11, 8, 10, tzinfo=UTC)


async def _pool(db: psycopg.Connection) -> Any:
    from pisa.db.pool import open_pool

    pool = await open_pool(dsn(db), max_size=6)
    await pool.wait()
    return pool


class World:
    def __init__(self, pool: Any, fetch: FakeFetch, models: FakeModels, embed: Any) -> None:
        self.pool, self.fetch, self.models = pool, fetch, models
        self.learn = LearnService(
            pool,
            fetch,
            embed=embed,
            seal=lambda b: b"sealed:" + b[::-1],
            unseal=lambda b: b.removeprefix(b"sealed:")[::-1],
            clock=lambda: NOW,
        )
        self.search = LearnSearch(pool, models, embed)
        self.ask = LearnAsk(pool, self.search, models, owner=owner_at(), clock=lambda: NOW)

    async def add(self, *items: str, course: str | None = "probes", **kw: Any) -> list[dict[str, Any]]:
        await self.learn.add(list(items), course=course, **kw)
        await self.learn.settled()
        return await self.learn.sources(course)


def run(db: psycopg.Connection, body: Any, *, embed: Any = None, models: FakeModels | None = None) -> Any:
    async def go() -> Any:
        pool = await _pool(db)
        try:
            return await body(World(pool, FakeFetch(), models or FakeModels(), embed))
        finally:
            await pool.close()

    return asyncio.run(go())


# --- 1-2. sources and import --------------------------------------------------------------------------------


def test_an_arxiv_source_is_read_and_indexed_once(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        rows = await w.add("arXiv:2601.00001")
        (src,) = rows
        assert src["state"] == "ready" and src["title"] == "Probing Gadget Models for Truth"
        assert src["origin"] == "arxiv_html" and src["licence"] == "CC BY 4.0" and src["citable"]
        assert src["lexical_only"] and src["segments"] > 5 and src["chunks"] > 3
        assert w.fetch.calls[:3] == ["meta:2601.00001", "html:2601.00001", "pdf:2601.00001"]
        # the same paper again (another version, its URL): one source, nothing fetched again
        w.fetch.calls.clear()
        again = await w.add("https://arxiv.org/abs/2601.00001v2")
        assert [r["id"] for r in again] == [src["id"]] and w.fetch.calls == []
        # ingested again by hand (a restart): unchanged text, not indexed again
        async with w.pool.connection() as conn:
            before = await (await conn.execute("SELECT min(id) FROM learn_chunks")).fetchone()
        assert await w.learn.ingest(src["id"]) == "ready"
        async with w.pool.connection() as conn:
            after = await (await conn.execute("SELECT min(id) FROM learn_chunks")).fetchone()
            kinds = {r[0] for r in await (await conn.execute("SELECT kind FROM learn_chunks")).fetchall()}
            pre = await (
                await conn.execute("SELECT prefix_it, prefix_en FROM learn_chunks WHERE kind = 'table'")
            ).fetchone()
        assert before == after
        assert {"table", "equation", "caption", "appendix", "paragraph"} <= kinds
        assert pre is not None and "Tabella 3" in pre[0] and "Table 3" in pre[1]

    run(db, body)


def test_ingestion_runs_in_the_background_and_resumes(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        async with w.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO learn_sources (id, key, kind, ref, arxiv_id, state) "
                "VALUES (%s, 'arxiv:2601.00002', 'arxiv', '2601.00002', '2601.00002', 'fetching')",
                (uuid.uuid4(),),
            )
        await w.learn.resume()  # a restart: the interrupted source is read again
        await w.learn.settled()
        (src,) = await w.learn.sources()
        assert src["state"] == "ready"

    run(db, body)


def test_sources_are_ingested_one_at_a_time_and_a_crash_is_named(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        rows = await w.add("2601.00001", "2601.00002", "2601.00003")
        assert [r["state"] for r in rows] == ["ready"] * 3 and w.fetch.most == 1
        w.fetch.broken = True  # a fetch or parse that breaks: failed, its cause named
        rows = await w.add("2601.00009", course="other")
        assert rows[0]["state"] == "failed" and rows[0]["error"].startswith("internal: ingestion failed")
        w.fetch.broken = False

        async def index_breaks(*args: Any) -> str:
            raise RuntimeError("a bug while indexing")

        w.learn._index = index_breaks  # type: ignore[method-assign]
        rows = await w.add("2601.00002", course="third")  # never left `queued`
        assert rows[0]["state"] == "failed" and rows[0]["error"].startswith("internal: ingestion failed")

    run(db, body)


def test_an_ambiguous_title_comes_back_as_candidates_never_picked(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        w.fetch.titles = [
            Candidate("Probing gadget models", 2025, ("Verdi",), None, "2601.00001"),
            Candidate("Probing gadget models revisited", 2026, ("Neri",), "10.1/x", None),
        ]
        rows = await w.add("Probing gadget", course=None)
        assert rows[0]["state"] == "candidates" and len(rows[0]["candidates"]) == 2
        assert rows[0]["candidates"][0]["item"] == "arXiv:2601.00001"
        assert not any(c.startswith("html:") for c in w.fetch.calls)  # nothing was read
        # one exact match: taken
        rows = await w.add("Probing Gadget Models", course="c2")
        assert rows[0]["state"] == "ready" and rows[0]["arxiv_id"] == "2601.00001"

    run(db, body)


def test_a_doi_without_an_open_copy_needs_his_copy(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        rows = await w.add("10.9999/closed.1")
        assert rows[0]["state"] == "needs_copy" and "Upload your copy" in rows[0]["error"]
        assert w.fetch.calls == ["unpaywall:10.9999/closed.1", "openalex:10.9999/closed.1"]
        # an open copy: read
        w.fetch.open["10.9999/open.2"] = Meta(
            "Open gadgets", ["Bo Neri"], 2024, "cc-by", None, None, None, "https://example.org/open"
        )
        w.fetch.pages["https://example.org/open"] = (
            "text/html",
            b"<html><title>Open gadgets</title><h2>1 Intro</h2><p>Gadgets are open.</p></html>",
        )
        rows = await w.add("doi:10.9999/open.2", course="c3")
        assert rows[0]["state"] == "ready" and rows[0]["licence"] == "cc-by" and rows[0]["parser"] == "html"

    run(db, body)


def test_a_study_plan_is_a_hint_never_a_source(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        plan = b"# Piano\n\n## Settimana 1\nI layer 5 e 9 sono i migliori per sondare la verita.\n"
        rows = await w.add(course="probes", upload=("plan.md", "text/markdown", plan))
        assert rows[0]["kind"] == "markdown" and not rows[0]["citable"] and rows[0]["state"] == "ready"
        async with w.pool.connection() as conn:  # Their file is kept sealed, read back through Pisa's key
            stored = await (await conn.execute("SELECT upload FROM learn_sources")).fetchone()
        assert stored is not None and bytes(stored[0]) == b"sealed:" + plan[::-1]
        found = await w.search.search("layer 5 e 9", "probes")
        assert found.passages == []  # never searched, never cited
        await w.add("2601.00001")  # a citable paper beside it: the plan's own words still find nothing of it
        found = await w.search.search(
            "quali sono i migliori per sondare la verita nella settimana?", "probes"
        )
        assert all(p.source != "plan.md" and "Settimana" not in p.text for p in found.passages)
        assert not any("migliori per sondare" in p.text for p in found.passages)

    run(db, body)


def test_imported_text_is_data_stored_never_acted_on(db: psycopg.Connection) -> None:
    """The injected sentence is stored as the paper's text; it reaches the answer model only inside a
    search-result block (`test_learn`), and the tool hands the answer back as a data block."""

    async def body(w: World) -> None:
        await w.add("2601.00001")
        async with w.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT count(*) FROM learn_segments WHERE text = %s", (INJECTED,))
            ).fetchone()
        assert row == (1,)

    run(db, body)


# --- 3. retrieval -------------------------------------------------------------------------------------------


def test_an_italian_question_finds_the_appendix_table_by_its_number(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        await w.add("2601.00001")
        found = await w.search.search("cosa mostra la Tabella 3 sull'accuratezza?", "probes")
        assert w.models.translated  # translated to the corpus's English for the lexical leg
        assert found.passages[0].kind == "table" and found.passages[0].blocks[0].label == "Table 3"
        assert found.passages[0].blocks[0].section == "B"  # the appendix is indexed
        assert found.lexical_only

    run(db, body)


def test_exact_numbers_are_boosted(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        await w.add("2601.00001")
        found = await w.search.search("why layers 5 and 9", "probes")
        texts = [p.text for p in found.passages[:3]]
        assert any("We probe layers 5 and 9" in t for t in texts)

    run(db, body)


def test_the_exact_number_outranks_the_repeated_words(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        await w.add("2601.00003")
        found = await w.search.search("why layer 9", "probes")
        assert found.passages[0].text == "Gadget readout uses layer 9 only."

    run(db, body)


def test_the_vector_leg_runs_when_an_embedding_model_is_set(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        rows = await w.add("2601.00001")
        assert not rows[0]["lexical_only"]
        found = await w.search.search("gadget truth direction residual stream", "probes")
        assert not found.lexical_only and found.passages

    run(db, body, embed=fake_embed())


def test_one_hop_along_a_citation_to_an_ingested_paper(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        await w.add("2601.00001", "2601.00002")
        found = await w.search.search("We probe layers 5 and 9 because", "probes", top=1)
        hops = [p for p in found.passages if p.hop]
        assert hops and hops[0].source == "Rossi 2025" and "Middle layers" in hops[0].text

    run(db, body)


def test_only_the_course_s_sources_are_searched(db: psycopg.Connection) -> None:
    async def body(w: World) -> None:
        await w.add("2601.00002", course="other")
        assert (await w.search.search("middle layers", "probes")).passages == []
        assert (await w.search.search("middle layers", "other")).passages

    run(db, body)


# --- 4. answering -------------------------------------------------------------------------------------------


def _rationale_answer(question: str, passages: Any) -> list[tuple[str, list[rules.Cite]]]:
    p, b = find(passages, "We probe layers 5 and 9")
    t, tb = find(passages, "Table 3")
    return [
        (
            "Gli autori sondano i layer 5 e 9 perché gli strati centrali danno il segnale più pulito.",
            [rules.Cite(p, b, b)],
        ),
        (" ", []),
        ("Nella Tabella 3 il layer 7 fa 0.62 e il layer 8 fa 0.64.", [rules.Cite(t, tb, tb)]),
        (" ", []),
        ("Il layer 8 è stato escluso per un bug del codice.", [rules.Cite(t, tb, tb)]),
        (" Il paper non dice perché non ha provato altri modelli.", []),
        (" Inoltre i layer pari sono sempre peggiori.", []),
    ]


def test_a_rationale_question_is_answered_in_tiers_and_logged(db: psycopg.Connection) -> None:
    models = FakeModels(_rationale_answer, verdicts={"bug del codice": "unsupported"})

    async def body(w: World) -> None:
        await w.add("2601.00001")
        out = await w.ask.ask("perché i layer 5 e 9 e non 7 e 8?", "probes")
        text = out["answer"]
        assert out["rationale"] and not out["abstained"] and out["lexical_only"]
        assert text.startswith("Il motivo dichiarato: le fonti dicono: Gli autori sondano i layer 5 e 9")
        assert "Le prove empiriche: le fonti dicono: Nella Tabella 3" in text
        assert "Tabella 3" in text and "§B" in text and "Verdi e Neri 2026" in text
        assert "bug" not in text  # the judge's unsupported sentence is dropped
        assert "pari" not in text  # an uncited claim is never spoken
        assert text.endswith(
            "Il paper non dice perché non ha provato altri modelli. Vuoi che cerchi stanotte?"
        )
        # every cited sentence went to the judge with exactly the blocks it cites
        assert len(models.judged) == 3 and all(len(c) == 1 for _, c in models.judged)
        tiers = {s["text"][:12]: s["tier"] for s in out["sentences"]}
        assert tiers == {"Gli autori s": 1, "Nella Tabell": 1, "Il paper non": 3}
        cite = out["sentences"][1]["citations"][0]
        assert cite["label"] == "Table 3" and cite["kind"] == "table" and cite["section"] == "B"
        async with w.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT rationale, tiers, latency_ms >= 0, jsonb_array_length(claims) "
                    "FROM learn_answer_log"
                )
            ).fetchone()
        assert row == (True, [2, 0, 1, 2], True, 5)

    run(db, body, models=models)


def test_a_partly_supported_sentence_is_said_as_an_inference(db: psycopg.Connection) -> None:
    def answer(q: str, passages: Any) -> list[tuple[str, list[rules.Cite]]]:
        t, tb = find(passages, "Table 3")
        return [("Il layer 7 rende meno del 5.", [rules.Cite(t, tb, tb)])]

    models = FakeModels(answer, verdicts={"rende meno": "partial"})

    async def body(w: World) -> None:
        await w.add("2601.00001")
        out = await w.ask.ask("quanto rende il layer 7 nella Tabella 3?", "probes")
        assert out["answer"].startswith("Non lo dicono direttamente; da Tabella 3 si può dedurre: Il layer 7")
        assert out["answer"].endswith("(mia deduzione).") and out["sentences"][0]["tier"] == 2

    run(db, body, models=models)


def test_nothing_found_is_tier_3_with_no_model_call(db: psycopg.Connection) -> None:
    models = FakeModels(None)

    async def body(w: World) -> None:
        out = await w.ask.ask("perché il cielo è blu?", "probes")  # no source at all
        assert out["answer"] == rules.NO_REASON["it"] or out["answer"] == rules.TIER3["it"]
        assert out["abstained"] and models.answered == []

    run(db, body, models=models)


def test_an_absence_sentence_carrying_a_claim_is_dropped(db: psycopg.Connection) -> None:
    def answer(q: str, passages: Any) -> list[tuple[str, list[rules.Cite]]]:
        return [("Il paper non dice perché, ma il layer 7 dava il 99% di accuratezza.", [])]

    async def body(w: World) -> None:
        await w.add("2601.00001")
        out = await w.ask.ask("perché i layer 5 e 9 e non 7 e 8?", "probes")
        assert "99%" not in out["answer"] and out["answer"] == rules.NO_REASON["it"]

    run(db, body, models=FakeModels(answer))


def test_everything_unsupported_means_abstaining(db: psycopg.Connection) -> None:
    def answer(q: str, passages: Any) -> list[tuple[str, list[rules.Cite]]]:
        return [("I gadget volano.", [rules.Cite(0, 0, 0)])]

    models = FakeModels(answer, verdicts={"volano": "contradicted"})

    async def body(w: World) -> None:
        await w.add("2601.00001")
        out = await w.ask.ask("cosa dice il paper sui gadget?", "probes")
        assert out["answer"] == rules.TIER3["it"] and out["abstained"] and out["sentences"] == []

    run(db, body, models=models)


# --- the models through the gateway -------------------------------------------------------------------------


def test_the_cheapest_slot_ranks_and_judges(db: psycopg.Connection) -> None:
    seed_catalog(
        db,
        [
            {"ref": "openai:gpt-6-luna", "input": 0.5, "output": 2, "capabilities": ["tools"]},
            {"ref": "google:gemini-3.5-flash-lite", "input": 0.05, "output": 0.2, "capabilities": ["tools"]},
            {"ref": "openai:embed-x", "input": 0.01, "output": 0, "capabilities": ["embeddings"]},
        ],
    )
    db.execute("UPDATE model_slots SET model_ref = 'openai:gpt-6-luna'")
    db.execute(
        "UPDATE model_slots SET model_ref = 'google:gemini-3.5-flash-lite' WHERE id = 'understand.confirm'"
    )
    db.execute("UPDATE model_slots SET model_ref = 'openai:embed-x' WHERE id = 'memory.embed'")

    async def body(w: World) -> None:
        models = GatewayLearnModels(None, w.pool)  # type: ignore[arg-type]
        assert await models.cheapest_slot() == "understand.confirm"

    run(db, body)


# --- erase, REST, the course turn ---------------------------------------------------------------------------


def test_the_learn_family_erases_sources_chunks_and_answers(db: psycopg.Connection) -> None:
    models = FakeModels(lambda q, p: [("Il paper sonda i layer 5 e 9.", [rules.Cite(0, 0, 0)])])

    async def body(w: World) -> None:
        await w.add("2601.00001")
        await w.ask.ask("cosa sonda il paper?", "probes")
        async with w.pool.connection() as conn:
            tables, _ = await erase.count(conn, "learn", date(2026, 11, 9), "Europe/Rome", NOW)
        assert tables == {"learn_sources": 1, "learn_answer_log": 1}

    run(db, body, models=models)
    db.execute("DELETE FROM learn_sources")
    assert db.execute("SELECT count(*) FROM learn_chunks").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM learn_segments").fetchone() == (0,)


def _arxiv_handler(request: Any) -> Any:
    import httpx

    path = request.url.path
    if request.url.host == "arxiv.org" and path == "/html/2601.00001":
        return httpx.Response(200, text=PAPER_A)
    if request.url.host == "arxiv.org" and path == "/abs/2601.00001":
        return httpx.Response(
            200,
            text='<meta name="citation_title" content="Probing Gadget Models '
            'for Truth">'
            '<meta name="citation_author" content="Verdi, Ada"><meta name="citation_date" '
            'content="2026/01/02"><div class="abs-license"><a href="http://creativecommons.org/'
            'licenses/by/4.0/">CC</a></div>',
        )
    return httpx.Response(404)


def test_the_rest_api_adds_lists_asks_and_deletes(
    client: Any, db: psycopg.Connection, http_handler: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pisa.connectors import learn_fetch

    monkeypatch.setattr(learn_fetch.ARXIV_GATE, "every", 0.0)

    async def public(host: str) -> list[str]:
        return ["93.184.215.14"]

    monkeypatch.setattr(learn_fetch, "system_resolver", public)
    http_handler["handler"] = _arxiv_handler
    _, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    r = client.post("/v1/learn/sources", json={"items": ["2601.00001"], "course": "probes"}, headers=auth)
    assert r.status_code == 202 and r.json()[0]["state"] in ("queued", "fetching", "indexing", "ready")
    runtime = client.app.state.runtime
    client.portal.call(runtime.learn.settled)
    listed = client.get("/v1/learn/sources", params={"course": "probes"}, headers=auth).json()["sources"]
    assert listed[0]["state"] == "ready" and listed[0]["licence"] == "CC BY 4.0"
    assert listed[0]["title"] == "Probing Gadget Models for Truth" and listed[0]["authors"] == ["Verdi, Ada"]
    # no learn.answer model chosen: the answer names Settings
    asked = client.post(
        "/v1/learn/ask", json={"question": "cosa mostra la Tabella 3?", "course": "probes"}, headers=auth
    )
    assert asked.status_code == 409 and "Settings → AI models" in asked.json()["error"]["action"]
    plan = base64.b64encode(b"# Plan\nread it").decode()
    up = client.post(
        "/v1/learn/sources",
        json={"file": {"name": "plan.md", "media_type": "text/markdown", "data": plan}},
        headers=auth,
    )
    assert up.status_code == 202 and up.json()[0]["citable"] is False
    gone = client.delete(f"/v1/learn/sources/{listed[0]['id']}", headers=auth)
    bad = client.post(
        "/v1/learn/sources",
        json={"file": {"name": "a.pdf", "media_type": "application/pdf", "data": "not base64!"}},
        headers=auth,
    )
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "validation_failed"
    assert gone.json() == {"deleted": 1}
    assert client.delete(f"/v1/learn/sources/{listed[0]['id']}", headers=auth).status_code == 404
    assert client.get("/v1/learn/sources", headers=auth).status_code == 200
    assert client.get("/v1/learn/sources").status_code == 401


class _Learn:
    async def ask(self, question: str, course: str | None = None) -> dict[str, Any]:
        return {"answer": "Le fonti dicono: ok.", "lexical_only": True}

    async def sources(self, course: str | None = None) -> list[dict[str, Any]]:
        return []


def _tools(words: str, learn: Any) -> set[str]:
    agent = build_agent(
        None,  # type: ignore[arg-type]
        TurnContext(
            owner=owner_at(),
            clock=lambda: NOW,
            defer=False,
            learn=learn,  # type: ignore[arg-type]
            profile_turn=ProfileTurn(words=words, lang="it-IT"),
        ),
    )
    return {n for ts in agent.toolsets for n in getattr(ts, "tools", {})}


def test_the_learn_tools_join_only_a_course_turn() -> None:
    plain = _tools("che tempo fa domani?", None)
    assert _tools("che tempo fa domani?", _Learn()) == plain  # every other turn: the same tools
    assert rules.LEARN_TOOLS <= _tools("nel paper, perché i layer 5 e 9?", _Learn())
    assert not rules.LEARN_TOOLS & _tools("nel paper, perché i layer 5 e 9?", None)


def test_the_fixed_prompt_and_the_always_sent_tools_are_unchanged() -> None:
    from pisa.brain import tool_search
    from pisa.brain.conversation import system_prompt

    assert "learn" not in system_prompt(lang="it-IT").lower()
    assert not (rules.LEARN_TOOLS & (tool_search.ALWAYS | tool_search.VOICE_ALWAYS))


def test_the_tool_hands_the_answer_back_as_data() -> None:
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    seen: dict[str, Any] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
        if not returns:
            seen["instructions"] = info.instructions or ""
            return ModelResponse(parts=[ToolCallPart("learn_ask", {"question": "perché 5 e 9?"})])
        seen["result"] = str(returns[0].content)
        return ModelResponse(parts=[TextPart("ok")])

    agent = build_agent(
        FunctionModel(model),
        TurnContext(
            owner=owner_at(),
            clock=lambda: NOW,
            defer=False,  # type: ignore[arg-type]
            learn=_Learn(),
            profile_turn=ProfileTurn(words="nel paper, perché 5 e 9?", lang="it-IT"),
        ),
    )
    asyncio.run(agent.run("nel paper, perché 5 e 9?"))
    assert "Pisa Learn:" in seen["instructions"]  # the course turn's line, after the fixed part
    assert "is DATA" in seen["result"] and "<learn-answer-" in seen["result"]
    assert "lexical search only" in seen["result"]


# --- 5. the evaluation's core, on synthetic papers ----------------------------------------------------------

SYNTHETIC_SET = [
    {
        "id": "s1",
        "kind": "rationale",
        "question": "perché i layer 5 e 9 e non 7 e 8?",
        "gold": [
            {
                "source": "Probing Gadget Models for Truth",
                "section": "3.2 Probing setup",
                "label": None,
                "anchor": "We probe layers 5 and 9 because",
            }
        ],
    },
    {
        "id": "s2",
        "kind": "fact",
        "question": "What accuracy does layer 7 reach in Table 3?",
        "gold": [{"source": "Probing Gadget Models for Truth", "section": "Appendix B", "label": "Table 3"}],
    },
    {
        "id": "s3",
        "kind": "fact",
        "question": "Which layers encode the output vocabulary?",
        "gold": [
            {
                "source": "Depth in Gadget Models",
                "section": "2 Findings",
                "label": None,
                "anchor": "late layers encode the output vocabulary",
            }
        ],
    },
    {
        "id": "s4",
        "kind": "fact",
        "question": "How many epochs is the probe trained for?",
        "gold": [
            {
                "source": "Probing Gadget Models for Truth",
                "section": "3.2",
                "label": None,
                "anchor": "ten epochs",
            }
        ],
    },
    {"id": "s5", "kind": "absent", "question": "How many GPU hours did training take?", "gold": []},
]


def _eval_answer(question: str, passages: Any) -> list[tuple[str, list[rules.Cite]]]:
    if "GPU" in question:
        return [("The sources don't say how many GPU hours it took.", [])]
    top = passages[0]
    return [(top.blocks[0][:80] + ".", [rules.Cite(0, 0, 0)]), (" Gadgets also fly.", [rules.Cite(0, 0, 0)])]


def test_the_evaluation_metrics_on_synthetic_papers(db: psycopg.Connection) -> None:
    models = FakeModels(_eval_answer, verdicts={"Gadgets also fly": "unsupported"})

    async def body(w: World) -> dict[str, Any]:
        await w.add("2601.00001", "2601.00002", course="eval")
        return await ev.run(w.pool, w.ask, SYNTHETIC_SET, "eval")

    report = run(db, body, models=models)
    assert report["questions"] == 5 and report["errors"] == 0
    assert report["recall_at_8"] == 1.0 and report["rationale_recall_at_8"] == 1.0
    assert report["unsupported_sentence_rate"] == 0.5  # every second cited sentence was made up
    assert report["absent_abstained"] == 1.0 and report["abstention_rate"] == 0.2
    assert report["latency_ms_p50"] is not None


def test_the_committed_set_has_50_questions_with_gold_locations_only() -> None:
    data = ev.load_set()
    qs = data["questions"]
    assert len(qs) == 50 and len({q["id"] for q in qs}) == 50
    assert sum(q["kind"] == "rationale" for q in qs) >= 10
    assert 5 <= len(data["papers"]) <= 8
    papers = {p["arxiv"] for p in data["papers"]}
    for q in qs:
        assert (q["kind"] == "absent") == (q["gold"] == [])
        for g in q["gold"]:
            assert g["arxiv"] in papers and set(g) <= {"arxiv", "section", "label", "page", "anchor"}
            assert len((g.get("anchor") or "").split()) <= 16  # a location, never a passage


def test_a_sentence_no_model_could_check_is_unsupported(db: psycopg.Connection) -> None:
    from pisa.domain.errors import PisaError

    class Down:
        async def run(self, slot: str, call: Any, **kw: Any) -> Any:
            raise PisaError("provider_error", "down", "try later")

    async def body(w: World) -> None:
        models = GatewayLearnModels(Down(), w.pool)  # type: ignore[arg-type]
        assert await models.judge("Layer 5 scores 0.91.", ["Layer 5: 0.91"]) == "unsupported"
        assert await models.rerank("q", ["a", "b"], 8) is None  # the fused order stands
        assert await models.translate("perché?", "en") is None  # their own words for the lexical leg

    run(db, body)


def test_a_cap_refusal_is_never_read_as_the_sources_silence(db: psycopg.Connection) -> None:
    """The merge (review M2): Pisa Learn's cap refusing the judge or the reranker says so; it is
    never turned into "unsupported" (which would make Pisa say "my sources don't say")."""
    from pisa.domain.errors import PisaError

    class Capped:
        async def run(self, slot: str, call: Any, **kw: Any) -> Any:
            raise PisaError("budget_exceeded", "Pisa Learn's monthly cap is reached.", "wait for next month")

    async def body(w: World) -> None:
        models = GatewayLearnModels(Capped(), w.pool)  # type: ignore[arg-type]
        for job in (
            models.judge("Layer 5 scores 0.91.", ["Layer 5: 0.91"]),
            models.rerank("q", ["a", "b"], 8),
        ):
            with pytest.raises(PisaError) as err:
                await job
            assert err.value.code == "budget_exceeded"

    run(db, body)


def test_pisa_learn_has_its_own_monthly_cap_inside_his_limits(db: psycopg.Connection) -> None:
    from decimal import Decimal

    from pisa.domain.errors import PisaError
    from pisa.infra import ledger

    # a tiny exchange rate keeps the sums far under their own caps and the hard limit: $20 = €0.20
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('usd_to_eur', '{\"rate\": \"0.01\"}') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    spent = Decimal("0.195")
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, trace_id, "
        "created_at) VALUES (%s, 'learn.answer', 'x:y', 'settled', %s, %s, true, 't', %s)",
        (uuid.uuid4(), spent, spent, NOW),
    )

    async def body(w: World) -> None:
        async with w.pool.connection() as conn:
            with pytest.raises(PisaError) as err:
                await ledger.reserve(
                    conn,
                    slot=rules.CHECK_SLOT,
                    model_ref="x:y",
                    estimate_eur=Decimal("0.01"),
                    price_known=True,
                    trace_id="t",
                    now=NOW,
                    tz="Europe/Rome",
                    hard=hard_limit.DEFAULT,
                )
            assert err.value.code == "budget_exceeded" and "Pisa Learn's monthly cap" in err.value.message
            # the rest of Pisa is untouched
            await ledger.reserve(
                conn,
                slot="chat.conversation",
                model_ref="x:y",
                estimate_eur=Decimal("0.01"),
                price_known=True,
                trace_id="t",
                now=NOW,
                tz="Europe/Rome",
                hard=hard_limit.DEFAULT,
            )
            # under the cap, Pisa Learn answers
            await ledger.reserve(
                conn,
                slot=rules.CHECK_SLOT,
                model_ref="x:y",
                estimate_eur=Decimal("0.001"),
                price_known=True,
                trace_id="t",
                now=NOW,
                tz="Europe/Rome",
                hard=hard_limit.DEFAULT,
            )

    run(db, body)


def test_the_checks_are_billed_as_pisa_learn(db: psycopg.Connection) -> None:
    billed: list[str | None] = []

    class Gateway:
        async def run(self, slot: str, call: Any, **kw: Any) -> Any:
            billed.append(kw.get("ledger_slot"))
            return "supported"

    async def body(w: World) -> None:
        models = GatewayLearnModels(Gateway(), w.pool)  # type: ignore[arg-type]
        assert await models.judge("s", ["c"]) == "supported"

    run(db, body)
    assert billed == [rules.CHECK_SLOT]


def test_answers_are_in_his_settings_language(db: psycopg.Connection) -> None:
    models = FakeModels(lambda q, p: [("The probe reads layers 5 and 9.", [rules.Cite(0, 0, 0)])])
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('settings', '{\"language\": \"en-US\"}') "
        "ON CONFLICT (key) DO UPDATE SET value = jsonb_set(app_settings.value, '{language}', '\"en-US\"')"
    )

    async def body(w: World) -> None:
        await w.add("2601.00001")
        out = await w.ask.ask("cosa sonda il paper sui layer 5 e 9?", "probes")  # asked in Italian
        assert out["lang"] == "en" and out["answer"].startswith("The sources say: ")

    run(db, body, models=models)
