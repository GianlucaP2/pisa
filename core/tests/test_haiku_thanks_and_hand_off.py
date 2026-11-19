"""After a Haiku replay (148/174).
The `b9-*` scenarios in `tests/replay/scenarios.py` play each fix; these pin row 1's two readings."""

from pisa.brain.conversation import handed, handed_instruction
from pisa.domain import talk


def test_a_bare_thanks_or_greeting_asks_nothing_anything_more_does() -> None:
    for words in (
        "grazie Pisa",
        "buongiorno Pisa",
        "Grazie mille!",
        "ciao Pisa",
        "thanks",
        "good morning Pisa",
    ):
        assert talk.courtesy(words), words
    for words in (
        "ok",
        "perfetto",
        "sì grazie",
        "grazie, che tempo fa?",
        "buongiorno Pisa, cosa ho oggi",
        "",
    ):
        assert not talk.courtesy(words), words  # "ok"/"perfetto" can be a yes; a question is a question


def test_what_he_hands_pisa_is_named_and_only_then_paid() -> None:
    assert handed("Pisa, problema: perdo ore ogni mese a fare le fatture ai clienti") == "problem"
    assert handed("Pisa, idea: un'app per dividere le spese di casa") == "idea"
    assert handed("Pisa, tienimelo aperto: conviene il fotovoltaico in affitto?") == "question to hold open"
    assert handed("ho un problema con la macchina") is None
    assert handed_instruction("cosa ho domani?") == ""
    assert "Ask nothing" in handed_instruction("Pisa, problema: perdo ore ogni mese a fare le fatture")
