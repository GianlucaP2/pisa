"""The user's words that ask Pisa to
find, recommend or compare something. Pure rules.

On such a turn the research tools and the screen's answer (`show_findings`, the sheet) are offered in the
first round and the research skill joins from the first request, never after a `search_tools` round (a
first plan made without the tools or the guide goes wrong). The model still decides what to
call; a quick request ("che ore sono", "sposta la palestra") matches nothing and stays as it was. A fact asked
about a place ("è aperto?", "com'è il Bistrot?") isn't a pick: its tools are found as before.

Also the short-lead rule (.3): once the complete answer is on the screen, Pisa's words are a lead of at
most LEAD_SENTENCES sentences, never a list read aloud.
"""

import re
from typing import Any
from urllib.parse import urlsplit

from pisa.domain.lexicon.numbers import comma_float
from pisa.domain.talk import WHOLE_SENTENCE
from pisa.domain.text import fold_words

RESEARCH_TOOLS = frozenset(
    {"map_search", "place_details", "open_at", "walk_times", "web_search", "read_page", "show_findings",
     "show_places", "compare_places", "show_sheet"}
)  # fmt: skip
# Pisa's findings of the last research, for a deeper question: offered whenever some are kept
FINDINGS_TOOL = "my_findings"

_NOT_A_SEARCH = (
    r"(?!\s+(?:di|d'|la\s+mail|le\s+mail|l'email|la\s+email|il\s+messaggio|i\s+messaggi|il\s+task|"
    r"l'evento|l'appuntamento|il\s+file|il\s+documento|my\b|the\s+email|the\s+message|"
    # their agenda, their contacts, their notes, their things are no research
    r"(?:un|il|lo|la|le|i|gli|mi|me)?\s*(?:buco|buchi|spazio|tempo|momento|slot|numero|numeri|contatto|"
    r"telefono|nota|note|chiavi|chiave|occhiali|portafoglio|agenda|calendario|promemoria)\b))"
)
_DAYS = (
    r"(?:oggi|domani|dopodomani|stasera|stamattina|lunedì|martedì|mercoledì|giovedì|venerdì|sabato|domenica|"
    r"lunedi|martedi|mercoledi|giovedi|venerdi|mattina|pomeriggio|sera|today|tomorrow|tonight)"
)
_ASKS = re.compile(
    r"\b(?:trova(?:mi|ci|re)?|cerca(?:mi|ci|re)?|consiglia(?:mi|ci|re)?|suggerisci(?:mi|ci)?|"
    r"raccomanda(?:mi|ci)?|confronta(?:mi|re)?|paragona(?:mi)?)\b" + _NOT_A_SEARCH + r"|"
    r"\bdove\s+(?:posso|potrei|possiamo|potremmo|si\s+può|si\s+puo|compro|trovo|mangio|mangiamo|"
    r"faccio|facciamo|prendo|bevo)\b|"
    # "dove vado a mangiare" is a pick; "dove vado domani alle 10?" is their agenda
    r"\bdove\s+(?:vado|andiamo)\s+(?:" + _DAYS + r"\s+)?(?:a|per)\s+\w|"
    # "le migliori pizzerie", never "sei il migliore" (a noun must follow)
    r"\b(?:i|le|il|la|gli)\s+miglior[ei]?\s+(?!cosa\b)[a-zà-ù]{3,}|\bqual\s+è\s+(?:il|la)\s+miglior|"
    r"\bmeglio\s+(?!" + _DAYS + r"\b)\w+(?:\s+\w+)?\s+o\b|"
    r"\b(?:find|recommend|suggest|compare|look\s+up|search\s+for)\b" + _NOT_A_SEARCH + r"|"
    r"\bwhere\s+(?:can|could|should|do)\s+(?:i|we)\b|\bthe\s+best\s+(?!thing\b)[a-z]{3,}|"
    r"\bwhich\s+is\s+better\b",
    re.IGNORECASE,
)


# a place to eat, drink or go, said with what makes it a pick (near, the guides, the best, tonight…)
_KIND = (
    r"colazion\w*|pranz\w*|cena|cenare|aperitiv\w*|brunch|pizz\w*|ristorant\w*|trattori\w*|osteri\w*|"
    r"gelat\w*|pasticceri\w*|caff[eè]|café|cafe|mangiare|eat|breakfast|lunch|dinner|restaurants?|coffee|"
    r"somewhere|un\s+posto|qualcosa\s+da\s+fare|something\s+to\s+do"
)
_CUE = (
    r"qui\s+vicino|vicino|in\s+zona|nearby|near\s+(?:me|here)|around\s+here|stasera|tonight|migliori?|best|"
    r"premiat\w*|riconosciut\w*|guid[ae]\w*|buon[ao]?|good|bene|quiet|tranquill\w*|con\s+calma|seduto|"
    r"dove\s+vado|for\s+(?:two|three|four|five|six|\d+)|per\s+(?:due|tre|quattro|cinque|sei|\d+)|before|"
    r"prima\s+del|all'aperto|outdoors?|to\s+work|per\s+lavorare|kids|bambini|vegetarian\w*|under\s+\d+|"
    r"sotto\s+i\s+\d+|raining|piove|cerco|looking\s+for"
)
_PICK_KIND = re.compile(rf"\b(?:{_KIND})\b", re.IGNORECASE)
_PICK_CUE = re.compile(rf"\b(?:{_CUE})\b", re.IGNORECASE)
# never a pick: a quick one ("un caffè al volo"), a fact about a place ("a che ora apre"), a command for their
# day ("aggiungi la cena con Marco stasera", "remind me about dinner tonight")
_NOT_A_PICK = re.compile(
    r"\b(?:al\s+volo|quick|veloce|a\s+che\s+ora|what\s+time|when(?:'s|\s+is|\s+does)|è\s+aperto|is\s+it\s+open|"
    r"aggiung\w*|mett\w*|segna\w*|spost\w*|ricord\w*|crea\w*|cancell\w*|annull\w*|invit\w*|scriv\w*|"
    r"mand\w*|chiam\w*|prenot\w*|add|put|move|remind|schedule|cancel|invite|write|send|call|book|"
    # what they did or have planned ("ho mangiato una buona pizza", "stasera cena con Giulia alle 20",
    # "il ristorante di stasera è vicino?") and a fact ("quanto costa…") are no pick
    r"(?:ho|abbiamo)\s+\w+(?:ato|uto|ito)|era|erano|had|was|were|ate|alle\s+\d+|at\s+\d+|quanto|how\s+much|"
    r"di\s+stasera|tonight's)\b",
    re.IGNORECASE,
)


def asks(words: str) -> bool:
    """Their words ask Pisa to find, recommend or compare something (a pick or a deep comparison).
    also a pick said with no verb (a place to eat, drink or go, and what makes it a pick)."""
    text = words or ""
    if _ASKS.search(text):
        return True
    return bool(_PICK_KIND.search(text) and _PICK_CUE.search(text) and not _NOT_A_PICK.search(text))


# a quick request (a coffee on the go, a place open now, when a place opens, the next departure) or a choice
# among things to buy ("che cuffie prendo sotto i 200 euro?") is answered short too
_SHORT_ASK = re.compile(
    r"\b(?:al\s+volo|on\s+the\s+go|apert[oaie]\s+(?:adesso|ora)|open\s+(?:now|right\s+now)|"
    r"a\s+che\s+ora\s+(?:apre|aprono|chiude|chiudono)|when\s+does\s+[^?]{1,40}?\s+(?:open|close)|"
    r"what\s+time\s+does|prossim[oa]\s+(?:treno|autobus|bus|tram|metro|corsa)|"
    r"next\s+(?:train|bus|tram|metro|departure)|"
    r"(?:che|quale|quali)\s+[a-zà-ù]{3,}(?:\s+[\w'-]+){0,6}?\s+(?:prendo|compro|scelgo|mi\s+conviene)|"
    r"which\s+[a-z]{3,}(?:\s+[\w'-]+){0,6}?\s+(?:should|do|would)\s+(?:i|you)\s+(?:buy|get|pick|recommend))\b",
    re.IGNORECASE,
)
# The tools of a research conversation: looking up, showing, and noting a taste they said (its consent
# question is code's, after the answer: d16). A follow-up that calls none other keeps the short rule; one that
# reads or changes their day (agenda, mail, tasks…) is no research conversation's
LOOKUPS = RESEARCH_TOOLS | {
    FINDINGS_TOOL, "search_tools", "show_note", "show_place", "focus_object", "close_object", "finish",
    "where_am_i", "what_now", "nearest_pharmacy", "next_departures", "travel_time", "airport_departure",
    "remember_about_me",
}  # fmt: skip


# a quick one wants one
# place, the nearest open: one card, never a table
_QUICK = re.compile(
    r"\b(?:al\s+volo|di\s+corsa|di\s+fretta|veloce|on\s+the\s+go|quick(?:ly)?|in\s+a\s+(?:rush|hurry))\b",
    re.IGNORECASE,
)


def quick(words: str) -> bool:
    """Their words ask for something quick ("un caffè al volo"): one card, never a table."""
    return bool(_QUICK.search(words or ""))


# a refusal for lack of a
# tool, or sending them to someone else's app, and an estimate by eye, come after the general tool (web
# search) was tried, never before
_DECLINES = re.compile(
    r"\b(?:non\s+(?:posso|riesco\s+a|ho\s+(?:un|una|gli|accesso|modo|orari|dati)|trovo|sono\s+in\s+grado)|"
    r"i\s+(?:can['\u2019]?t|cannot|couldn['\u2019]?t|could\s+not|don['\u2019]?t\s+have|do\s+not\s+have|have\s+no)|"
    r"(?:my|il\s+mio)\s+(?:live\s+)?(?:\w+\s+)?(?:feed|tool|strumento)\s+(?:only|copre\s+solo|covers\s+only)|"
    r"(?:check|use|open|controlla|usa|apri)\s+(?:the\s+|l['\u2019])?[\w'\u2019 ]{0,30}\bapp\b|"
    r"(?:operator|carrier|operatore)['\u2019]?s?\s+app|app\s+(?:dell['\u2019]?|del|di)\s*operatore)",
    re.IGNORECASE,
)
_BY_EYE = re.compile(
    r"\b(?:a\s+occhio|ad\s+occhio|a\s+spanne|grosso\s+modo|by\s+eye|a\s+rough\s+(?:estimate|guess)|"
    r"roughly\s+estimat\w*|rule\s+of\s+thumb)\b",
    re.IGNORECASE,
)


_QUANTITY = re.compile(
    r"\bquant[oaie]\s+(?:pesa|pesano|misura|misurano|dista|distano|costa|costano|dura|durano|consuma|consumano|"
    r"ci\s+(?:mette|mettono|vuole|vogliono)|(?:e|è|sono)\s+(?:lung|alt|larg|grand|profond|pesant|lontan|cald|"
    r"fredd|vecch|veloc)\w*)|\bquant[oaie]\s+(?:gradi|litri|chili|metri|chilometri|calorie|watt|anni)\b|"
    r"\bhow\s+(?:many|long|far|heavy|tall|high|wide|big|deep|hot|cold|fast|old)\b|"
    r"\bhow\s+much\b(?!\s+(?:did|have|do|am|was)\s+i\b)",
    re.IGNORECASE,
)


def unsearched(sentence: str, words: str, searched: bool) -> str | None:
    """Why a sentence waits for a web search (None: it may be said): it declines their look-up, or sends them
    to another app, or estimates by eye, and this turn tried no web search. Code's, from the words."""
    if searched:
        return None
    if _BY_EYE.search(sentence or "") and _QUANTITY.search(words or ""):
        # only on a quantity of the world they asked ("quanto pesa", "how far"), never their own
        # data's ("grosso modo 300 euro" on their spending: no web search for that)
        return "estimates by eye what nobody looked up"
    if (short_ask(words) or asks(words)) and _DECLINES.search(sentence or ""):
        return "declines their question without looking it up"
    return None


# two or more
# capitalised words in a row (with a name's little words between them) are a name; said only as a result,
# their words or their own data wrote it
_LINK = r"(?:d[aei]l\w*|di|de|of|the|sul|al|alla|dei|degli|delle|su|a|in)"
_NAME_RUN = re.compile(rf"[A-ZÀ-Ý][\w'\u2019]+(?:[ \t]+(?:{_LINK}[ \t]+)?[A-ZÀ-Ý][\w'\u2019]+)+")


_KINDS = frozenset(
    "pizzeria trattoria osteria ristorante bar caffe gelateria pasticceria enoteca hotel albergo museo "
    "libreria farmacia panificio forno birreria bistrot cafe restaurant bakery pub cinema teatro".split()
)  # a place's kind before its name ("Pizzeria Vico")


def plain(text: str) -> str:
    """Lower case, no accents, words only: names compared as written, whatever their spelling of accents."""
    return fold_words(text or "")


def misnamed(sentence: str, known: str) -> list[str]:
    """The names a sentence says that `known` (this turn's results, their words, their own data, as `plain`)
    doesn't hold as written: a run of capitalised words, or a tail of it (a sentence's first word before a
    name), or each part of it between little words ("Tre Lune di Tavola Chiara")."""
    held = f" {known} "

    def has(x: str) -> bool:
        return f" {plain(x)} " in held

    out = []
    text = sentence or ""
    for m in _NAME_RUN.finditer(text):
        run = re.sub(r"['\u2019]s\b", "", m.group(0))
        words = run.split()
        if not text[: m.start()].strip() or text[: m.start()].rstrip()[-1:] in '.!?:;«"(':
            # a sentence's first word is no name's ("Domani a Roma", "Controlla su Trenitalia"): the rest is,
            # when it still has two capitalised words
            rest = re.sub(rf"^\S+[ \t]+{_LINK}[ \t]+", "", run)
            if rest != run and not _NAME_RUN.fullmatch(rest):
                continue
            run, words = rest, rest.split()
        tails = [" ".join(words[i:]) for i in range(len(words) - 1) if words[i][:1].isupper()]
        if any(has(t) for t in tails):
            continue
        if plain(words[0]) in _KINDS and has(" ".join(words[1:])):
            continue  # "la Pizzeria Vico" for a result named "Vico": its kind and its written name
        parts = [x for x in re.split(rf"[ \t]+{_LINK}[ \t]+", run) if x.strip()]
        names = [x for x in parts if len(x.split()) > 1]  # a lone capitalised word: a place, a noun
        if len(parts) > 1 and names and all(has(x) for x in names):
            continue
        out.append(run)
    return out


def without_misnamed(sentence: str, known: str) -> str:
    """The sentence without the clauses (between , ; :) that hold a name `known` doesn't (d15: "Gelateria
    Nuvola, in via Nuvola a Lucimbra: ha il Cono d'oro 2026." → "Gelateria Nuvola: ha il Cono d'oro 2026."),
    when what is left still names something a result wrote; "" when it doesn't (the sentence goes back to the
    model); the sentence itself when it names nothing unwritten."""
    if not misnamed(sentence, known):
        return sentence
    body = sentence.rstrip()
    trail = sentence[len(body) :]
    end = body[-1] if body[-1:] in ".!?" else ""
    bits = re.split(r"(\s*[,;:]\s+)", body[: len(body) - len(end)])
    clauses, delims = bits[0::2], bits[1::2]
    keep = [not misnamed(c, known) for c in clauses]
    out = ""
    for i, c in enumerate(clauses):
        if keep[i]:  # joined by the delimiter just before it (after a dropped clause: that one's)
            out += (delims[i - 1] if out else "") + c
    out = out.strip()
    named = [m.group(0) for m in _NAME_RUN.finditer(out)]
    if not out or not named or misnamed(out, known) or len(out.split()) < 3:
        return ""
    return out[:1].upper() + out[1:] + end + trail


def short_ask(words: str) -> bool:
    """Their words make a quick request or a choice among things to buy."""
    return bool(_SHORT_ASK.search(words or ""))


LEAD_SENTENCES = 2  # "one or two sentences, as a friend would"
_LIST_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s")


def past_lead(sentence: str, said: list[str]) -> bool:
    """Once the complete answer is on the screen: this sentence would go past the short lead (the lead's
    sentences already said, `said`), or it is a list item read aloud."""
    if _LIST_LINE.match(sentence):
        return True
    return sum(1 for s in said if s.strip()) >= LEAD_SENTENCES


# --- the spoken lead ----------------------------------------------------------------------------------------

# The lead Pisa speaks after a research turn: the model's own `lead` (show_findings) when it fits, else Pisa's
# own. 30 words leave room for the interim "Cerco, un attimo." inside the depth's 35.
LEAD_WORDS = 30
_WORD = re.compile(r"\w+(?:['\u2019]\w+)?")
# a decimal or a clock time ("4.5", "alle 7.30") and an initial ("Caffè S. Eustachio", "F.lli") never
# end a sentence: the lead is cut between sentences only (one splitter, `talk.WHOLE_SENTENCE`)
_SENTENCE = WHOLE_SENTENCE
_READ_ALOUD = re.compile(r"https?://|www\.|\|")


def spoken_words(text: str) -> int:
    """Words as a listener counts them ("dell'Osteria" is one)."""
    return len(_WORD.findall(text or ""))


def _key(text: str) -> str:
    return fold_words(text or "")


def lead(text: str, pick: str, lang: str) -> str | None:
    """The model's lead as Pisa says it: whole sentences only (never cut mid-sentence), at most LEAD_SENTENCES
    and LEAD_WORDS words, never a list, a table or an address; None when what is left doesn't name the pick
    or isn't in their language (Pisa's own lead is said instead, `own_lead`)."""
    from pisa.domain.tongue import foreign

    kept: list[str] = []
    count = 0
    for sentence in (s.strip() for s in _SENTENCE.findall(" ".join((text or "").split()))):
        if not sentence or _LIST_LINE.match(sentence) or _READ_ALOUD.search(sentence):
            continue
        n = spoken_words(sentence)
        if len(kept) == LEAD_SENTENCES or count + n > LEAD_WORDS:
            break
        kept.append(sentence)
        count += n
    said = " ".join(kept)
    if not said or not pick.strip() or f" {_key(pick)} " not in f" {_key(said)} " or foreign(said, lang):
        return None
    return said


# the pick first, its recognition, their reason; the rest is on the phone
_OWN_LEAD = {
    "it-IT": ("Per te ora {pick}{recognition}{why}.", "Gli altri sono sul telefono.",
              "Il resto è sul telefono."),
    "en-US": ("For you now, {pick}{recognition}{why}.", "The others are on your phone.",
              "The rest is on your phone."),
}  # fmt: skip


def own_lead(found: int, pick: str, reasons: list[str], lang: str, recognition: str = "") -> str:
    """Pisa's own two sentences when the model's lead doesn't fit: the pick, its recognition ("Tre Lune di
    Tavola Chiara"), the reasons that are their (`why_now`'s parts, code's facts), then that the others are on
    the phone; within LEAD_WORDS: a reason that doesn't fit is left out first, then the recognition. Never a
    count ("Ho trovato N posti")."""
    first, others, rest = _OWN_LEAD.get(lang, _OWN_LEAD["en-US"])
    parts = list(reasons)
    named = recognition
    while True:
        why = (": " + ", ".join(parts)) if parts else ""
        recog = f", {named}" if named else ""
        said = f"{first.format(pick=pick, recognition=recog, why=why)} {others if found > 1 else rest}"
        if spoken_words(said) <= LEAD_WORDS or not (parts or named):
            return said
        if parts:
            parts.pop()
        else:
            named = ""


# a lead that opens by counting what Pisa found names no reason
_COUNTS = re.compile(
    r"\b(?:ho\s+trovato|ne\s+ho\s+trovat\w+|i(?:'ve)?\s+found|there\s+are)\s+\d+\b", re.IGNORECASE
)
_MINUTES_SAID = re.compile(r"\b(\d+)\s*(?:minut\w*|min\b)", re.IGNORECASE)
_DISTANCE_SAID = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(km|chilometr\w*|kilomet\w*|m\b|metri|meters|metres)", re.IGNORECASE
)


def counts(text: str) -> bool:
    """The lead counts what Pisa found ("Ho trovato 23 posti", "I found 6 places")."""
    return bool(_COUNTS.search(text or ""))


def same_distance(text: str, minutes: int | None, meters: int | None) -> bool:
    """Every walk or distance figure the lead
    says is the one the card shows (its minutes on foot, its metres, or those metres in km to one decimal)."""
    for m in _MINUTES_SAID.finditer(text or ""):
        if minutes is None or int(m.group(1)) != minutes:
            return False
    for m in _DISTANCE_SAID.finditer(text or ""):
        value = comma_float(m.group(1))
        unit = m.group(2).casefold()
        if meters is None:
            return False
        if unit.startswith(("km", "chilom", "kilom")):
            if round(meters / 1000, 1) != round(value, 1) and round(meters / 1000) != value:
                return False
        elif int(value) != meters:
            return False
    return True


_SPLIT_DECIMAL = re.compile(r"(\d)([,.])\s+(\d{2})(?=\s*(?:€|euro\b|eur\b))", re.IGNORECASE)
_GLUED = re.compile(r"([a-zà-ù0-9][.!?])([A-ZÀ-Ý])")


def tidy(text: str) -> str:
    """A price's decimals never split by a space, and a sentence's full stop
    always followed by one ("pizzerie.Ho" → "pizzerie. Ho")."""
    return _GLUED.sub(r"\1 \2", _SPLIT_DECIMAL.sub(r"\1\2\3", text or ""))


# the little words of a request (where, what, near, tomorrow…), beside `tongue`'s markers
_ASKED_EN = frozenset(
    "where what which how can could should would we i me my near here there somewhere something tomorrow "
    "tonight today with kids children good best quiet place places find recommend rainy dinner lunch "
    "breakfast coffee go eat open now "
    # their small talk ("Hi, how are you?", §32 D2)
    "hi hello hey thanks sorry".split()
)
_ASKED_IT = frozenset(
    "dove cosa quale quali come posso possiamo vorrei voglio qui vicino posto posti qualcosa domani stasera "
    "oggi bambini buon buona migliore tranquillo trova trovami consigli consigliami cena pranzo colazione "
    "caffè caffe andare mangiare aperto adesso "
    # their small talk ("Ciao, come va?", §32 D2)
    "ciao va stai sto bene grazie buongiorno buonasera salve scusa".split()
)


def asked_lang(words: str, lang: str) -> str:
    """The language their typed words are written in (any typed turn: the reply follows it, §32 D2), when
    they read clearly as Italian or English and not as the turn's; else the turn's."""
    from pisa.domain.profile import plain as plain_words
    from pisa.domain.tongue import MARKERS

    found = plain_words((words or "").replace("'", " ")).split()
    it = sum(w in MARKERS["it"] or w in _ASKED_IT for w in found)
    en = sum(w in MARKERS["en"] or w in _ASKED_EN for w in found)
    if lang.startswith("it") and en >= 2 and it == 0:
        return "en-US"
    if lang.startswith("en") and it >= 2 and en == 0:
        return "it-IT"
    return lang


# "Why for you now": code's part of a place's reason, from their situation and this turn's facts only
_NOW = {
    "it-IT": {"walk": "a {m} minuti a piedi", "one": "a 1 minuto a piedi", "open": "aperto {part}",
              "parts": ("stamattina", "oggi pomeriggio", "stasera"),
              "in_time": "in tempo per {title} alle {at}", "tight": "ma alle {at} hai {title}",
              "next": "il tuo prossimo impegno",
              # the minutes their next event leaves them, who is with them, their tastes
              "leaves": "ti lascia {m} minuti prima di {title} alle {at}",
              "leaves_next": "ti lascia {m} minuti prima del tuo prossimo impegno alle {at}",
              "only": "ti lascia solo {m} minuti prima di {title} alle {at}",
              "only_next": "ti lascia solo {m} minuti prima del tuo prossimo impegno alle {at}",
              "with": "anche per {who}", "and": " e ", "likes": "{what}, come piace a te"},
    "en-US": {"walk": "{m} minutes on foot", "one": "1 minute on foot", "open": "open {part}",
              "parts": ("this morning", "this afternoon", "tonight"),
              "in_time": "in time for {title} at {at}", "tight": "but you have {title} at {at}",
              "next": "your next event",
              "leaves": "it leaves you {m} minutes before {title} at {at}",
              "leaves_next": "it leaves you {m} minutes before your next event at {at}",
              "only": "it leaves you only {m} minutes before {title} at {at}",
              "only_next": "it leaves you only {m} minutes before your next event at {at}",
              "with": "for {who} too", "and": " and ", "likes": "{what}, as you like it"},
}  # fmt: skip
EVENT_AHEAD_H = 6  # their next event counts for a pick when it starts within this many hours
EVENT_SLACK_MIN = 45  # "in time for" when this much is left after the walk; else said as tight
# the minutes left are said as a figure up to this many ("ti lascia 20 minuti prima del
# teatro"); past it, "in tempo per X alle 21:00" (a "ti lascia 170 minuti" helps nobody)
LEAVES_MAX = 90


def why_now(
    minutes: int | None,
    open_now: bool | None,
    now: Any,
    event: tuple[str, Any] | None,
    lang: str,
    *,
    company: list[str] | None = None,
    likes: list[str] | None = None,
) -> list[str]:
    """The reasons that are theirs, as short phrases: their next event and what it leaves them, who is with
    them, the minutes on foot (this turn's walk times), what they like that the place has, open in this part
    of their day ("ti lascia 20 minuti prima di Teatro Vela alle 21:00"). Every figure is a tool's or their
    calendar's (the minutes left: their event's start, less the time now and the walk), never a number nobody
    gave; `company` and `likes` only from their own data (their words, their profile)."""
    w = _NOW.get(lang, _NOW["en-US"])
    # their day first, then who, the walk, their taste, the hours: Pisa's own lead drops from the end
    out: list[str] = []
    if event is not None and now is not None:
        title, start = event
        left = (start - now).total_seconds() / 60
        if 0 < left <= EVENT_AHEAD_H * 60:
            at = f"{start:%H:%M}"
            # no title (an invite's, a sensitive one: `_situation`): Pisa's own words for it
            name = " ".join(str(title).split())[:40]
            spare = int(left) - (minutes or 0)
            fits = spare >= EVENT_SLACK_MIN
            if 0 < spare <= LEAVES_MAX:
                kind = ("leaves" if fits else "only") + ("" if name else "_next")
                out.append(w[kind].format(m=spare, title=name, at=at))
            else:
                out.append((w["in_time"] if fits else w["tight"]).format(title=name or w["next"], at=at))
    if who := [n for n in (company or []) if n.strip()][:2]:
        out.append(w["with"].format(who=w["and"].join(who)))
    if minutes is not None:
        out.append(w["walk"].format(m=minutes) if minutes != 1 else w["one"])
    if what := [x for x in (likes or []) if x.strip()][:1]:
        out.append(w["likes"].format(what=" ".join(what[0].split())[:40]))
    if open_now and now is not None:
        part = 0 if now.hour < 12 else 1 if now.hour < 18 else 2
        out.append(w["open"].format(part=w["parts"][part]))
    return out


# a profile statement's object ("Ama i cornetti integrali" → "cornetti integrali"): what a like names
_LIKES = re.compile(
    r"^\s*(?:(?:a\s+lui\s+|gli\s+|mi\s+|he\s+|i\s+)?(?:ama|adora|preferisce|piace|piacciono|apprezza|cerca"
    r"|likes|loves|prefers|enjoys|adores)\s+)?(?:(?:il|lo|la|i|gli|le|un|una|uno|the|a|an)\s+|l['\u2019]\s*)?",
    re.IGNORECASE,
)


def liked(statement: str) -> str:
    """What a like names, without its verb and article ("" when nothing is left)."""
    return " ".join(_LIKES.sub("", statement or "", count=1).strip(" .").split())


# who is with them, from their own words in the conversation ("con Giulia e Marco",
# "with Anna"): a capitalised name right after "con" / "insieme a" / "with", never a place's or a guess
_WITH = re.compile(
    r"\b(?:con|insieme\s+a|with)\s+([A-ZÀ-Ý][\w'\u2019]+(?:\s+(?:e|and|ed)\s+[A-ZÀ-Ý][\w'\u2019]+)?)"
)
_NOT_A_NAME = frozenset({
    "vista", "calma", "tavoli", "wifi", "il", "la", "lo", "le", "gli", "un", "una", "me", "te", "lui", "lei",
    "noi", "voi", "loro", "my", "the", "a", "an", "you", "him", "her", "us", "them", "kids", "friends",
})  # fmt: skip


def company(words: str) -> list[str]:
    """The people their words say are with them ("Cena con Giulia e Marco": ["Giulia", "Marco"]), at most
    two."""
    out: list[str] = []
    for found in _WITH.findall(words or ""):
        for name in re.split(r"\s+(?:e|and|ed)\s+", found):
            if name.casefold() not in _NOT_A_NAME and name not in out:
                out.append(name)
    return out[:2]


# a guide is a guide. A page a place publishes itself (its menu, its counter, its
# site) never counts as the guides consulted, nor as "cited by"
_OWN_PATH = re.compile(
    r"/(?:menu|menù|carta|listino|prezzi|banco|price[s-]?\w*|our-menu)(?:/|\.|$)", re.IGNORECASE
)


def own_page(url: str, websites: list[str], names: list[str]) -> bool:
    """The page is a place's own (its website's host, a host named after a place this turn found, or a menu
    path), never a guide, an award or the press."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return False
    sites = {(urlsplit(w if "//" in w else f"//{w}").hostname or "").lower().removeprefix("www.")
             for w in websites if w}  # fmt: skip
    if host in sites:
        return True
    label = re.sub(r"[^a-z0-9]", "", host.rsplit(".", 1)[0].split(".")[-1])
    for name in names:
        squashed = re.sub(r"[^a-z0-9]", "", _key(name))
        if len(squashed) >= 5 and squashed in label:
            return True
    return bool(_OWN_PATH.search(parts.path or ""))


# every spoken answer of
# a research conversation (the turn that shows the sheet and each follow-up about it) is two sentences and
# at most ANSWER_WORDS words, Pisa's interim line included; the rest goes on the screen, never read aloud
ANSWER_WORDS = 35
_TRIM = " ,;:\u2014\u2013"
# a clause ends at , ; : or a dash followed by white space (or the end): never inside a decimal, a
# price or a clock time ("4,5 stelle", "8,50 euro", "alle 19:30" stay whole)
_CLAUSE = re.compile(r"(?:[,;:\u2014\u2013](?=\S)|[^,;:\u2014\u2013])+(?:[,;:\u2014\u2013]+(?=\s|$))?\s*")
# a word a cut never ends on: a figure, an initial ("S."), a little word ("alle", "a", "di")
_DANGLING = re.compile(r"^(?:\W*\d[\d.,:]*\W*|[A-ZÀ-Ý]\.|\w{1,3})$")


# a web address in brackets is an aside
# the screen has (the turn's sources), so the sentence is said without it; one outside brackets keeps the
# sentence for the screen, a bare "host.example/path" too
_ADDRESS = r"(?:https?://|www\.|\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}/)[^\s()]*[^\s().,;:!?]"
_BRACKETED = re.compile(r"\s*\(([^()]*)\)")
_IN_BRACKETS = re.compile(rf"(?:,\s*|\s+|^){_ADDRESS}")
_BARE_ADDRESS = re.compile(_ADDRESS)


def unaddressed(sentence: str) -> str:
    """The sentence with the web addresses in its brackets taken out (and the brackets they leave empty)."""

    def aside(m: re.Match[str]) -> str:
        inner = _IN_BRACKETS.sub("", m.group(1)).strip(" ,;:")
        return f" ({inner})" if inner else ""

    return " ".join(_BRACKETED.sub(aside, sentence or "").split())


def brief(
    sentence: str,
    said: list[str],
    working: frozenset[str] | set[str] = frozenset(),
    *,
    word_cap: int = ANSWER_WORDS,
    sentence_cap: int = LEAD_SENTENCES,
) -> tuple[str, str]:
    """(what Pisa says of this sentence, what goes on the screen instead), given what Pisa said this turn
    (`said`, its interim line among them, which counts in words and never as a sentence). Whole sentences;
    only a first sentence that alone is too long is cut, at a clause (its tail goes on the screen). Sentences
    are counted on the words said (the stream cuts after "Caffè S." too: an initial never ends one,
    and the piece that follows it finishes that sentence). `word_cap`, `sentence_cap`: the caps (a standing
    "rispondimi breve" sets its own, `memoria.style`)."""
    text = unaddressed(sentence)
    if not text:
        return "", ""
    own_text = " ".join(s.strip() for s in said if s.strip() and s.strip() not in working)
    sentences = [m for m in WHOLE_SENTENCE.findall(own_text) if m.strip()]
    whole = [m for m in WHOLE_SENTENCE.findall(f"{own_text} {text}".strip()) if m.strip()]
    going_on = bool(sentences) and len(whole) <= len(sentences)  # this piece ends a sentence begun
    count = len(sentences) - (1 if going_on else 0)
    used = sum(spoken_words(s) for s in said if s.strip())
    room = word_cap - used
    if count >= sentence_cap or room <= 0 or _LIST_LINE.match(text) or _READ_ALOUD.search(text):
        return "", text
    if _BARE_ADDRESS.search(text):  # a bare "host.example/path" is the screen's too
        return "", text
    if spoken_words(text) <= room:
        return text, ""
    if sentences and not going_on:  # the essential was said: the rest is on the screen
        return "", text
    end_at = 0
    for clause in _CLAUSE.finditer(text):
        if not clause.group().strip():
            continue
        if spoken_words(text[: clause.end()]) > room:
            break
        end_at = clause.end()
    if end_at:
        return text[:end_at].rstrip(_TRIM) + ".", text[end_at:].strip(_TRIM)
    # a single clause too long: its first words (never silence, never ending on a figure or an initial)
    words = text.split()[: max(1, room)]
    while len(words) > 1 and (spoken_words(" ".join(words)) > room or _DANGLING.match(words[-1])):
        words.pop()
    kept = " ".join(words)
    return kept.rstrip(_TRIM) + "…", text[len(kept) :].strip(_TRIM)


def _carries(sentence: str, results: set[str]) -> int:
    """How many of the sentence's words (four letters or more) the turn's tool results hold: what it says
    of what the tools returned (a task's title), not Pisa's own framing."""
    return sum(1 for w in set(fold_words(sentence).split()) if len(w) >= 4 and w in results)


def answer_first(
    sentences: list[str],
    results: str,
    said: list[str],
    working: frozenset[str] | set[str] = frozenset(),
    *,
    word_cap: int = ANSWER_WORDS,
    sentence_cap: int = LEAD_SENTENCES,
) -> tuple[list[str], list[str]]:
    """(the round's sentences to say, in their order; the rest), within the caps, given what Pisa said this
    turn (`said`). A cut to the cap keeps the answer (quality audit §23 m1: "Te la metto nel piano? Oggi non
    hai eventi.", the three tasks cut): questions and offers go first, then the sentences that carry least
    of the turn's tool results (`results`: what the tools returned), the latest first; a question stays only
    after the answer, when it fits."""
    if not sentences:
        return [], []
    own = [s for s in said if s.strip() and s.strip() not in working]
    room_words = word_cap - sum(spoken_words(s) for s in said if s.strip())
    room_sentences = sentence_cap - len([m for m in WHOLE_SENTENCE.findall(" ".join(own)) if m.strip()])
    held = set(fold_words(results).split())
    asked = [n for n, s in enumerate(sentences) if s.rstrip().endswith("?")]
    keep = [n for n in range(len(sentences)) if n not in asked]

    def fits(chosen: list[int]) -> bool:
        return sum(spoken_words(sentences[n]) for n in chosen) <= room_words and len(chosen) <= room_sentences

    while len(keep) > 1 and not fits(keep):
        # the sentence that carries least goes, the latest of those first
        least = min(keep, key=lambda n: (_carries(sentences[n], held), -n))
        keep.remove(least)
    if keep:  # a question after the answer, when it still fits
        for n in asked:
            if n > min(keep) and fits([*keep, n]):
                keep.append(n)
    elif asked:  # only questions: the first one, as the model wrote it
        keep = [asked[0]]
    keep.sort()
    return [sentences[n] for n in keep], [s for n, s in enumerate(sentences) if n not in keep]
