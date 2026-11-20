"""The depth scenarios as the real model played them in a recorded depth run, for the stub: one map search,
two places checked, the old 2-place card, then a reply of ~50 words that reads the details aloud. The model
obeys what a tool tells it (as the real one does): when Pisa answers "Not shown yet: … the guides", it
searches and reads the guide, then shows the places again. Quick requests and follow-up turns keep the deep
script.

Measured before and after the research changes with the same script: the difference is Pisa's structure (the
lead, the why column, the guides asked for, the table), never a better model.
"""

from typing import Any

from tests.replay.depth import _local, _top, card, first, ms, pd

_LONG = {
    "it-IT": (
        "Ho trovato alcuni posti qui vicino che fanno al caso tuo. Il migliore secondo me è {a}, che ha "
        "un voto di {ra} con moltissime recensioni, è vicino e ha un ottimo assortimento. In alternativa "
        "c'è {b}, con un voto di {rb}, anch'esso vicino, un po' più tranquillo e con qualche tavolino. "
        "Te li ho messi sul telefono con tutti i dettagli."
    ),
    "en-US": (
        "I found a few places near you that should work. The best one in my view is {a}, rated {ra} "
        "with lots of reviews, close by and with a great selection. As an alternative there is {b}, "
        "rated {rb}, also close, a little quieter and with a few tables. I've put them on your phone "
        "with all the details."
    ),
}


def _calls(s: dict[str, Any], tool: str) -> list[tuple[str, dict[str, Any]]]:
    return [c for rnd in s["script"][0] if isinstance(rnd, list) for c in rnd
            if isinstance(c, tuple) and c[0] == tool]  # fmt: skip


def observed(s: dict[str, Any]) -> dict[str, Any]:
    """The scenario as the first recorded depth evaluation's model played it (see the module's docstring)."""
    if any(spec.get("quick") for spec in s["depth"]):
        return s
    maps = _calls(s, "map_search")
    if not maps:
        return s
    query = maps[0][1]["query"]
    best = _top(query, s["area"], _local(s["now"]))[:2]
    if len(best) < 2:
        return s
    a, b = best
    shown = card("Qui vicino", first(s), [(p.key, f"valutato {p.rating}", []) for p in best])
    long = _LONG.get(s["lang"], _LONG["en-US"]).format(a=a.name, ra=a.rating, b=b.name, rb=b.rating)
    web = _calls(s, "web_search")[:1] or [("web_search", {"query": first(s)})]
    read = _calls(s, "read_page")[:1]
    then: list[Any] = [web, *([read] if read else []), [shown], long]
    turn: list[Any] = [[ms(query)], [pd(a.key), pd(b.key)], [shown],
                       {"seen": "Not shown yet", "then": then[0], "else": long}, *then[1:]]  # fmt: skip
    return s | {"script": [turn, *s["script"][1:]]}


# A later recorded depth evaluation: the pick's sheet and lead
# held, but 7 of the 8 hard fails were a spoken answer over 60 words on a SECOND turn ("why not the other?",
# "quali guide la citano?", "quanto costa il menu?"): the model answered the follow-up well, then went on
# aloud. The tail adds no name and no figure (nothing to trace): only length.
_ON = {
    "it-IT": (
        " Te lo spiego meglio: è un posto che ha convinto quasi tutti quelli che l'hanno provato, il "
        "servizio è attento, l'ambiente è accogliente e quello che spendi vale quello che ricevi. Se vuoi "
        "posso guardare anche altri dettagli per te, come gli orari o le recensioni più recenti, e dirti "
        "cosa ne penso."
    ),
    "en-US": (
        " Let me explain a bit more: it is a place that convinced nearly everyone who tried it, the service "
        "is attentive, the room is welcoming and what you spend is worth what you get. If you want I can "
        "look at other details for you too, like the hours or the latest reviews, and tell you what I think."
    ),
}


def observed_2(s: dict[str, Any]) -> dict[str, Any]:
    """The scenario as the later depth evaluation's model played it: the first turn as the deep script (the
    sheet, the lead), each follow-up's answer read aloud at length (its own words, then ~50 more)."""
    if len(s["script"]) < 2:
        return s
    tail = _ON.get(s["lang"], _ON["en-US"])
    turns: list[Any] = [s["script"][0]]
    for n, turn in enumerate(s["script"][1:], start=1):
        spec = s["depth"][n] if n < len(s["depth"]) else {}
        if spec.get("quick") or not turn or not isinstance(turn[-1], str):
            turns.append(turn)
            continue
        turns.append([*turn[:-1], turn[-1] + tail])
    return s | {"script": turns}


# Three shapes the real model
# writes that the stub's follow-ups never did. Each must stay short with no hard fail.
_LIST = {
    "d06-recognised-dinner-explain-the-menu": (
        "Il menu vegetariano ha cinque portate:\n- risotto alle erbe di riva\n- sformato di zucca\n"
        "- tortelli di ricotta\n- verdure dell'orto alla brace\n- crostata di more\nIn tutto costa 55 euro."
    ),
    "d20-lunch-by-the-river-then-the-price": (
        "Le portate sono cinque:\n- risotto alle erbe di riva\n- sformato di zucca\n- tortelli di ricotta\n"
        "- verdure dell'orto alla brace\n- crostata di more\nIl menu vegetariano costa 55 euro."
    ),
}
_PHONE_OFFER = {
    "it-IT": " Te l'ho messo sul telefono, vuoi che guardi anche gli orari? Così decidi con calma.",
    "en-US": " It's on your phone, shall I check the hours too? Then you can decide calmly.",
}
_QUICK_ON = {
    "it-IT": (
        " Te lo dico perché è la cosa più semplice da fare adesso, senza giri inutili, e se cambi idea "
        "possiamo sempre guardare qualcos'altro insieme, magari con calma, quando hai più tempo e meno "
        "fretta di adesso."
    ),
    "en-US": (
        " I'm telling you this because it is the simplest thing to do right now, with no detours, and if "
        "you change your mind we can always look at something else together, calmly, when you have more "
        "time and less of a rush than now."
    ),
}


def _last_text(turn: list[Any], text: str) -> list[Any]:
    return [*turn[:-1], text]


def list_with_the_price_last(s: dict[str, Any]) -> dict[str, Any]:
    """A follow-up answered as a list, one item a line, the price on the last line."""
    if s["id"] not in _LIST:
        return s
    return s | {"script": [s["script"][0], _last_text(s["script"][1], _LIST[s["id"]])]}


def on_the_phone_and_an_offer(s: dict[str, Any]) -> dict[str, Any]:
    """Each follow-up: its first sentence, "it's on your phone" and an offer mid-answer, then the rest
    (H3b, e)."""
    if len(s["script"]) < 2:
        return s
    turns: list[Any] = [s["script"][0]]
    for turn in s["script"][1:]:
        if not turn or not isinstance(turn[-1], str):
            turns.append(turn)
            continue
        head, _, tail = turn[-1].partition(". ")
        mid = _PHONE_OFFER.get(s["lang"], _PHONE_OFFER["en-US"])
        turns.append(_last_text(turn, f"{head.rstrip('.')}.{mid} {tail}".strip()))
    return s | {"script": turns}


def quick_at_length(s: dict[str, Any]) -> dict[str, Any]:
    """A quick request answered in ~50 words: its fact, then ~40 more words with no name or figure."""
    if not any(spec.get("quick") for spec in s["depth"]):
        return s
    turn = s["script"][0]
    return s | {"script": [_last_text(turn, turn[-1] + _QUICK_ON.get(s["lang"], _QUICK_ON["en-US"]))]}
