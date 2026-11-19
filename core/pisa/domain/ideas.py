"""The idea engine's rules, pure.

- **Problems** come from their own words ("Pisa, problema: …") or their own records (a goal they said or
  confirmed); **fragments** only from their "Pisa, idea: …". Pisa never makes either up.
- **Forced diversity**: the constraint deck and far personas drawn per reframe, dedup at cosine 0.8
  (the cluster's size is the idea's convergence), the distance quota 70/20/10 near/atypical/far, every lever
  logged per idea. The generate-critique cycle runs a fixed **k = 2** (no bandit over k).
- **The Validation Ladder**, V0-V2, V4 (labelled simulated) and V5 (conscience). **V3, the Night Lab, is a
  typed hook only** (`LabRequest`, `LAB_ON = False`): the lab's budget is the user's.
- **"Validated" means checked by code**: a link resolves (the page was fetched), a number is on the fetched
page
  (`on_page`), a computation's output is its own (`economics`). Kill criteria are typed and written before any
  evidence (`fires`).
"""

import hashlib
import math
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from pisa.domain.lexicon.numbers import comma_float

# --- caps and sizes ---

K = 2  # FACT: k = 2 generate-critique cycles; a bandit over k ∈ {2…8} is not built
PROBLEMS_MAX = 5  # FACT: ≤ 5 problems a night
REST_DAYS = 30  # FACT: a problem not touched for 30 days rests
WEEK_DAYS = 7  # their words of the last week: new problems and fragments
REFRAMES = 3  # FACT: each problem restated three ways
PER_CALL = 10  # candidates per generation call (≈ 30 a problem in cycle 1)
DEDUP_COS = 0.8  # FACT (Si 2024): cluster at cosine ≥ 0.8
QUOTA = (("near", 0.7), ("atypical", 0.2), ("far", 0.1))  # FACT: 70/20/10
POOL_MAX = 16  # the tournament runs over ≤ 16 survivors (40 starve the ladder)
ROUNDS = 3  # a Swiss tournament, 3 rounds (≤ 24 judge calls a night)
FILTER_MAX = 8  # FACT: it filters to ≤ 8; it ranks nothing for them
MORNING_MAX = 3  # FACT: at most three ideas ready for a morning, one of them wild at most
RUNG_SEARCHES = 6  # FACT: a rung that needs a seventh search is "not provable cheaply"
NIGHT_SEARCHES = 26  # the ideas' share of the night's 40: ideas 26, curiosity 8, desk 6
IDEA_CAP_EUR = Decimal("0.10")  # FACT: ≤ €0.10 of Pisa's calls per idea (V0-V2, V4, V5)
# FACT: the rungs' own caps
RUNG_CAP_EUR = {"V2": Decimal("0.02"), "V4": Decimal("0.03"), "V5": Decimal("0.005")}
CEMETERY_PARENTS = 3  # killed ideas offered as parents in cycle 2 (the cemetery becomes a nursery)

KINDS = ("software", "service", "content", "arbitrage", "physical")  # its columns
RUNGS = ("V0", "V1", "V2", "V4", "V5")
GRADE = {None: 0, "V0": 1, "V1": 2, "V2": 3, "V5": 4}  # the map's grade: the highest validating rung

Band = Literal["near", "atypical", "far"]
Market = Literal["it", "us", "both"]
Kind = Literal["software", "service", "content", "arbitrage", "physical"]

# --- the constraint deck and the far personas ---

DECK: tuple[tuple[str, str], ...] = (
    ("zero_capital", "costa zero per partire: niente capitale, niente magazzino"),
    ("one_person", "una sola persona lo gestisce in 5 ore a settimana"),
    ("no_app", "funziona senza un'app: un messaggio, un foglio, una pagina"),
    ("public_data", "usa solo dati pubblici"),
    ("thirty_days", "il primo cliente paga entro 30 giorni"),
    ("ten_x", "regge a 10x il volume senza assumere"),
    ("no_ads", "nessuna pubblicità: i clienti arrivano da un canale che esiste già"),
    ("other_country", "funziona anche nell'altro paese (Italia <-> USA)"),
    ("subtract", "togli un passaggio che oggi tutti fanno"),
    ("reverse", "il cliente diventa il fornitore"),
    ("bundle", "unisci due servizi che oggi si comprano separati"),
    ("price_tenth", "costa un decimo dell'alternativa di oggi"),
)
PERSONAS: tuple[tuple[str, str], ...] = (
    ("logistics", "un responsabile di logistica portuale"),
    ("nurse", "un'infermiera di pronto soccorso che organizza turni"),
    ("farmer", "un agricoltore che vende in filiera corta"),
    ("game_designer", "un game designer che progetta ricompense"),
    ("librarian", "una bibliotecaria che cataloga archivi"),
    ("insurer", "un attuario che prezza il rischio"),
    ("chef", "uno chef che gestisce sprechi e mise en place"),
    ("ngo", "chi coordina volontari in una ONG"),
    ("pilot", "un pilota che lavora per checklist"),
    ("tailor", "un sarto che lavora su misura"),
)
LEVERS_ALWAYS = ("cot", "verbalized_sampling")  # FACT: Meincke 2024; Zhang 2025


def draw(seed: str, deck: Sequence[tuple[str, str]], n: int = 1) -> list[tuple[str, str]]:
    """n cards from a deck, by a stable seed (the night, the problem, the reframe): the same night draws the
    same cards, two reframes draw different ones."""
    rng = random.Random(  # noqa: S311 (a stable draw, not a secret)
        int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16)
    )
    return rng.sample(list(deck), min(n, len(deck)))


# --- their words: problems and fragments ---

_LEAD = r"^\s*(?:(?:hey|ehi|ok)\s+)?pisa\s*[,:]?\s*"
_DASH = r"\s*[:\-\u2013\u2014]\s*"
_FRAGMENT = re.compile(_LEAD + r"(?:ho\s+un['\u2019]\s*)?idea" + _DASH + r"(?P<text>.+)$", re.I | re.S)
_PROBLEM = re.compile(
    _LEAD
    + r"(?:(?:il\s+mio\s+|un\s+)?problema|problem|the\s+problem)\s*(?:è|is)?"
    + _DASH
    + r"(?P<text>.+)$",
    re.I | re.S,
)
WORDS_MIN = 3  # a fragment or a problem says something: three words at least


def fragment(text: str) -> str | None:
    """Their "Pisa, idea: …" (or "Pisa, ho un'idea: …"): the idea in their words, else None. Only they write
    fragments."""
    found = _FRAGMENT.match(text or "")
    said = " ".join(found.group("text").split()) if found else ""
    return said if len(said.split()) >= WORDS_MIN else None


def problem(text: str) -> str | None:
    """Their "Pisa, problema: …" (or "Pisa, the problem is: …"): the problem in their words, else None."""
    found = _PROBLEM.match(text or "")
    said = " ".join(found.group("text").split()) if found else ""
    return said if len(said.split()) >= WORDS_MIN else None


# --- vectors: dedup and distance ---

DIM = 256


def local_vector(text: str) -> list[float]:
    """A free, local embedding: hashed character trigrams of the folded words, unit length. The fallback when
    no embedding model is chosen; dedup and distance work the same on either."""
    vec = [0.0] * DIM
    words = re.findall(r"[^\W_]+", text.casefold())
    for word in words:
        padded = f" {word} "
        for i in range(len(padded) - 2):
            h = int.from_bytes(hashlib.blake2b(padded[i : i + 3].encode(), digest_size=4).digest(), "big")
            vec[h % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb) if na and nb else 0.0


def dedup(vectors: Sequence[Sequence[float]], against: Sequence[Sequence[float]] = ()) -> list[list[int]]:
    """Greedy clusters at cosine ≥ 0.8, in order: each cluster's first member is kept, the rest are its
    duplicates (the cluster's size is the convergence count, Kornish & Ulrich). A candidate within 0.8 of
    `against` (ideas already kept) joins no cluster: it is dropped (index absent from the result)."""
    clusters: list[list[int]] = []
    for i, v in enumerate(vectors):
        if any(cosine(v, a) >= DEDUP_COS for a in against):
            continue
        for cluster in clusters:
            if cosine(v, vectors[cluster[0]]) >= DEDUP_COS:
                cluster.append(i)
                break
        else:
            clusters.append([i])
    return clusters


def distance(v: Sequence[float], known: Sequence[Sequence[float]]) -> float:
    """From their existing ideas, their fragments and the problem's obvious answer: 1 - the nearest cosine."""
    return round(1.0 - max((cosine(v, k) for k in known), default=0.0), 4)


def bands(distances: Sequence[float]) -> list[Band]:
    """Each candidate's band, by its rank in tonight's distances: the farthest 10% far, the next 20% atypical,
    the rest near (no absolute scale is calibrated yet: the ranks keep the quota honest on any embedder)."""
    order = sorted(range(len(distances)), key=lambda i: (-distances[i], i))
    n = len(distances)
    far = max(1, round(n * 0.1)) if n >= 3 else 0
    atypical = max(1, round(n * 0.2)) if n >= 3 else 0
    out: list[Band] = ["near"] * n
    for rank, i in enumerate(order):
        out[i] = "far" if rank < far else "atypical" if rank < far + atypical else "near"
    return out


def quota(ranked: Sequence[int], band_of: dict[int, Band], n: int) -> list[int]:
    """Up to n of `ranked` (best first) with the 70/20/10 quota: each band gets its share first (at least one
    far and one atypical when there are any), the rest filled by rank. A filter never drops every far idea."""
    if n <= 0:
        return []
    picked: list[int] = []
    for band, share in QUOTA:
        want = max(1, math.floor(n * share)) if band != "near" else math.floor(n * share)
        picked += [i for i in ranked if band_of.get(i) == band][:want]
    for i in ranked:
        if len(picked) >= n:
            break
        if i not in picked:
            picked.append(i)
    keep = set(picked[:n])
    return [i for i in ranked if i in keep]


def fit(statement: str, mechanism: str) -> bool:
    """The fit gate: "name the mechanism". No mechanism of three words or more, distinct from the
    statement, no survival."""
    m = " ".join(mechanism.split()).casefold()
    return len(m.split()) >= WORDS_MIN and m != " ".join(statement.split()).casefold()


# --- the Swiss tournament: pairs by score, never a rematch ---


def swiss_pairs(scores: dict[int, int], met: set[frozenset[int]]) -> list[tuple[int, int]]:
    """One round's pairs: by score then index, each with the next it hasn't met; an odd one out sits."""
    waiting = sorted(scores, key=lambda i: (-scores[i], i))
    pairs: list[tuple[int, int]] = []
    while len(waiting) > 1:
        a = waiting.pop(0)
        for j, b in enumerate(waiting):
            if frozenset((a, b)) not in met:
                pairs.append((a, b))
                waiting.pop(j)
                break
    return pairs


# --- kill criteria: typed, written before any evidence ---

METRICS: tuple[str, ...] = ("competitors", "buyers", "revenue_year_eur", "margin", "payback_months")


@dataclass(frozen=True)
class Kill:
    metric: str
    op: Literal["<", ">"]
    value: float
    said: str = ""


def fires(kill: Kill, facts: dict[str, float]) -> bool | None:
    """Whether a kill criterion is true on the facts code has checked; None while its metric is unknown."""
    if kill.metric not in facts:
        return None
    got = facts[kill.metric]
    return got < kill.value if kill.op == "<" else got > kill.value


def first_fired(kills: Sequence[Kill], facts: dict[str, float]) -> int | None:
    return next((i for i, k in enumerate(kills) if fires(k, facts)), None)


# --- evidence: a number is on the page ---

_NUM = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:(?:[.,]|[ \u00a0\u202f])\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"(?:\s*(k|mila|mln|milioni|milione|million|millions|mld|miliardi|miliardo|billion|billions|bn|m)\b)?",
    re.I,
)
_SCALE = {
    "k": 1e3, "mila": 1e3, "mln": 1e6, "milioni": 1e6, "milione": 1e6, "million": 1e6, "millions": 1e6,
    "mld": 1e9, "miliardi": 1e9, "miliardo": 1e9, "billion": 1e9, "billions": 1e9, "bn": 1e9,
}  # fmt: skip
TOLERANCE = 0.005  # a number matches within 0.5% (a rounding on the page, never a different figure)
NEAR_CHARS = 200  # a number counts only this close to one of its claim's words
_YEAR = re.compile(r"(?:19|20)\d\d|2100")


def _readings(token: str) -> set[float]:
    """The values a number written on a page can mean. A grouped number ("1.200", "38,000", "38 000") is
    read only as grouped (1200, 38000), never as a decimal; "1,2" is 1.2 either way."""
    plain = re.sub(r"[ \u00a0\u202f]", "", token)
    grouped: set[float] = set()
    for thousands, decimal in ((".", ","), (",", ".")):
        if re.fullmatch(rf"\d{{1,3}}(?:\{thousands}\d{{3}})+(?:\{decimal}\d+)?", plain):
            grouped.add(float(plain.replace(thousands, "").replace(decimal, ".")))
    if grouped:
        return grouped
    if plain != token:  # spaces that are no thousands grouping: two numbers, not one
        return set()
    try:
        return {comma_float(plain)}
    except ValueError:
        return set()


def numbers(text: str) -> set[float]:
    """Every number on a page, each read every way it can be (scales such as "38 mila" or "2.5 million")."""
    return {v for v, _ in _numbers(text)}


def _numbers(text: str) -> list[tuple[float, int]]:
    found: list[tuple[float, int]] = []
    for m in _NUM.finditer(text or ""):
        scale_word = (m.group(2) or "").casefold()
        if not scale_word and _YEAR.fullmatch(m.group(1)):  # a bare year is never a figure
            continue
        scale = _SCALE.get(scale_word, 1.0)
        found += [(v * scale, m.start()) for v in _readings(m.group(1))]
    return found


def _keywords(claim: str) -> set[str]:
    return {w for w in re.findall(r"[^\W\d_]{4,}", claim.casefold())}


def on_page(value: float, text: str, claim: str) -> bool:
    """RAIL ("every claim Pisa shows is checkable by code"): the claimed number is on the fetched page, within
    200 characters of one of the claim's own words (never a copyright year or a page number)."""
    if not math.isfinite(value):
        return False
    words = _keywords(claim)
    page = (text or "").casefold()
    spots = [m.start() for w in words for m in re.finditer(re.escape(w), page)]
    if not spots:
        return False
    return any(
        abs(v - value) <= max(abs(value) * TOLERANCE, 1e-9) and any(abs(at - s) <= NEAR_CHARS for s in spots)
        for v, at in _numbers(text)
    )


# Their names, unknown to Pisa's records: a query the model writes keeps no capitalised word but these
# (markets, institutions and categories, folded); everything else capitalised may be a person or a place.
GENERIC = frozenset(
    "italia italy italian italiano italiani usa us america american europe european europa eu ue uk "
    "istat eurostat inps inail sdi iva vat api app ai saas pmi sme b2b b2c ocse oecd census bls sec edgar "
    "openalex google apple android ios amazon".split()
)


def generic(query: str) -> str:
    """RAIL: a model-written query loses every capitalised word that is not a generic one (a market,
    an institution, a category), so a client's or a town's name Pisa never stored cannot leave Pisa either."""
    kept = [
        t for t in query.split() if not (t[:1].isupper() and re.sub(r"\W", "", t).casefold() not in GENERIC)
    ]
    return " ".join(kept)


def proper_nouns(text: str) -> list[str]:
    """The capitalised words of their own text (their problem, their fragments): passed to the frame's filter
    as names, whatever case the model later writes them in."""
    return sorted({w for w in re.findall(r"\b[A-ZÀ-Ý][^\W\d_]+", text or "") if w.casefold() not in GENERIC})


def same_site(cited: str, final: str) -> bool:
    """The page Pisa fetched is still the cited site after its redirects (same registrable domain): a
    parked domain or another site's home page is no evidence."""

    def site(url: str) -> str:
        host = re.sub(r"^https?://", "", url or "").split("/", 1)[0].split(":", 1)[0].casefold().rstrip(".")
        labels = host.split(".")
        two_level = (
            len(labels) >= 3 and len(labels[-2]) <= 3 and len(labels[-1]) == 2
        )  # co.uk, com.au, gov.it
        return ".".join(labels[-3:] if two_level else labels[-2:])

    return bool(site(cited)) and site(cited) == site(final)


def link(url: str) -> bool:
    """A link Pisa may cite and fetch: https, a host, no credentials, no spaces."""
    return bool(re.fullmatch(r"https://[^\s/@:]+(?::443)?(?:/\S*)?", url or "")) and len(url) <= 2000


# --- V2: the numbers, computed in code ---


@dataclass(frozen=True)
class Inputs:
    buyers: float  # linked: a number checked on a V1 page
    price_eur_month: float  # assumption, labelled
    uptake: float  # assumption, 0 < uptake < 1
    unit_cost_eur_month: float  # assumption
    cac_eur: float  # assumption


def economics(i: Inputs) -> dict[str, float]:
    """Unit economics, sizing and sensitivity, computed (never estimated in prose). The run is its own
    output: its every number comes from `i`."""
    customers = i.buyers * i.uptake
    revenue = customers * i.price_eur_month * 12
    unit = i.price_eur_month - i.unit_cost_eur_month
    margin = unit / i.price_eur_month if i.price_eur_month > 0 else -1.0
    payback = i.cac_eur / unit if unit > 0 else 1e9
    return {
        "customers": round(customers, 2),
        "revenue_year_eur": round(revenue, 2),
        "margin": round(margin, 4),
        "payback_months": round(payback, 2),
        "revenue_uptake_half": round(revenue / 2, 2),
        "revenue_uptake_double": round(revenue * 2, 2),
        "revenue_price_minus_20": round(revenue * 0.8, 2),
        "revenue_price_plus_20": round(revenue * 1.2, 2),
    }


def sane(i: Inputs) -> bool:
    return (
        i.buyers > 0
        and 0 < i.uptake < 1
        and i.price_eur_month > 0
        and i.unit_cost_eur_month >= 0
        and i.cac_eur >= 0
        and all(
            math.isfinite(x)
            for x in (i.buyers, i.uptake, i.price_eur_month, i.unit_cost_eur_month, i.cac_eur)
        )
    )


# --- V3: the Night Lab, a typed hook only ---

LAB_ON = False  # RAIL: nothing starts until the user sets the lab's budget


@dataclass(frozen=True)
class LabRequest:
    """What V3 would hand the Night Lab: composed by code from the idea's typed fields,
    never from page text. Built and logged, never started, while `LAB_ON` is False."""

    idea_id: str
    hypothesis: str
    kill_criteria: tuple[Kill, ...]
    origin: Literal["pisa_initiative"] = "pisa_initiative"
    budget_usd: Decimal = Decimal("3")
    verify: tuple[str, ...] = field(default_factory=tuple)


# --- the grade, in words ---


def grade_words(rungs: Iterable[str], facts: dict[str, float]) -> str:
    """The evidence grade said in words, V4 labelled simulated and the lab's absence said plainly."""
    done = set(rungs)
    parts: list[str] = []
    if "V0" in done:
        n = int(facts.get("competitors", 0))
        parts.append("verificato che non esiste uguale" if n == 0 else f"{n} simili trovati, linkati")
    if "V1" in done and "buyers" in facts:
        parts.append(f"mercato: {int(facts['buyers']):,} letto sulla pagina citata".replace(",", "."))
    if "V2" in done and "revenue_year_eur" in facts:
        parts.append(
            f"conti fatti in codice: circa €{int(facts['revenue_year_eur']):,} l'anno".replace(",", ".")
        )
    if "V4" in done:
        parts.append("pannello simulato: non è una prova")
    if "V5" in done:
        parts.append("chi ci perde: scritto")
    parts.append("la parte tecnica non è provata: il laboratorio non è ancora attivo")
    return "; ".join(parts)
