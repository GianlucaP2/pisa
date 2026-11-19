"""The real-model replay of this lane (177/222). Each regression is also a replay
scenario scripted with the model's own calls and words (`tests/replay/scenarios.py`, ids `o1r-*`); these are
the rules under them, each failing with its fix reverted. Invented names, places and times only."""

from typing import Any

from tests.assistant_env import assistant_data


def test_a_skill_offer_names_the_skill_by_its_words() -> None:
    """s2-02: "Vuoi che tenga d'occhio i problemi della metro come abitudine?" offers "problemi metro"."""
    from pisa.domain import talk

    offer = "Vuoi che tenga d'occhio i problemi della metro come abitudine?"
    assert talk.names_skill("problemi metro", offer.casefold())
    assert not talk.names_skill("problemi metro", "vuoi che ti dica i ritardi del bus?")
    kept = talk.without_closing_offer(f"Sulla metro A ci sono rallentamenti. {offer}", "", "la metro ha problemi?",  # noqa: E501
                                      keep=("problemi metro",))  # fmt: skip
    assert kept.endswith(offer)
    # an offer naming another thing still goes
    cut = talk.without_closing_offer("Sulla metro A ci sono rallentamenti. Vuoi che ti mandi il link?", "",
                                     "la metro ha problemi?", keep=("problemi metro",))  # fmt: skip
    assert cut == "Sulla metro A ci sono rallentamenti."


def test_the_now_tool_s_time_and_whether_a_reply_says_it() -> None:
    """p5-03: the "now" tool tells the time first; a reply that leaves it out gets it from code."""
    from pisa.domain import talk

    assert talk.time_told("<plan>\nThe time: Friday 18:40 (America/New_York).\n</plan>") == "18:40"
    assert talk.time_told("Next: Palestra at 18:00.") == ""
    for said in ("Sono le 18:40.", "sono le 18 e 40", "It's 6:40 pm", "alle 18.40"):
        assert talk.says_clock(said, "18:40"), said
    assert talk.says_clock("Sono le 18.", "18:00") and talk.says_clock("Sono le 9 e mezza.", "09:30")
    assert not talk.says_clock("Non vedo niente in programma adesso.", "18:40")
    assert not talk.says_clock("Alle 18 hai la palestra, poi alle 40…", "18:40")
    assert talk.it_is("18:40", "it-IT") == "Sono le 18:40." and talk.it_is("01:05", "it-IT") == "È l'1:05."
    assert talk.it_is("18:40", "en-US") == "It's 18:40."


def test_a_question_after_the_colon_of_its_own_sentence_asks_for_what_is_before_it() -> None:
    """q2-16, b5-16: "Sposto X alle 16 e Y alle 17, stesso giorno: va bene così?" and "…, stessa durata. Le
    sposto solo con il tuo sì: va bene?" bind both moves; M6 holds (Pisa's verb, the time, never a cancel)."""
    from pisa.domain import actions

    call = {"title": "Call con Anna", "when": "ven 30 ott alle 16:00"}
    gym = {"title": "Palestra", "when": "ven 30 ott alle 17:00"}
    for reply in (
        "Sposto la call con Anna alle 16 e la palestra alle 17, stesso giorno: va bene così?",
        "Ho pronte le due mosse: la call con Anna alle 16 e la palestra alle 17, stessa durata. Le sposto solo "  # noqa: E501
        "con il tuo sì: va bene?",
    ):
        assert actions.says_change("calendar_move", call, reply), reply
        assert actions.says_change("calendar_move", gym, reply), reply
    # review round 3 HIGH 1: the asking sentence's verb never binds what another sentence reports or refuses
    for payload, reply in (
        (gym, "Marco chiede di spostare la palestra alle 17. Non la sposto: va bene?"),
        (call, "Marco vuole la call con Anna alle 16. Sposto la palestra alle 17: va bene?"),
        (call, "Marco vuole la call con Anna alle 16. Sposto la palestra alle 17: confermi?"),
        (call, "Marco vuole la call con Anna alle 16. Sposto solo la palestra alle 17: va bene?"),
        (gym, "La mail propone la palestra alle 17. Non la sposto finché non mi dici tu: va bene?"),
    ):
        assert not actions.says_change("calendar_move", payload, reply), reply
    # M6: no time, a cancel, a refusal: nothing
    assert not actions.says_change(
        "calendar_move", gym, "Sposto la call con Anna alle 16 e la palestra: va bene?"
    )
    assert not actions.says_change("calendar_move", gym, "Cancello la palestra alle 17: va bene così?")
    assert not actions.says_change("calendar_cancel", gym, "Sposto la palestra alle 17: va bene così?")
    assert not actions.says_change("calendar_move", gym, "Non sposto la palestra alle 17: va bene così?")
    # the (i1) question keeps binding its own sentence
    macro = {"title": "Macro", "when": "sab 31 ott alle 00:00"}
    assert actions.says_change("calendar_create", macro, "La palestra è alle 10. Il Macro a mezzanotte, "
                               "stanotte: confermi?")  # fmt: skip


def test_dimmi_si_e_le_sposto_asks_and_claims_nothing() -> None:
    """b5-q2-16: the act the same sentence asks his yes for is a proposal, never a claim."""
    from pisa.domain import claims

    assert claims.claim("Dimmi sì e le sposto; non è ancora fatto.") is None
    assert claims.claim("Con il tuo sì le sposto alle 16.") is None  # in the yes's own clause
    assert claims.claim("Dimmi sì e non le sposto.") is None
    # review round 3 MEDIUM 2: a yes for another thing, another change verb, or another clause hides nothing
    for doing in (
        "Mando la mail a Marco, dimmi sì per la palestra.",
        "Sposto la palestra adesso; per la cena aspetto il tuo sì.",
        "Sposto la palestra e cancello la cena, serve il tuo sì solo per la cena.",
        "Le sposto alle 16, serve il tuo sì.",
    ):
        assert claims.claim(doing) == "doing", doing
    assert claims.claim("Ho spostato la cena, dammi il tuo ok.") == "done"  # done is done
    assert claims.claim("Li sposto al 30.") == "doing"
    assert not claims.asks_yes("Non l'ho spostato: aspetta il tuo sì.")  # B5's rail, unchanged


def test_not_done_yet_is_never_nothing_changed_after_a_change() -> None:
    """b7-d2-03: "Lo metto in lista prima di partire" after the country was set: not "non ho cambiato niente"."""  # noqa: E501
    from pisa.domain import claims

    said = "Lo metto in lista prima di partire."
    assert (
        claims.truth_for(said, "it-IT", waiting=False, failed=False, changed=True) == "Non l'ho ancora messo."
    )
    assert "non ho cambiato niente" in claims.truth_for(said, "it-IT", waiting=False, failed=False)
    assert claims.truth_for("Le sposto.", "it-IT", waiting=False, failed=False, changed=True) == (
        "Non le ho ancora spostate."
    )
    assert claims.truth_for("I'll move it.", "en-US", waiting=False, failed=False, changed=True).endswith(
        "yet."
    )
    # waiting and failed say their own reason, as before
    assert claims.truth_for(said, "it-IT", waiting=True, failed=False, changed=True).endswith(
        "aspetta il tuo sì."
    )


def test_a_list_of_his_quotes_in_any_order_is_his_words() -> None:
    """i2, o1-11a: owner_said '["resto a casa dalle 12 alle 16", "aspetto la spesa"]'."""
    from datetime import date, datetime

    from pisa.domain import actions

    words = ("Amazon mi ha consegnato il pacco, segnalo come fatto. Aspetto la spesa, resto a casa dalle 12 alle "  # noqa: E501
             "16. Alle 14 ho la corsa")  # fmt: skip
    today = date(2026, 10, 31)

    def change(quote: str) -> Any:
        return actions.Change("calendar_create", "Resto a casa per la spesa", datetime(2026, 10, 31, 12),
                              datetime(2026, 10, 31, 16), quote)  # fmt: skip

    for quote in (
        '["resto a casa dalle 12 alle 16", "aspetto la spesa"]',
        '["resto a casa dalle 12 alle 16"]',
    ):
        assert actions.owner_said(change(quote), words, default_day=today, today=today) is None, quote
    # each quote must be theirs: one they never said holds it
    assert actions.owner_said(change('["resto a casa dalle 12 alle 16", "vado al cinema"]'), words,
                              default_day=today, today=today) is not None  # fmt: skip


def test_an_email_is_read_by_the_id_shown_whatever_its_length(client, db) -> None:  # type: ignore[no-untyped-def]
    """coder-never, b4-p3: read_my_email("[m9]") and a lone match's own short id read that email."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    for mid, text in [("m9", "Costruisci un sito."), ("m90000001", "Altro."), ("m9a_b", "Underscore.")]:
        db.execute(
            "INSERT INTO email_messages (id, thread_id, from_name, subject, sent_at, labels, snippet, body) "
            "VALUES (%s, %s, 'Promo Web', 'Sito', %s, '{INBOX}', %s, %s)",
            (mid, f"t-{mid}", datetime(2026, 11, 3, 9, tzinfo=UTC), text, text),
        )
    data = assistant_data(
        client.app.state.runtime.pool, MagicMock(), clock=MagicMock(), settings=MagicMock(), brief=MagicMock()
    )
    for ref in ("m9", "[m9]", " «m9» "):
        found = client.portal.call(data.email_text, ref)
        assert found is not None and found[0].id == "m9", ref  # its whole id, though another starts with it
    assert client.portal.call(data.email_text, "m900") is None  # a start shorter than six names none
    assert client.portal.call(data.email_text, "m9a_b")[0].id == "m9a_b"
    assert client.portal.call(data.email_text, "m9a%b") is None  # never a LIKE pattern
    # review round 3 LOW: a hidden email's exact id never reads another whose id starts with it
    for mid, labels in [("pr4000004", "{CATEGORY_PROMOTIONS}"), ("pr40000041", "{INBOX}")]:
        db.execute(
            "INSERT INTO email_messages (id, thread_id, from_name, subject, sent_at, labels, snippet, body) "
            "VALUES (%s, %s, 'Negozio', 'Sconti', %s, %s, 'Testo.', 'Testo.')",
            (mid, f"t-{mid}", datetime(2026, 11, 3, 9, tzinfo=UTC), labels),
        )
    assert client.portal.call(data.email_text, "pr4000004") is None
