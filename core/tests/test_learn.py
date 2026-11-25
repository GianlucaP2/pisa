"""Without a database: the item parser, the parsers (arXiv HTML, LaTeX, PDF pages, Markdown), the
chunker that never crosses a table or an equation, the exact numbers, RRF, the cited sentences and their
tiers,
the spoken answer, the Citations API request, the course-turn gate and the arXiv rate limit. Every word is
synthetic."""

import asyncio
import gzip
import io
import itertools
import tarfile
import time
from types import SimpleNamespace
from typing import Any

import pytest

from pisa.brain import learn as brain
from pisa.connectors.learn_fetch import ARXIV_GATE, LearnFetcher, _ArxivGate, latex_from_eprint
from pisa.domain import learn as rules
from pisa.domain import learn_parse as parse
from pisa.domain import tool_choice
from tests.learn_fakes import INJECTED, PAPER_A

# --- 2. import: what an item is -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "key"),
    [
        ("2310.01405", "arxiv", "arxiv:2310.01405"),
        ("arXiv:2310.01405v3", "arxiv", "arxiv:2310.01405"),
        ("https://arxiv.org/pdf/2310.01405v2", "arxiv", "arxiv:2310.01405"),
        ("https://doi.org/10.48550/arXiv.2310.01405", "arxiv", "arxiv:2310.01405"),
        ("10.1145/3442188.3445922", "doi", "doi:10.1145/3442188.3445922"),
        ("https://doi.org/10.1145/ABC.1", "doi", "doi:10.1145/abc.1"),
        ("https://example.org/paper.html", "url", "url:https://example.org/paper.html"),
        ("Probing Gadget Models: for Truth!", "title", "title:probing gadget models for truth"),
    ],
)
def test_an_item_is_an_arxiv_id_a_doi_a_url_or_a_title(text: str, kind: str, key: str) -> None:
    item = rules.parse_item(text)
    assert (item.kind, item.key) == (kind, key)


def test_a_title_is_taken_only_on_one_exact_match() -> None:
    assert rules.exact_title_matches("Probing gadget models", ["Probing Gadget Models.", "Other"]) == [0]
    # two exact, or only a near one: never picked silently
    assert len(rules.exact_title_matches("Probing", ["Probing", "probing"])) == 2
    assert rules.exact_title_matches("Probing gadget models", ["Probing gadget models 2"]) == []


# --- 1. sources: the parsers and the chunks -----------------------------------------------------------------


def test_arxiv_html_keeps_sections_appendix_tables_equations_and_citations() -> None:
    p = parse.arxiv_html(PAPER_A)
    assert p.title == "Probing Gadget Models for Truth"
    kinds = {s.kind for s in p.segments}
    assert {"paragraph", "table", "equation", "caption", "appendix"} <= kinds
    table = next(s for s in p.segments if s.kind == "table")
    assert (
        table.label == "Table 3" and "0.91" in table.text and table.section_path[0].startswith("Appendix B")
    )
    eq = next(s for s in p.segments if s.kind == "equation")
    assert eq.label == "Eq. (1)" and "sigma" in eq.text
    stated = next(s for s in p.segments if "We probe layers 5 and 9" in s.text)
    assert stated.section_path == ("3 Method", "3.2 Probing setup")
    assert stated.cites == ("arxiv:2601.00002",)  # resolved through the bibliography
    assert next(s for s in p.segments if s.text.startswith("Layers 7 and 8")).kind == "appendix"
    assert not any("Rossi. Depth" in s.text for s in p.segments)  # the bibliography is not a source of claims


def test_a_chunk_never_crosses_a_table_or_an_equation() -> None:
    segs = [
        rules.Seg("paragraph", "a " * 10, ("1 A",)),
        rules.Seg("paragraph", "b " * 10, ("1 A",)),
        rules.Seg("table", "Table 1: x", ("1 A",), "Table 1"),
        rules.Seg("paragraph", "c " * 10, ("1 A",)),
        rules.Seg("equation", "x=1", ("1 A",)),
        rules.Seg("paragraph", "d " * 10, ("1 A",)),
        rules.Seg("paragraph", "e " * 10, ("2 B",)),
    ]
    plans = rules.chunk(segs)
    assert [(c.seg_from, c.seg_to, c.kind) for c in plans] == [
        (0, 1, "paragraph"),
        (2, 2, "table"),
        (3, 3, "paragraph"),
        (4, 4, "equation"),
        (5, 5, "paragraph"),
        (6, 6, "paragraph"),
    ]
    for c in plans:  # no chunk spans a table or equation with anything else
        inside = segs[c.seg_from : c.seg_to + 1]
        assert len(inside) == 1 or not any(s.kind in rules.NEVER_CROSSED for s in inside)


def test_a_chunk_stops_at_its_size() -> None:
    segs = [rules.Seg("paragraph", "w" * 3000, ("1",)) for _ in range(3)]
    assert len(rules.chunk(segs)) == 3


def test_figure_descriptions_are_marked_and_alone() -> None:
    segs = [
        rules.Seg("paragraph", "a"),
        rules.Seg("caption", "The plot appears to rise", is_description=True),
    ]
    plans = rules.chunk(segs)
    assert plans[1].is_description and not plans[0].is_description


def test_prefixes_in_italian_and_english() -> None:
    it = rules.prefix("Gadget", ("Appendix B Per-layer",), "Table 3", "it")
    en = rules.prefix("Gadget", ("Appendix B Per-layer",), "Table 3", "en")
    assert "Tabella 3" in it and "sezione" in it and "Table 3" in en and "section" in en


def test_latex_source_is_parsed_with_appendix_tables_and_equations() -> None:
    tex = r"""\documentclass{article}\title{Gadget \emph{Probes}}\begin{document}
\section{Method}
We probe layers 5 and 9 \cite{rossi}.

% a comment that is dropped
\begin{equation}p=\sigma(w h)\label{eq:p}\end{equation}
\appendix
\section{Per-layer results}
\begin{table}\caption{Probe accuracy per layer.}\begin{tabular}{ll}5 & 0.91 \\ 7 &
0.62\end{tabular}\end{table}
Layers 7 and 8 score lower.
\end{document}"""
    bbl = r"\begin{thebibliography}{1}\bibitem{rossi} C. Rossi. Depth. arXiv:2601.00002.\end{thebibliography}"
    p = parse.latex(tex, bbl)
    assert p.title == "Gadget Probes"
    para = next(s for s in p.segments if "probe layers" in s.text)
    assert para.section_path == ("1 Method",) and para.cites == ("arxiv:2601.00002",)
    eq = next(s for s in p.segments if s.kind == "equation")
    assert eq.label == "Eq. (1)" and "label" not in eq.text
    table = next(s for s in p.segments if s.kind == "table")
    assert (
        table.label == "Table 1"
        and "0.91" in table.text
        and table.section_path == ("Appendix A Per-layer results",)
    )
    assert next(s for s in p.segments if s.text.startswith("Layers 7")).kind == "appendix"
    assert not any("comment" in s.text for s in p.segments)


def test_the_eprint_tarball_gives_the_main_file_with_its_inputs() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in {
            "main.tex": r"\documentclass{article}\begin{document}\input{sec}\end{document}",
            "sec.tex": r"\section{Intro} Hello gadget.",
            "main.bbl": r"\bibitem{a} arXiv:2601.00002",
        }.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    got = latex_from_eprint(buf.getvalue())
    assert got is not None and "Hello gadget" in got[0] and "2601.00002" in got[1]
    assert latex_from_eprint(gzip.compress(b"%PDF-1.4 only")) is None


def test_pdf_pages_give_pages_headings_captions_and_appendix() -> None:
    pages = [
        "1 Introduction\nGadget models hold truth.\n\n3.2 Probing setup\nWe probe layers 5 and 9.",
        "Table 3: Probe accuracy per layer.\nLayer 5 0.91\n\nReferences\n[1] Rossi 2025.\n\nB Per-layer "
        "results\n"
        "Layers 7 and 8 score lower.",
    ]
    p = parse.pdf_pages(pages)
    probe = next(s for s in p.segments if "probe layers" in s.text)
    assert probe.page_from == 1 and probe.section_path == ("1 Introduction", "3.2 Probing setup")
    table = next(s for s in p.segments if s.kind == "table")
    assert table.label == "Table 3" and table.page_from == 2
    app = next(s for s in p.segments if s.text.startswith("Layers 7"))
    assert app.kind == "appendix" and app.section_path[0] == "Appendix B Per-layer results"
    assert not any("Rossi" in s.text for s in p.segments)


def test_html_text_gets_its_page_from_the_pdf() -> None:
    p = parse.with_pages(
        parse.arxiv_html(PAPER_A),
        [
            "Abstract We train linear probes on the hidden states",
            "We probe layers 5 and 9 because the middle layers hold",
        ],
    )
    probe = next(s for s in p.segments if "We probe layers" in s.text)
    assert probe.page_from == 2 and p.has_pages


def test_markdown_headings_tables_and_equations() -> None:
    p = parse.markdown(
        "# Plan\n\n## Week 1\nRead the probes paper.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n$$x=1$$\n"
    )
    assert p.title == "Plan"
    assert [s.kind for s in p.segments] == ["paragraph", "table", "equation"]
    assert p.segments[0].section_path == ("Plan", "Week 1")


# --- 3. retrieval: numbers and fusion -----------------------------------------------------------------------


def test_exact_numbers_are_found_in_italian_and_english() -> None:
    assert rules.exact_terms("perché i layer 5 e 9 e non 7 e 8?") == ["layer 5", "layer 9"]
    assert rules.exact_terms("cosa mostra la Tabella 3?") == ["table 3"]
    assert rules.exact_terms("why L9 and Figure 7, Appendix B") == ["figure 7", "appendix b", "layer 9"]


def test_rrf_fuses_two_rankings() -> None:
    assert rules.rrf([[1, 2, 3], [3, 1]]) == [1, 3, 2]
    assert rules.rrf([[], [5]]) == [5]


def test_rationale_and_language() -> None:
    assert rules.is_rationale("perché i layer 5 e 9 e non 7 e 8?")
    assert rules.is_rationale("Why did they choose a logistic loss?")
    assert not rules.is_rationale("What accuracy does layer 5 reach?")
    assert rules.question_lang("perché i layer 5 e 9 e non 7 e 8?") == "it"
    assert rules.question_lang("What does Table 3 show?") == "en"


# --- 4. the cited answer, the judge, the tiers --------------------------------------------------------------


def test_sentences_carry_their_citations() -> None:
    c1, c2 = rules.Cite(0, 1, 1), rules.Cite(2, 0, 0)
    out = rules.sentences(
        [
            ("Ecco. ", []),
            ("Il paper sonda i layer 5 e 9.", [c1]),
            (" ", []),
            ("Lo strato 7 fa 0.62.", [c2]),
            (" Il paper non dice perché esclude 8. Bene", []),
        ]
    )
    assert [(s.text, s.cites) for s in out] == [
        ("Ecco.", []),
        ("Il paper sonda i layer 5 e 9.", [c1]),
        ("Lo strato 7 fa 0.62.", [c2]),
        ("Il paper non dice perché esclude 8.", []),
        ("Bene", []),
    ]


def test_the_tier_follows_the_judge_never_tier_1_unchecked() -> None:
    cited = rules.Sentence("Layer 5 scores 0.91.", [rules.Cite(0, 0, 0)])
    assert rules.tier(cited, "supported", only_descriptions=False) == 1
    assert rules.tier(cited, "partial", only_descriptions=False) == 2
    assert rules.tier(cited, "unsupported", only_descriptions=False) == 0
    assert rules.tier(cited, "contradicted", only_descriptions=False) == 0
    assert rules.tier(cited, None, only_descriptions=False) == 0
    # a figure description is never the author's claim
    assert rules.tier(cited, "supported", only_descriptions=True) == 2
    # an inference is said as one
    inferred = rules.Sentence("Da qui si può dedurre che 7 è peggio (mia deduzione).", [rules.Cite(0, 0, 0)])
    assert rules.tier(inferred, "supported", only_descriptions=False) == 2
    # uncited: only the sources' silence is said, plainly
    assert (
        rules.tier(rules.Sentence("Il paper non dice perché ha escluso 7."), None, only_descriptions=False)
        == 3
    )
    assert rules.tier(rules.Sentence("Il layer 7 è rumoroso."), None, only_descriptions=False) == 0


def _where(kind: str = "paragraph", label: str | None = None) -> rules.Where:
    return rules.Where("Verdi e Neri 2026", "3.2", 4, label, kind)  # type: ignore[arg-type]


def test_an_unchecked_claim_never_rides_on_an_absence_sentence() -> None:
    q = "perché i layer 5 e 9 e non 7 e 8?"
    assert rules.plain_absence("Il paper non dice perché ha escluso i layer 7 e 8.", q)
    assert not rules.plain_absence("Il paper non dice perché, ma il layer 7 dava il 99%.", q)
    assert not rules.plain_absence("The paper does not say why; layer 7 was simply the best.", q)
    assert not rules.plain_absence("The paper does not say why it reached 0.91 on layer 7.", q)


def test_the_three_tiers_are_spoken_as_such() -> None:
    said = [
        rules.Sentence("Il paper sonda i layer 5 e 9.", [rules.Cite(0, 0, 0)], tier=1),
        rules.Sentence("Il layer 7 rende meno (mia deduzione).", [rules.Cite(1, 0, 0)], tier=2),
        rules.Sentence("Il paper non dice perché ha escluso l'8.", [], tier=3),
        rules.Sentence("Inventato.", [rules.Cite(0, 0, 0)], tier=0),
    ]
    text = rules.compose(said, {0: [_where()], 1: [_where("table", "Table 3")]}, "it", rationale=False)
    assert text.startswith("Le fonti dicono: Il paper sonda i layer 5 e 9 (Verdi e Neri 2026, §3.2, p. 4).")
    assert "Non lo dicono direttamente; da Tabella 3 si può dedurre: Il layer 7 rende meno" in text
    assert "(mia deduzione)." in text and "Inventato" not in text
    assert text.endswith("Il paper non dice perché ha escluso l'8. Vuoi che cerchi stanotte?")
    assert rules.compose([], {}, "it", rationale=False) == rules.TIER3["it"]
    assert (
        rules.compose([], {}, "en", rationale=False)
        == "None of my sources explains this: shall I look tonight?"
    )


def test_a_rationale_answer_separates_reason_evidence_and_absence() -> None:
    said = [
        rules.Sentence("Layer 9 reaches 0.88.", [rules.Cite(1, 0, 0)], tier=1),
        rules.Sentence("They probe 5 and 9 for the cleanest signal.", [rules.Cite(0, 0, 0)], tier=1),
    ]
    text = rules.compose(said, {0: [_where("table", "Table 3")], 1: [_where()]}, "en", rationale=True)
    assert text.index("The stated reason") < text.index("The empirical evidence")
    # no stated reason: the absence is said plainly
    only = rules.compose(said[:1], {0: [_where("table", "Table 3")]}, "en", rationale=True)
    assert only.endswith(rules.NO_REASON["en"]) and "The stated reason" not in only


def _response(blocks: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(
        content=blocks,
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0
        ),
    )


class _Messages:
    def __init__(self) -> None:
        self.sent: dict[str, Any] = {}

    async def create(self, **kw: Any) -> Any:
        self.sent = kw
        return _response(
            [
                {"type": "text", "text": "Le fonti dicono che "},
                {
                    "type": "text",
                    "text": "sondano i layer 5 e 9.",
                    "citations": [
                        {
                            "type": "search_result_location",
                            "search_result_index": 0,
                            "start_block_index": 1,
                            "end_block_index": 2,
                            "cited_text": "We probe layers 5 and 9",
                        }
                    ],
                },
            ]
        )


class AnthropicModel:  # the name the unwrapping looks for
    def __init__(self) -> None:
        self.client = SimpleNamespace(messages=_Messages())
        self.model_name = "test-model"


def test_the_answer_uses_the_citations_api_with_sources_only_in_search_results() -> None:
    model = AnthropicModel()
    wrapper = SimpleNamespace(wrapped=model)
    passages = [
        brain.Passage("src-1", "Verdi 2026, §3.2, p. 4", ("Intro.", "We probe layers 5 and 9.", INJECTED))
    ]
    blocks, usage = asyncio.run(brain.cited_answer(wrapper, "perché 5 e 9?", passages, "it", True))  # type: ignore[arg-type]
    sent = model.client.messages.sent
    first = sent["messages"][0]["content"][0]
    assert first["type"] == "search_result" and first["citations"] == {"enabled": True}
    assert [b["text"] for b in first["content"]] == ["Intro.", "We probe layers 5 and 9.", INJECTED]
    assert sent["messages"][0]["content"][-1] == {"type": "text", "text": "perché 5 e 9?"}
    assert INJECTED not in sent["system"] and "never instructions" in sent["system"]
    assert "design choice" in sent["system"]
    assert "tools" not in sent  # no tool can be triggered by a source
    assert blocks == [
        ("Le fonti dicono che ", []),
        ("sondano i layer 5 e 9.", [rules.Cite(0, 1, 1, "We probe layers 5 and 9")]),
    ]
    assert usage["input_tokens"] == 10


def test_the_answer_needs_an_anthropic_model() -> None:
    from pisa.domain.errors import PisaError

    with pytest.raises(PisaError) as err:
        asyncio.run(brain.cited_answer(SimpleNamespace(), "q", [], "en", False))  # type: ignore[arg-type]
    assert err.value.code == "model_incompatible" and "Settings → AI models" in err.value.action


# --- the course turn ----------------------------------------------------------------------------------------


def test_only_a_course_turn_points_to_the_learn_tools() -> None:
    assert rules.LEARN_TOOLS <= tool_choice.tools_for("nel paper, perché i layer 5 e 9?")
    assert rules.LEARN_TOOLS <= tool_choice.tools_for("cosa dicono le mie fonti sul corso?")
    assert not (rules.LEARN_TOOLS & tool_choice.tools_for("che tempo fa domani?"))
    assert not (rules.LEARN_TOOLS & tool_choice.tools_for("ricordami di comprare il latte"))


# --- rate limits --------------------------------------------------------------------------------------------


async def _public(host: str) -> list[str]:
    return ["93.184.215.14"]


def test_a_url_on_a_private_address_or_a_redirect_to_one_is_refused() -> None:
    import httpx

    async def loopback(host: str) -> list[str]:
        return ["127.0.0.1"] if host in ("localhost", "intranet.example") else ["93.184.215.14"]

    def answer(r: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://intranet.example/secret"})

    fetch = LearnFetcher(transport=lambda _ip: httpx.MockTransport(answer), resolver=loopback)
    for url in ("http://localhost:8000/v1/x", "https://example.org/paper"):
        with pytest.raises(ValueError, match="not_public"):
            asyncio.run(fetch.page(url))


def test_arxiv_is_asked_once_every_3_seconds() -> None:
    assert ARXIV_GATE.every == 3.0 == rules.ARXIV_EVERY_S
    gate = _ArxivGate(0.2)
    calls: list[float] = []

    async def go() -> None:
        import httpx

        def answer(request: httpx.Request) -> httpx.Response:
            calls.append(time.monotonic())
            return httpx.Response(200, text="<html></html>")

        fetch = LearnFetcher(transport=lambda _ip: httpx.MockTransport(answer), gate=gate, resolver=_public)
        await asyncio.gather(*(fetch.arxiv_html("2601.00001") for _ in range(3)))
        await fetch.page("https://api.openalex.org/works")  # other hosts aren't gated

    asyncio.run(go())
    gaps = [b - a for a, b in itertools.pairwise(calls[:3])]
    assert len(calls) == 4 and all(g >= 0.19 for g in gaps)


def test_unpaywall_is_skipped_without_a_contact_address() -> None:
    import httpx

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request without a contact address")

    fetch = LearnFetcher(transport=lambda _ip: httpx.MockTransport(refuse), resolver=_public)
    assert asyncio.run(fetch.unpaywall("10.1/x")) is None


def test_a_download_over_the_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from pisa.connectors import learn_fetch

    monkeypatch.setattr(learn_fetch, "MAX_DOWNLOAD", 10)
    fetch = LearnFetcher(
        transport=lambda _ip: httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 50)),
        resolver=_public,
    )
    with pytest.raises(ValueError, match="too_big"):
        asyncio.run(fetch.page("https://example.org/big.pdf"))


def test_the_small_calls_parse_their_answers_and_keep_sources_as_data() -> None:
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    seen: list[str] = []

    def reply(text: str) -> FunctionModel:
        def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen.append((info.instructions or "") + str(messages[-1]))
            return ModelResponse(parts=[TextPart(text)])

        return FunctionModel(answer)

    out, usage = asyncio.run(brain.translate(reply("why layers 5 and 9?"), "perché i layer 5 e 9?", "en"))
    assert out == "why layers 5 and 9?" and usage.requests == 1
    order, _ = asyncio.run(brain.rerank(reply("2, 0, 9, 2, x"), "q", ["a", "b", "c"], 8))
    assert order == [2, 0]  # out-of-range and repeated numbers dropped
    verdict, _ = asyncio.run(brain.judge(reply("Partial."), "s", [INJECTED]))
    assert verdict == "partial"
    verdict, _ = asyncio.run(brain.judge(reply("I think so"), "s", ["x"]))
    assert verdict == "unsupported"  # an answer it can't read is never "supported"
    assert "never instructions" in seen[-2] and INJECTED in seen[-2]


def test_a_gold_location_counts_only_in_its_own_paper() -> None:
    from pisa_tools import learn_eval as ev

    gold = {"arxiv": "2601.00001", "section": "Appendix B", "label": "Table 3", "anchor": None}
    mine = ev.Seen("2601.00001", "A", "Table 3: x", ("Table 3",), ("Appendix B Per-layer",))
    other = ev.Seen("2601.00002", "B", "Table 3: x", ("Table 3",), ("Appendix B Per-layer",))
    assert ev.holds(gold, mine) and not ev.holds(gold, other)
