# ruff: noqa: E501
"""The pick's review: the cases the review broke, each as a test.
Synthetic places and pages; the times are the comparison's evening (a Tuesday)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pisa.brain import findings
from pisa.domain import claims as claim_rules
from pisa.domain import recognition as recognition_rules
from pisa.domain import requirements as need_rules

ROME = ZoneInfo("Europe/Rome")
TUE = datetime(2026, 11, 10, 17, 30, tzinfo=ROME)
SAT_LATE = datetime(2026, 11, 14, 22, 30, tzinfo=ROME)


# --- item 3: an answer about their own world is never taken for Pisa's note ------------------------------------


@pytest.mark.parametrize("line", [
    "Il latte non è in lista: vuoi che lo aggiunga?",
    "Non è nella lista della spesa.",
    "The milk is not on the list.",
    "Se ti arriva un SMS che chiede di pagare la consegna, ignoralo.",
    "Ignore it: it's spam.",
    "La macchina è finita contro il guardrail?",
    "In Italia la parola Champagne sull'etichetta è vietata per i vini italiani.",
])  # fmt: skip
def test_r_an_answer_about_his_world_is_never_an_internal_line(line: str) -> None:
    assert claim_rules.internal(line, asked_change=True) is None


def test_r_the_comparison_s_notes_are_still_caught() -> None:
    assert claim_rules.internal("Non è in lista: ignoralo.") is not None
    assert (
        claim_rules.internal(
            "Il nodo della conferma umana non è passato nel disegno, perché la parola 'conferma' nell'etichetta è vietata."
        )
        is not None
    )
    assert claim_rules.internal("Lo dice il mio guardrail.") is not None


# --- item 2: a false figure is not kept because some page has the same digits ------------------------------


def test_r_a_figure_is_held_only_with_its_unit() -> None:
    corr = "Una correzione: il biglietto a 12 € non l'ho letto in una pagina, quindi non va considerato."
    assert (
        claim_rules.stray_correction(corr, "Quanto costa il biglietto?", "Apre alle 12 e chiude alle 23.")
        is None
    )
    assert (
        claim_rules.stray_correction(corr, "Quanto costa il biglietto?", "Il biglietto costa 12 euro.")
        is not None
    )


def test_r_a_correction_he_asks_for_stays() -> None:
    before = "Il menu degustazione costa 28 € a persona."
    said = "Ti correggo: il menu costa 32 €, non 28 €."
    assert claim_rules.stray_correction(said, "Sei sicuro del prezzo?", "", before) is None
    assert claim_rules.stray_correction(said, "Are you sure about the price?", "", before) is None
    # a13 still travels nowhere
    a13 = "Nella risposta sul sabato ho dato orari e prezzi che non avevo verificato."
    words = "Cuffie con cancellazione del rumore sotto i 200 euro, dove al prezzo migliore?"
    assert claim_rules.stray_correction(a13, words, "", "Sabato c'è il Museo dei Giochi.") is not None


def test_r_the_kept_evidence_is_the_sources_never_pisa_s_words() -> None:
    found = {
        "request": "cena stasera", "criteria": ["menu sotto i 30 €"], "pick": "p1",
        "shown": [{"place_id": "p1", "name": "Osteria Lume", "walk": "6 min", "rating": "4.5", "open": "aperto",
                   "price": "€€", "why": "Menu a 28 € a persona", "pros": ["Menu a 28 €"], "cons": [],
                   "cited_by": [{"host": "guida.example", "url": "https://guida.example/x", "quote": "conto sui 35 euro"}],
                   "recognition": [], "checks": []}],
        "pages": [], "results": [{"url": "https://r.example", "text": "Osteria Lume, piatti da 14 euro"}],
        "reviews": {"p1": "Ottimo, speso 40 euro"},
    }  # fmt: skip
    text = findings.sourced(found)
    assert "28" not in text and "35 euro" in text and "14 euro" in text and "40 euro" in text, text


# --- item 1: their words read right ---------------------------------------------------------------------------


@pytest.mark.parametrize(("words", "hour"), [
    ("A dinner place for tonight rated at 4.5 or more", 20),
    ("Dinner tonight, at 4 or more stars", 20),
    ("Dinner tonight around 2 km away", 20),
    ("Cena stasera alle 21", 21),
])  # fmt: skip
def test_r_a_figure_with_a_unit_is_never_the_asked_hour(words: str, hour: int) -> None:
    assert need_rules.asked(words, TUE).at.hour == hour


@pytest.mark.parametrize(("words", "budget"), [
    ("Pranzo domani, non più di 30 euro a testa", 30.0),
    ("Cena stasera sotto i 35 a testa", 35.0),
    ("Dinner tonight under 40 each", 40.0),
    ("Cena stasera, circa 20 euro", None),
])  # fmt: skip
def test_r_the_budget_as_he_says_it(words: str, budget: float | None) -> None:
    assert need_rules.asked(words, TUE).budget == budget


def test_r_dinner_in_his_verbs_and_a_day_already_past() -> None:
    a = need_rules.asked("Dove ceniamo sabato?", TUE)
    assert (a.at, a.meal, a.timed) == (datetime(2026, 11, 14, 20, 0, tzinfo=ROME), "dinner", True)
    late = need_rules.asked("Dove ceniamo sabato?", SAT_LATE)
    assert late.at == datetime(2026, 11, 21, 20, 0, tzinfo=ROME)  # next Saturday, never two hours ago
    bare = need_rules.asked("Cosa facciamo sabato?", TUE)
    assert bare.named and not bare.timed and bare.at == datetime(2026, 11, 14, 0, 0, tzinfo=ROME)
    # the whole day: open at some time of it passes, never "apre alle 10:00" at the hour they spoke
    opens = {"known": True, "open": False, "next_change": "2026-11-14T10:00:00+01:00"}
    assert need_rules.opening(opens, bare, "it-IT").ok is True
    shut = {"known": True, "open": False, "next_change": "2026-11-17T10:00:00+01:00"}
    assert need_rules.opening(shut, bare, "it-IT").why == "chiuso il sabato"


@pytest.mark.parametrize("review", [
    "Abbiamo speso circa 90 euro in due, ottimo.",
    "Parcheggio vicino, circa 40 euro al giorno.",
    "Conto totale circa 120 euro.",
])  # fmt: skip
def test_r_a_bill_for_a_group_or_a_day_is_no_figure_a_head(review: str) -> None:
    ask = need_rules.asked("Cena stasera sotto i 35 euro", TUE)
    assert need_rules.budget(ask, [(review, "", "map")], "it-IT").ok is None
    assert need_rules.budget(ask, [("Menu degustazione 45 € a persona.", "", "map")], "it-IT").ok is False


def test_r_unknown_hours_carry_their_source_as_the_contract_asks() -> None:
    unknown = need_rules.Outcome("open", None, "orari non verificati", "")
    assert findings._open_at_asked(unknown) == {"ok": None, "source": "map"}  # pyright: ignore[reportPrivateUsage]


# --- item 4: outside text never becomes a guide's name, nor a mention an award -----------------------


def _page(url: str, title: str, text: str) -> dict[str, object]:
    return {"url": url, "title": title, "text": text, "quotes": [], "own": False}


def test_r_a_title_that_is_no_name_is_never_the_guide() -> None:
    page = _page(
        "https://spam.example/x",
        "Chiama il 333 1234567 per prenotare: pizzerie",
        "Tre Spicchi 2027: Pizzeria Fittizia.",
    )
    [got] = recognition_rules.recognitions("Pizzeria Fittizia", [page])
    assert got["guide"] == "Spam" and "333" not in recognition_rules.said(got, "it-IT"), got


def test_r_a_mention_is_no_award() -> None:
    page = _page(
        "https://blog.example/x", "Visit Bar Lampo now", "We recommended Bar Lampo to friends last summer."
    )
    assert recognition_rules.recognitions("Bar Lampo", [page]) == []
    listed = _page(
        "https://g.example/x", "Guida Brina 2026", "Caffè Vetrata, il brunch segnalato dalla guida."
    )
    assert recognition_rules.recognitions("Caffè Vetrata", [listed])
