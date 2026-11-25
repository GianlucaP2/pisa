"""Is a line Pisa shows them in their language? A model's free line (an email's triage reason)
may come back in the email's language or in English whatever it was asked; the phone shows it only when it
reads as their language, else the code's own line. Pure rules, no I/O.

The test is the little words each language cannot do without: a line is theirs when their language's are at
least as many as any other's. A line of two words or more with none of theirs is not their ("Invoice due
soon."); a line with no words, "F24 16/11", is anyone's.
"""

from pisa.domain.profile import plain

MARKERS: dict[str, frozenset[str]] = {
    "it": frozenset(
        "il lo la gli le di del della dei degli delle che e per non un una uno con su entro scade scadono "
        "sono ha hanno da dal dalla al alla nel nella questo questa tuo tua ti si ma perche gia ancora "
        "giorni pagamento richiesta mittente nessuna niente scadenza fattura rinnovo rinnovare prenotazione "
        "conferma promemoria imminente avviso urgente importo bolletta pagare consegna ordine accesso "
        "conto "
        # a move's purpose is checked on read too ("così arrivi in tempo" is theirs)
        "cosi piu anche tra quando oggi domani ieri dopo sempre mai ora".split()
    ),
    "en": frozenset(
        "the an of to and is are for with by within this that it its your you be has have will not on from "
        "expires days payment request sender nothing no which due soon deadline reminder invoice renewal "
        "required approaching booking confirmation promotional email alert notice order delivery "
        "account".split()
    ),
    "nl": frozenset(
        "de het een van en is zijn dat niet op met voor binnen je jouw uw wordt verloopt dagen betaling "
        "verzoek afzender niets geen".split()
    ),
}


def speaks(text: str | None, lang: str) -> bool:
    """`text` reads as `lang` (a BCP-47 tag: "it-IT"); a language Pisa has no markers for: always."""
    mine = lang.split("-")[0].lower()
    if mine not in MARKERS:
        return True
    words = plain((text or "").replace("'", " ")).split()
    counts = {code: sum(w in markers for w in words) for code, markers in MARKERS.items()}
    if counts[mine] == 0 and sum(w.isalpha() for w in words) >= 2:
        return False  # "Invoice due soon.": words, none of theirs; the code's own line is always a right one
    return counts[mine] >= max(n for code, n in counts.items() if code != mine)


def foreign(text: str | None, lang: str) -> bool:
    """`text` reads as another language: another's markers outnumber theirs. For a model's short free line (a
    gist, a place's or an intention's why): "Biglietti Trenitalia Roma-Milano" or "Terrazza tranquilla sul
    Naviglio" have none of another's markers and are theirs; `speaks` would refuse them."""
    mine = lang.split("-")[0].lower()
    if mine not in MARKERS:
        return False
    words = plain((text or "").replace("'", " ")).split()
    counts = {code: sum(w in markers for w in words) for code, markers in MARKERS.items()}
    return max(n for code, n in counts.items() if code != mine) > counts[mine]


def foreign_title(title: str | None, lang: str) -> bool:
    """A task's title the model wrote in another language than the turn's ("Ritirare la ricetta in farmacia"
    for "pick up my prescription"): another's markers and none of theirs, the names in it left out (a
    capitalised word after the first: "Dinner at La Pergola", "Pranzo da Da Gino" are theirs)."""
    mine = lang.split("-")[0].lower()
    if mine not in MARKERS:
        return False
    tokens = (title or "").replace("'", " ").split()
    kept = [w for i, w in enumerate(tokens) if i == 0 or not w[:1].isupper()]
    words = plain(" ".join(kept)).split()
    counts = {code: sum(w in markers for w in words) for code, markers in MARKERS.items()}
    return counts[mine] == 0 and max(n for code, n in counts.items() if code != mine) > 0
