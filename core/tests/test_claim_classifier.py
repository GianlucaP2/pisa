"""The claim rail's sentence classifier (the reviewer's probes, kept as tests). Left for
the builder: "Ho messo in fila…", "Done is better…", "Ok, la segno come idea.", "La sposto io se vuoi.",
"Lo metto in conto." (claimed), "Ho già sistemato tutto." (missed)."""

import pytest

from pisa.domain.claims import claim

INNOCENT = [
    "Oggi hai fatto 8000 passi.",
    "Il volo è sul 31.",
    "Prenotate il venerdì.",
    "Ho capito.",
    "Fatto sta che domani piove.",
    "Sono le 3.",
    "Te lo segno sul telefono.",  # showing
    "Mi metto subito al lavoro.",
    "Il treno è sul binario 3.",
    "Sei sul 31, giusto.",
    "Domani alle 10 hai il dentista, l'ho visto nel calendario.",
]


@pytest.mark.parametrize("s", INNOCENT)
def test_innocent_is_not_a_claim(s):
    assert claim(s) is None


CLAIMS = [
    ("I've moved it to 5.", "done"),
    ("Done.", "done"),
    ("I moved the dinner.", "done"),
    ("È tutto a posto, sono sul 30.", "done"),
    ("Li ho aggiunti al calendario.", "done"),
    ("Ci penso io, te le aggiungo subito.", "doing"),
    ("Now I'll move it to 5.", "doing"),
    ("Ok, I'll add it.", "doing"),
    ("Ecco, l'evento è stato creato.", "done"),
    ("Ok, sposto la cena alle 21.", "doing"),
    ("Fatto, non preoccuparti.", "done"),  # negation word elsewhere in the sentence
    ("Spostata la cena alle 21.", "done"),
    ("Aggiunto!", "done"),
    ("Messo in calendario.", "done"),
]


@pytest.mark.parametrize("s,kind", CLAIMS)
def test_claim_is_caught(s, kind):
    assert claim(s) is not None


@pytest.mark.parametrize(
    "s", ["Fatto, non preoccuparti.", "Done, no problem.", "Ho spostato la cena, niente di che."]
)
def test_talker_done_check_regression(s):
    # whisper.grounding uses claims_change now; the older _DONE_CLAIM regex caught all three
    from pisa.domain.claims import claims_change

    assert claims_change(s)
