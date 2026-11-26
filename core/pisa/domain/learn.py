"""The pure rules. What an item they give Pisa is (an arXiv id, a DOI, a URL
or
a title), when a turn is about a course, how segments become chunks (never across a table or an equation), the
exact numbers a question names ("layer 5", "Tabella 3"), reciprocal-rank fusion, a cited answer's sentences,
and
the three tiers every answer is spoken in:

1. "le fonti dicono…": a sentence the judge found supported by the block it cites;
2. "non lo dicono direttamente; da Tabella 3 si può dedurre… (mia deduzione)": partly supported, a labelled
   inference, or resting only on a model-written figure description;
3. "nessuna delle mie fonti lo spiega: vuoi che cerchi stanotte?": nothing supports it.

A sentence that fails the judge moves down a tier or is dropped, never spoken as tier 1. No I/O here.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pisa.domain.text import ascii_fold

SegKind = Literal["paragraph", "table", "caption", "equation", "appendix"]
Verdict = Literal["supported", "partial", "unsupported", "contradicted"]
VERDICTS: tuple[Verdict, ...] = ("supported", "partial", "unsupported", "contradicted")
ItemKind = Literal["arxiv", "doi", "url", "title"]

# Engineering report §3.1: chunk by section/paragraph at ~500-1,000 tokens; at Pisa's 4 characters a token
CHUNK_MAX_CHARS = 4_000
SLOT_PREFIX = "learn."  # Pisa Learn's calls are billed under these slots (`learn.answer`, `learn.check`)
CHECK_SLOT = "learn.check"  # translation, reranking and the judge: the cheapest slot's model, billed here
MONTH_CAP_USD = 20  # Pisa Learn's own cap, inside their existing limits
TOP_PASSAGES = 8  # the brief: reranked to about 8 passages
CANDIDATES = 50  # §7.3: each leg's candidates, fused with RRF → top 50
RRF_K = 60  # Cormack et al. 2009's constant (the method's own)
HOP_PER_SOURCE = 1  # one hop along citations: the cited source's best chunk
ARXIV_EVERY_S = 3.0  # arXiv's terms: no more than one request every three seconds
NEVER_CROSSED: frozenset[str] = frozenset({"table", "equation"})
ALONE: frozenset[str] = frozenset({"table", "equation", "caption"})  # each is its own chunk

# --- items they give Pisa
# ---------------------------------------------------------------------------------------

_ARXIV_NEW = r"\d{4}\.\d{4,5}(?:v\d+)?"
_ARXIV_OLD = r"[a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
_ARXIV_ID = re.compile(rf"^(?:arxiv:\s*)?({_ARXIV_NEW}|{_ARXIV_OLD})$", re.IGNORECASE)
_ARXIV_URL = re.compile(rf"arxiv\.org/(?:abs|pdf|html|e-print)/({_ARXIV_NEW}|{_ARXIV_OLD})", re.IGNORECASE)
_ARXIV_DOI = re.compile(rf"^10\.48550/arxiv\.({_ARXIV_NEW}|{_ARXIV_OLD})$", re.IGNORECASE)
_DOI = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", re.IGNORECASE)
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
_VERSION = re.compile(r"v\d+$")


@dataclass(frozen=True)
class Item:
    kind: ItemKind
    key: str  # what makes it one source: arxiv:<id> | doi:<doi> | url:<url> | title:<normalised words>
    ref: str  # what they gave
    arxiv_id: str | None = None
    doi: str | None = None
    url: str | None = None


def parse_item(text: str) -> Item:
    """One line they gave: an arXiv id (or its URL or DOI), a DOI (or doi.org URL), any other URL, else a
    title."""
    ref = text.strip().strip("<>").rstrip(".,;")
    if m := _ARXIV_ID.match(ref):
        return arxiv_item(m.group(1), ref)
    if m := _ARXIV_URL.search(ref):
        return arxiv_item(m.group(1).removesuffix(".pdf"), ref)
    if m := _DOI.match(ref):
        doi = m.group(1).rstrip(".")
        if a := _ARXIV_DOI.match(doi):
            return arxiv_item(a.group(1), ref)
        return Item("doi", f"doi:{doi.lower()}", ref, doi=doi)
    if _URL.match(ref):
        return Item("url", f"url:{ref}", ref, url=ref)
    return Item("title", f"title:{normal_title(ref)}", ref)


def arxiv_item(arxiv_id: str, ref: str) -> Item:
    plain = _VERSION.sub("", arxiv_id)  # one paper, whatever version they named: the latest is read
    return Item("arxiv", f"arxiv:{plain.lower()}", ref, arxiv_id=plain)


def normal_title(title: str) -> str:
    folded = ascii_fold(title).lower()
    return " ".join(re.findall(r"[a-z0-9]+", folded))


def exact_title_matches(title: str, found: Sequence[str]) -> list[int]:
    """The candidates whose title is their title, word for word (case, accents and punctuation aside). One
    such match is picked; anything else (none, several, only near ones) goes back to them: never picked
    silently."""
    want = normal_title(title)
    return [i for i, t in enumerate(found) if want and normal_title(t) == want]


# --- the turn -----------------------------------------------------------------------------------------------

_COURSE = re.compile(
    r"\b(?:cors[oi]|lezion[ei]|paper|papers|articol[oi] scientific[oi]|le (?:mie )?fonti|nelle fonti|"
    r"secondo le fonti|arxiv|course|courses|lesson|lessons|my sources|the sources|pisa learn)\b",
    re.IGNORECASE,
)


LEARN_TOOLS: frozenset[str] = frozenset({"learn_ask", "learn_sources"})  # the brain's course tools


def about_course(words: str) -> bool:
    """A turn about a course or its sources: the only turns that get Pisa Learn's tools and line."""
    return bool(_COURSE.search(words or ""))


_RATIONALE = re.compile(
    r"\b(?:perch[eé]|come mai|why|how come)\b"
    r".*\b(?:non|invece|anzich[eé]|piuttosto|not|instead|rather|over)\b"
    r"|\b(?:scelt[oaie]|scegli\w*|chose|choose|chosen|choice|design choice|motivazion\w*|rationale)\b",
    re.IGNORECASE,
)


def is_rationale(question: str) -> bool:
    """A "why this design choice" question: "perché i layer 5 e 9 e non 7 e 8?"."""
    return bool(_RATIONALE.search(question or ""))


_IT_WORDS = frozenset(
    "il lo la i gli le di del della dei delle che perché come non sono è nel nella quale quali cosa usano "
    "usa "
    "hanno con per una uno tra dove quando quanto".split()
)
_EN_WORDS = frozenset(
    "the of and is are why what which how does do did in on with for not they their was were this that "
    "to".split()
)


def question_lang(text: str) -> Literal["it", "en"]:
    words = re.findall(r"[a-zàèéìòù]+", (text or "").lower())
    it = sum(w in _IT_WORDS for w in words)
    en = sum(w in _EN_WORDS for w in words)
    return "it" if it > en else "en"


_NUMBERED = re.compile(
    r"\b(layers?|strat[oi]|livell[oi]|tables?|tabell[ae]|figures?|figur[ae]|fig\.|appendix|appendice|"
    r"appendici|sections?|sezion[ei]|§|eq\.|equations?|equazion[ei])\s*"
    r"([0-9]+(?:\.[0-9]+)*|[A-H]\b)((?:\s*(?:,|e|and|o|or)\s*(?:[0-9]+(?:\.[0-9]+)*|[A-H]\b))*)",
    re.IGNORECASE,
)
_LAYER_L = re.compile(r"\bL([0-9]{1,3})\b")
_CANON = {
    "layer": "layer",
    "layers": "layer",
    "strato": "layer",
    "strati": "layer",
    "livello": "layer",
    "livelli": "layer",
    "table": "table",
    "tables": "table",
    "tabella": "table",
    "tabelle": "table",
    "figure": "figure",
    "figures": "figure",
    "figura": "figure",
    "fig.": "figure",
    "appendix": "appendix",
    "appendice": "appendix",
    "appendici": "appendix",
    "section": "section",
    "sections": "section",
    "sezione": "section",
    "sezioni": "section",
    "§": "section",
    "eq.": "equation",
    "equation": "equation",
    "equations": "equation",
    "equazione": "equation",
    "equazioni": "equation",
}


def exact_terms(question: str) -> list[str]:
    """The exact numbers a question names, in the corpus's English ("i layer 5 e 9" → layer 5, layer 9;
    "Tabella 3" → table 3; "L9" → layer 9): the lexical leg ranks the passages that hold them first."""
    found: list[str] = []
    for m in _NUMBERED.finditer(question or ""):
        word = _CANON.get(m.group(1).lower(), m.group(1).lower())
        numbers = [m.group(2), *re.findall(r"[0-9]+(?:\.[0-9]+)*|\b[A-H]\b", m.group(3) or "")]
        found += [f"{word} {n.lower()}" for n in numbers]
    found += [f"layer {n}" for n in _LAYER_L.findall(question or "")]
    return list(dict.fromkeys(found))


# --- segments and chunks ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Seg:
    """One paragraph, table, figure caption, equation or appendix paragraph, in reading order."""

    kind: SegKind
    text: str
    section_path: tuple[str, ...] = ()
    label: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    is_description: bool = False
    cites: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChunkPlan:
    seg_from: int
    seg_to: int
    kind: SegKind
    text: str
    is_description: bool


def chunk(segments: Sequence[Seg], max_chars: int = CHUNK_MAX_CHARS) -> list[ChunkPlan]:
    """Consecutive paragraphs of one section join until `max_chars`; a table, an equation or a caption is a
    chunk of its own, so no chunk ever crosses a table or an equation. Appendix paragraphs stay appendix."""
    out: list[ChunkPlan] = []
    start, texts, kind = -1, list[str](), "paragraph"

    def close(end: int) -> None:
        nonlocal start, texts
        if start >= 0 and texts:
            out.append(ChunkPlan(start, end, kind, "\n\n".join(texts), False))  # type: ignore[arg-type]
        start, texts = -1, []

    for i, seg in enumerate(segments):
        if seg.kind in ALONE or seg.is_description:
            close(i - 1)
            out.append(ChunkPlan(i, i, seg.kind, seg.text, seg.is_description))
            continue
        prev = segments[i - 1] if i else None
        same = (
            start >= 0
            and prev is not None
            and prev.section_path == seg.section_path
            and prev.kind == seg.kind
            and sum(len(t) for t in texts) + len(seg.text) <= max_chars
        )
        if not same:
            close(i - 1)
            start, kind = i, seg.kind
        texts.append(seg.text)
    close(len(segments) - 1)
    return out


_IT_LABEL = (("Table", "Tabella"), ("Figure", "Figura"), ("Appendix", "Appendice"), ("Section", "Sezione"))


def label_it(label: str) -> str:
    for en, it in _IT_LABEL:
        if label.startswith(en):
            return it + label[len(en) :]
    return label


def prefix(title: str, section_path: Sequence[str], label: str | None, lang: Literal["it", "en"]) -> str:
    """The chunk's structural context, before any model's words: the document, its section, its label."""
    where = " / ".join(section_path)
    if lang == "it":
        bits = [f"Documento: {title}", f"sezione: {where}" if where else "", label_it(label) if label else ""]
    else:
        bits = [f"Document: {title}", f"section: {where}" if where else "", label or ""]
    return "; ".join(b for b in bits if b)


def parse_quality(segments: Sequence[Seg], *, pages_expected: bool) -> float:
    """0..1: the share of segments with a page (when pages exist), whether sections, tables or equations were
    found at all. A low score says the parse lost structure."""
    if not segments:
        return 0.0
    paged = sum(s.page_from is not None for s in segments) / len(segments) if pages_expected else 1.0
    sectioned = sum(bool(s.section_path) for s in segments) / len(segments)
    structure = 1.0 if any(s.kind in ("table", "equation", "caption") for s in segments) else 0.5
    return round((paged + sectioned + structure) / 3, 3)


def page_of(text: str, pages: Sequence[str]) -> int | None:
    """The PDF page (1-based) whose text holds this segment's opening words: the page-number oracle for text
    parsed from LaTeX or HTML."""
    words = _plain_words(text)[:8]
    if len(words) < 3:
        return None
    probe = " ".join(words)
    for i, page in enumerate(pages):
        if probe in " ".join(_plain_words(page)):
            return i + 1
    probe = " ".join(words[:4])
    hits = [i for i, page in enumerate(pages) if probe in " ".join(_plain_words(page))]
    return hits[0] + 1 if len(hits) == 1 else None


def _plain_words(text: str) -> list[str]:
    return re.findall(r"[a-z]{2,}", ascii_fold(text).lower())


# --- retrieval ----------------------------------------------------------------------------------------------


def rrf(rankings: Iterable[Sequence[int]], k: int = RRF_K) -> list[int]:
    """Reciprocal-rank fusion: each list adds 1 / (k + rank) to an id's score; best first, ties by first
    seen."""
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            score[item] = score.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(score, key=lambda i: -score[i])


# --- the cited answer ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cite:
    """A citation the Citations API returned: which passage (search result) and which of its blocks."""

    passage: int  # the search_result's index in the request
    block_from: int
    block_to: int
    cited_text: str = ""


@dataclass
class Sentence:
    text: str
    cites: list[Cite] = field(default_factory=list)
    tier: int = 0  # 1, 2, 3; 0 = dropped
    verdict: Verdict | None = None
    label: str | None = None  # the cited block's label, for a tier-2 sentence ("Table 3")


_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÀ-Ý0-9«\"(])")


def sentences(blocks: Sequence[tuple[str, Sequence[Cite]]]) -> list[Sentence]:
    """The answer's sentences from the API's text blocks: a cited block is one claim with its citations; an
    uncited block is split at sentence ends (its first piece finishes the sentence still open)."""
    out: list[Sentence] = []
    open_: Sentence | None = None
    for text, cites in blocks:
        if cites:
            if open_ is None:
                open_ = Sentence("")
            open_.text += text
            open_.cites.extend(cites)
            if _ends(open_.text):
                out.append(open_)
                open_ = None
            continue
        pieces = _END.split(text)
        for j, piece in enumerate(pieces):
            if open_ is None:
                open_ = Sentence("")
            open_.text += piece
            if j < len(pieces) - 1 or _ends(open_.text):
                out.append(open_)
                open_ = None
    if open_ is not None:
        out.append(open_)
    return [Sentence(" ".join(s.text.split()), s.cites) for s in out if s.text.strip(" .,;:\n")]


def _ends(text: str) -> bool:
    return text.rstrip().endswith((".", "!", "?", "…"))


_INFERENCE = re.compile(
    r"\((?:mia deduzione|my inference)\)|\b(?:si può dedurre|one can infer)\b", re.IGNORECASE
)
_ABSENCE = re.compile(
    r"\b(?:non (?:lo )?(?:dic\w*|spieg\w*|motiv\w*|indic\w*|riport\w*|giustific\w*)|"
    r"nessuna (?:delle mie )?fonti?|"
    r"(?:does|do|did) not (?:say|state|explain|give|report|justify|mention)|doesn't (?:say|state|explain)|"
    r"don't (?:say|state|explain)|not stated|no (?:stated )?(?:reason|rationale)|"
    r"none of (?:my|the) sources)\b",
    re.IGNORECASE,
)


def is_inference(text: str) -> bool:
    return bool(_INFERENCE.search(text))


def is_absence(text: str) -> bool:
    """The sources' silence said plainly ("il paper non dice perché…"): tier 3, kept, no citation needed."""
    return bool(_ABSENCE.search(text))


_SECOND_CLAUSE = re.compile(r"[;:]|,\s*(?:ma|però|tuttavia|but|however|though|yet)\b", re.IGNORECASE)


def plain_absence(text: str, question: str) -> bool:
    """An uncited sentence is spoken (tier 3) only when it says nothing but the sources' silence: one
    clause, short, no number the question didn't name (no unchecked claim rides on an absence)."""
    numbers = set(re.findall(r"\d+(?:[.,]\d+)?", text)) - set(re.findall(r"\d+(?:[.,]\d+)?", question))
    return is_absence(text) and len(text.split()) <= 20 and not numbers and not _SECOND_CLAUSE.search(text)


def tier(sentence: Sentence, verdict: Verdict | None, *, only_descriptions: bool) -> int:
    """The tier this sentence may be spoken at (0: dropped). Never tier 1 without the judge's `supported` on
    the author's own text."""
    if not sentence.cites:
        if is_absence(sentence.text):
            return 3
        return 0  # an uncited claim is never spoken: there is nothing it could be checked against
    if verdict in (None, "unsupported", "contradicted"):
        return 0
    if verdict == "partial" or is_inference(sentence.text) or only_descriptions:
        return 2
    return 1


# --- the spoken answer --------------------------------------------------------------------------------------

TIER1 = {"it": "Le fonti dicono: ", "en": "The sources say: "}
TIER2 = {
    "it": "Non lo dicono direttamente; da {label} si può dedurre: {text} (mia deduzione).",
    "en": "They don't say it directly; from {label} one can infer: {text} (my inference).",
}
TIER3 = {
    "it": "Nessuna delle mie fonti lo spiega: vuoi che cerchi stanotte?",
    "en": "None of my sources explains this: shall I look tonight?",
}
OFFER = {"it": "Vuoi che cerchi stanotte?", "en": "Shall I look tonight?"}
STATED = {"it": "Il motivo dichiarato: ", "en": "The stated reason: "}
EMPIRICAL = {"it": "Le prove empiriche: ", "en": "The empirical evidence: "}
NO_REASON = {
    "it": "Nessuna delle mie fonti dice il motivo di questa scelta: vuoi che cerchi stanotte?",
    "en": "None of my sources states the reason for this choice: shall I look tonight?",
}
THE_SOURCES = {"it": "le fonti", "en": "the sources"}
_MARKERS = re.compile(
    r"\s*\((?:mia deduzione|my inference)\)\s*|"
    r"^(?:non lo dicono direttamente;?\s*|they don't say it directly;?\s*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Where:
    """Where a cited block is: what each spoken sentence names ("source, section, page")."""

    source: str  # a short name: "Zou et al. 2023" or the title's first words
    section: str | None
    page: int | None
    label: str | None
    kind: SegKind


def cite_text(where: Where, lang: Literal["it", "en"]) -> str:
    bits = [where.source]
    if where.label:
        bits.append(label_it(where.label) if lang == "it" else where.label)
    if where.section:
        bits.append(f"§{where.section}")
    if where.page is not None:
        bits.append(f"p. {where.page}")
    return ", ".join(bits)


def compose(
    said: Sequence[Sentence],
    places: dict[int, list[Where]],
    lang: Literal["it", "en"],
    *,
    rationale: bool,
) -> str:
    """The answer as Pisa says it, each tier spoken as such, each sentence with its citation. `places[i]` is
    where sentence i's cited blocks are. A design-rationale answer separates the stated reason, the empirical
    evidence (tables, figures, captions, appendices) and the absence, in that order; absence said plainly."""

    def cited(i: int) -> str:
        text = _MARKERS.sub(" ", said[i].text).strip()
        where = places.get(i) or []
        if not where:
            return text
        refs = "; ".join(dict.fromkeys(cite_text(w, lang) for w in where))
        return f"{text.rstrip('.')} ({refs})."

    def empirical(i: int) -> bool:
        return any(w.kind != "paragraph" for w in places.get(i, []))

    one = [i for i, s in enumerate(said) if s.tier == 1]
    two = [i for i, s in enumerate(said) if s.tier == 2]
    three = [i for i, s in enumerate(said) if s.tier == 3]
    parts: list[str] = []
    if rationale:
        stated = [i for i in one if not empirical(i)]
        shown = [i for i in one if empirical(i)]
        if stated:
            parts.append(STATED[lang] + TIER1[lang].lower() + " ".join(cited(i) for i in stated))
        if shown:
            parts.append(EMPIRICAL[lang] + TIER1[lang].lower() + " ".join(cited(i) for i in shown))
    elif one:
        parts.append(TIER1[lang] + " ".join(cited(i) for i in one))
    for i in two:
        where = places.get(i) or []
        label = next((w.label for w in where if w.label), None)
        name = (label_it(label) if lang == "it" else label) if label else THE_SOURCES[lang]
        text = _MARKERS.sub(" ", said[i].text).strip().rstrip(".")
        refs = "; ".join(dict.fromkeys(cite_text(w, lang) for w in where))
        parts.append(TIER2[lang].format(label=name, text=f"{text} ({refs})" if refs else text))
    if rationale and not any(not empirical(i) for i in one):
        absent = [said[i].text for i in three]
        parts.append(" ".join(absent) + " " + OFFER[lang] if absent else NO_REASON[lang])
    elif three:
        parts.append(" ".join(said[i].text for i in three) + " " + OFFER[lang])
    if not parts:
        return TIER3[lang]
    return " ".join(p.strip() for p in parts)


def abstained(said: Sequence[Sentence]) -> bool:
    """An answer with no tier-1 or tier-2 sentence: Pisa said its sources don't explain it."""
    return not any(s.tier in (1, 2) for s in said)


def tier_counts(said: Sequence[Sentence]) -> list[int]:
    """[tier 1, tier 2, tier 3, dropped]."""
    return [sum(s.tier == t for s in said) for t in (1, 2, 3, 0)]
