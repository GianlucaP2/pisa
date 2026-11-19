"""Pisa talks like a person: the conversation rules that are code, not prompt. Pure rules, no I/O.

Each rule's number is in brackets.
- **"Eh?"** [rule 12]: a bare "eh?", "cosa?", "come?", "what?", "sorry?" asks for the last thing Pisa said
  again; Pisa says it again, shorter (its question when it asked one, the key item first, ≤ 12 words), with no
  model call.
- **Polish** [rules 24, 25]: a reply never opens with "Certamente!" or "Certo!" alone, never closes with
  "C'è altro?" / "Anything else?", and greets ("Ciao!", "Buongiorno!") only in the day's first exchange.
  The sentences are dropped as they stream, before the voice.
- **Urgency** [rules 29, 30]: Pisa speaking first is `now` only for what can't wait (leave now, an event about
  to start, a safety alert: "Scusa, un attimo:" first); the rest waits for a breakpoint.
- **Register** [rule 24]: Italian "tu", never "Lei": `formal_it` finds the formal forms (a test runs it on
  every Italian sentence the code itself says).
"""

import re
from collections.abc import Iterable
from typing import Literal

from pisa.domain.text import fold_apostrophe

Urgency = Literal["now", "breakpoint"]

_HUH = frozenset(
    {"eh", "ehm", "cosa", "come", "scusa", "prego", "cos'hai detto", "che hai detto", "non ho capito",
     "what", "sorry", "pardon", "huh", "come again", "say again", "what did you say", "ripeti"}
)  # fmt: skip
_FILLER_OPEN = re.compile(
    r"^\s*(certamente|certo|assolutamente|ottima domanda|perfetto|of course|certainly|absolutely|"
    r"great question|sure thing)\s*[!.,]?\s*",
    re.IGNORECASE,
)
_GREETING = re.compile(
    r"^\s*(ciao|buongiorno|buonasera|salve|hey|hi|hello|good (morning|afternoon|evening))\b"
    r"[^.!?]{0,20}[!.,]?\s*",
    re.IGNORECASE,
)
_ANYTHING_ELSE = re.compile(
    r"(c'è altro|c'e altro|posso fare altro|posso aiutarti (in|con) (altro|qualcos'altro)|serve altro|"
    r"ti serve (altro|qualcos'altro)|anything else|can i help (you )?with anything else|"
    r"is there anything else)"
    r"[^.!?]*\?\s*$",
    re.IGNORECASE,
)
_FORMAL_IT = re.compile(
    r"\b(Lei|La prego|Le (posso|serve|interessa|consiglio|ricordo)|[Mm]i dica|[Ss]i accomodi|"
    r"[Gg]entile cliente)\b"
    r"|(?:^|[.!?]\s+)(Desidera|Vuole|Preferisce|Gradisce)\b"
)
_SENTENCE = re.compile(r"[^.!?…]+[.!?…]*\s*")
# a decimal or a clock time ("4.5", "alle 7.30") and an
# initial ("Caffè S. Eustachio", "F.lli") never end a sentence: the lead's splitter and `split_sentences`
# a
# full stop inside a word or followed by a comma ("p.m.,", "e.g.", "sito.example") ends no sentence either
WHOLE_SENTENCE = re.compile(r"(?:\d[.,]\d|\b[A-ZÀ-Ý]\.(?=\s*\w)|\.(?=\w|[,;:])|[^.!?…])+[.!?…]*\s*")


def names(words: str, title: str) -> bool:
    """FACT: the user's words name this place or thing: one of its words of 4+ letters, whole ("com'è il
    Bistrot dell'Ansa?" names «Bistrot dell'Ansa»; "un posto per pranzo" names no place)."""
    said = set(re.findall(r"\w+", fold_apostrophe(words)))
    return any(len(w) >= 4 and w in said for w in re.findall(r"\w+", fold_apostrophe(title)))


# words the user leaves for the architect, never for anyone else ("Questa è per l'architetto"
# made a WhatsApp message): stored as feedback, said back as noted. Only an utterance
# that opens as a note (after an optional "Pisa," or "ok"), with no request to Pisa in it and no name after
# "architetto": "dillo all'architetto Marco", "compra un regalo per l'architetto" are requests.
_NOTE_OPEN = re.compile(
    r"^(?:(?:pisa|ok|okay)\b[\s,.:;!]*)*"
    r"(?:(?:nota|questa e|questo e) per l'?\s?(?:architett[oa]|operatore)"
    r"|per l'?\s?(?:architett[oa]|operatore)\s*:"
    r"|(?:nota )?per i log\b|(?:this is |note )?for the (?:architect|operator)|(?:note )?for the logs?\b)"
)
_TO_PISA = re.compile(
    r"\b(?:scrivi\w*|manda\w*|di'|dill[oae]|digli|porta\w*|compra\w*|chiama\w*|cerca\w*|send|tell|buy|call)"
    r"(?=[\s,.:;!?]|$)"
)
_NAMED = re.compile(r"\b(?:architett[oa]|operatore|architect|operator)\s+[A-ZÀ-Ý]")
# Said back to the user: the note goes to the server's operator (the words "architect" are still understood).
NOTED_FOR_ARCHITECT = {"it-IT": "Segnato per l'operatore.", "en-US": "Noted for the server's operator."}


def for_architect(words: str) -> bool:
    """The user's words are a note for the server's operator (or the logs), not a request to Pisa."""
    said = fold_apostrophe(words).strip()
    return bool(_NOTE_OPEN.match(said)) and not _TO_PISA.search(said) and not _NAMED.search(words)


# their words ask Pisa to pass something on to Pisa's team (the architect, the developers): "segnalalo al
# team", "dillo agli sviluppatori", "proponilo al team di sviluppo", "tell your developers".
# the ways they really ask, in Italian
# and English: a suggestion, segnala, dillo, fai sapere, manda, passa, to the architect / architetto / team /
# sviluppatori. Never a message for a person ("dillo a Marco"), their own people ("il mio architetto", "my
# team"), a work team ("il team marketing"), nor something to send them ("manda la mail al team").
# things sent to someone, never Pisa's team's note; and a real (house) architect's papers
_SENT = (
    r"(?:e-?mail|mail|messaggi\w*|message|whatsapp|sms|invit\w*|file|document\w*|allegat\w*|foto|pdf|report|"
    r"preventiv\w*|bozz\w*|piantin\w*|planimetri\w*|disegn\w*|contratt\w*|fattur\w*|misure|"
    r"quote|invoice|draft|plans?|drawings?)"
)
# what may follow "team" when it is Pisa's: never a word naming another team ("il team marketing", "di
# calcio")
_AFTER_TEAM = (
    r"che|di\s+sviluppo|di\s+pisa|tecnico|dev|di\s+\w+(?:are|ere|ire)|questo|questa|questi|queste|quello|quella|"
    r"per|su|sul|sulla|se|e|anche|pure|subito|the|that|this|these|it|about|of\s+pisa|if|and|too|please|now|"
    r"il|lo|la|i|gli|le|un|una|uno|l'|un'"
)
_ASKS_TEAM = re.compile(
    r"\b(?P<verb>segnal\w*|propon\w*|rifer\w*|dill[oae]|digli|dicci|dir(?:e|lo|la|gli|glielo)|scriv\w*|"
    r"fa(?:i|r|gli|llo|lle|glielo)?\s+sapere|comunic\w*|pass\w*|gir\w*|riport\w*|mand\w*|invi\w*|inoltr\w*|"
    r"sugger\w*|consigli\w*|feedback|tell|report|flag|pass|let|send|forward|suggest\w*|mention|share|give)\b"
    rf"(?P<gap>(?:(?!\b{_SENT}\b)[^.!?]){{0,60}}?)\b(?:(?:a[il]?|ai|agli|all'|alla|to|know)\s*)?"
    r"(?:the\s+|your\s+|tuo\s+|tuoi\s+|il\s+tuo\s+|i\s+tuoi\s+)?"
    rf"(?:team(?=\s*$|\s*[,;:!]|\s+(?:{_AFTER_TEAM})\b)|sviluppator\w*|developers?|devs?\b|architett[oa]|"
    r"architect|chi\s+ti\s+(?:sviluppa|ha\s+fatto|programma)|makers)\b(?P<after>[^.!?]*)"
)
# what manda / invia / inoltra / send / forward / passa may carry to Pisa's team: their idea, the
# problem, a suggestion, this; never a thing for people ("gli auguri", "the invite", "il link")
_SENDING = re.compile(r"(?:mand|invi|inoltr|pass|send|forward)\w*")
_CARRIED = frozenset(
    "questo questa quello quella questa cosa idea idee suggerimento suggerimenti problema problemi bug "
    "feedback segnalazione nota note consiglio proposta errore lo la li le l il i un una uno gli the a an "
    "this that it these my mio mia this idea suggestion suggestions problem problems note notes issue "
    "error proposal on pure anche subito please crash log richiesta request commento comment osservazione "
    "hint".split()
)
_ARTICLES = frozenset("il lo la i gli le un una uno l the a an this these questo questa".split())
# a person's message, or their own people: never Pisa's team. A name in lower case too
# ("manda a nino il link per il team"), and English with no "to" ("send Giulia the notes for the team")
_PERSON = re.compile(
    r"\b(?:a|ad|to)\s+(?!Pisa\b)[A-ZÀ-Ý][\wà-ý]*|\b(?:tell|send|give|show|pass|forward|let)\s+(?!Pisa\b)[A-ZÀ-Ý]"
    r"|\bteam\s+di\s+[A-ZÀ-Ý]"
)
_PERSON_LOWER = re.compile(
    r"\b(?:a|ad)\s+(?!(?:il|lo|la|i|gli|le|un|una|uno|tuo|tuoi|tua|tue|chi|pisa|tutti)\b)[a-z]+\s+"
    r"(?:per|che|il|la|lo|i|gli|le|del|della|e)\b"
)
_HIS_OWN = re.compile(
    r"\b(?:mio|mia|miei|mie|nostro|nostra|nostri|nostre|my|our)\s+(?:team|architett\w*|architect\w*|"
    r"sviluppator\w*|developers?|devs?)\b|\b(?:a|ad|to)\s+(?!the\b|your\b|il\b|tuo\b|tuoi\b|pisa\b)\w+\s+"
    r"(?:del|dal|of|from)\s+(?:the\s+|tuo\s+)?team\b"
)


def _carries_a_thing(match: re.Match[str]) -> bool:
    """A sending verb's object, before the team or right after it ("forward the team the invite"), is a
    thing for people, not their note."""
    if not _SENDING.fullmatch(match["verb"]):
        return False
    before = re.findall(r"\w+", match["gap"])  # "l'acconto" is an article and its noun
    after = re.findall(r"\w+", match["after"])
    if any(w not in _CARRIED for w in before):
        return True
    if after and after[0] in _ARTICLES:  # a second object after the team: its noun says what it is
        noun = next((w for w in after[1:] if w not in _ARTICLES), "")
        return noun not in _CARRIED
    return False


def asks_team(words: str) -> bool:
    """Their words ask Pisa to tell its team something (never a question about whether Pisa can)."""
    # "dì al team", "di' all'architetto": the imperative's accent and apostrophe never survive the fold
    said = fold_apostrophe(re.sub(r"(?i)\b(?:dì|di')(?=\s)", "dillo", words.replace("\u2019", "'")))
    found = _ASKS_TEAM.search(said)
    return (
        found is not None
        and not said.rstrip().endswith("?")
        and not _carries_a_thing(found)
        and not _PERSON.search(words)
        and not _PERSON_LOWER.search(found["gap"])  # before the team, never in their message
        and not _NAMED.search(words)
        and not _HIS_OWN.search(said)
    )


_POINTS_BACK = re.compile(
    r"\b(?:segnal|propon|rifer|dil|dic|digl|scriv|comunic|pass|gir|riport)\w*(?:lo|la|li|le)\b|"
    r"\b(?:questo|questa|quello|quella)\b|\b(?:tell|report|flag|pass)\b.*\b(?:it|this|that)\b"
)


def points_back(words: str) -> bool:
    """Their words point back to what they said before ("segnalalo", "dillo", "questo")."""
    return bool(_POINTS_BACK.search(fold_apostrophe(words)))


TEAM_NOTED = {
    "it-IT": "Segnato per il team: lo leggono nel prossimo rapporto.",
    "en-US": "Noted for the team: they read it in the next report.",
}


def is_huh(words: str) -> bool:
    """The user didn't catch Pisa's last words: "eh?", "cosa?", "come scusa?", "sorry?" (the whole
    message)."""
    plain = re.sub(r"[^\w' ]", " ", fold_apostrophe(words))
    plain = " ".join(w for w in plain.split() if w not in ("pisa", "scusa", "sorry", "ma")) or plain.strip()
    return plain in _HUH or plain.replace(" ", "") in {"eh", "eeh", "eh eh"}


AGAIN_MAX_WORDS = 12


def again(reply: str) -> str:
    """Pisa's last reply said again, shorter (rule 12): its question when it asked one (the user must answer
    it), else its first sentence (answers come first), without a greeting or a filler opening; over 12 words,
    its first clause."""
    sentences = [s.strip() for s in _SENTENCE.findall(reply) if s.strip()]
    sentences = [s for s in sentences if _FILLER_OPEN.sub("", _GREETING.sub("", s)).strip()] or sentences
    if not sentences:
        return reply.strip()
    asked = [s for s in sentences if s.endswith("?")]
    chosen = asked[-1] if asked else sentences[0]
    chosen = _FILLER_OPEN.sub("", _GREETING.sub("", chosen)).strip() or chosen
    if word_count(chosen) <= AGAIN_MAX_WORDS:
        return chosen
    clauses = [c.strip() for c in re.split("[,;:\u2014\u2013]", chosen) if c.strip()]
    key = clauses[0]  # answer first (rule 17): the first clause carries the answer
    end = chosen[-1] if chosen[-1] in ".?!" else "."
    return key.rstrip(".?!") + end


# their bare thanks or greeting ("grazie Pisa", "buongiorno Pisa") asks
# nothing of Pisa, so Pisa's answer asks nothing of them (the day's question, a hunch, a consent stay code's
# own)
_COURTESY = frozenset(
    "grazie mille tante ciao buongiorno buonasera buonanotte salve thanks thank you a lot "
    "good morning evening night hi hello hey ehi pisa".split()
)


def courtesy(text: str) -> bool:
    """Their words are only a thanks or a greeting ("grazie Pisa", "buongiorno Pisa", "thanks"): nothing
    asked."""
    words = re.findall(r"[\w']+", (text or "").casefold())
    return bool(words) and not text.rstrip().endswith("?") and all(w in _COURTESY for w in words)


def thanks(text: str) -> bool:
    """Their words thank Pisa ("grazie", "thanks", "thank you")."""
    return bool(re.search(r"\b(?:grazie|thanks|thank)\b", (text or "").casefold()))


def keep(sentence: str, *, first: bool, greet: bool) -> str:
    """A streamed sentence as Pisa says it (rules 26-28): a filler opening goes; a greeting goes unless it is
    the day's first exchange (`greet`); a closing "C'è altro?" goes. `first`: the reply's first sentence. ""
    when nothing is left."""
    out = sentence
    if _FILLER_OPEN.sub("", out).strip() == "" and out.strip():
        return ""  # a sentence that is only "Certamente!" or "Perfetto." says nothing
    if first:
        if not greet:
            out = _GREETING.sub("", out, count=1)
        out = _FILLER_OPEN.sub("", out, count=1)
        if out and out[0].islower() and out != sentence:
            out = out[0].upper() + out[1:]
    if _ANYTHING_ELSE.search(out) and len(_SENTENCE.findall(out.strip())) <= 1:
        return ""
    out = _ANYTHING_ELSE.sub("", out)
    return out if out.strip() else ""


def formal_it(text: str) -> list[str]:
    """The formal Italian ("Lei", "La prego", "desidera…") in a sentence: Pisa says "tu" (rule 24)."""
    return [m.group(0).strip(" .!?") for m in _FORMAL_IT.finditer(text)]


def word_count(text: str) -> int:
    return len(re.findall("[\\w'\u2019]+", text))


# --- proactive timing (rules 29-30) ---------------------------------------------------------------------

NOW_KINDS = frozenset({"leave_now", "event_soon", "alert"})
NOW_PREFIX = {"it-IT": "Scusa, un attimo: ", "en-US": "Sorry, one moment: "}
_DEFER = frozenset({"dopo", "più tardi", "piu tardi", "non ora", "later", "not now", "after"})


# Whole words ("classico" is no class), each with its plural or gender endings.
_MEETING = re.compile(
    r"meetings?|riunion[ei]|calls?|colloqui[oa]?|interviews?|lezion[ei]|lessons?|class(es)?|visit[ae]|"
    r"appuntament[oi]|appointments?|dentist[ae]?|medic[oi]|doctors?|esam[ei]|exams?|teatro|cinema|"
    r"concert[oi]?|concerts|spettacol[oi]|conferenz[ae]|conferences?|webinars?|presentazion[ei]"
)


# RAIL: the floor: leave-now and an event within 10 minutes are always `now`.
def urgency(kind: str, priority: str = "normal", minutes_left: float | None = None) -> Urgency:
    """`now` only for what can't wait: leaving now, an event within 10 minutes, an urgent message or an
    alert. Everything else waits for a breakpoint."""
    if kind in NOW_KINDS or priority == "urgent":
        return "now"
    if kind == "reminder" and minutes_left is not None and minutes_left <= 10:
        return "now"
    return "breakpoint"


# RAIL: never speak unprompted in a meeting; the words are a floor, not the model's judgement.
def is_meeting(title: str) -> bool:
    """An event where Pisa doesn't speak up unasked (a meeting, a call, a visit, a show)."""
    return any(_MEETING.fullmatch(w) for w in re.findall(r"[a-z]+", fold_apostrophe(title)))


# RAIL: Pisa speaks unasked by voice at most this often (the user's local day); past it the item goes to
# the phone silently (a banner or the notice), never dropped. The time-critical floors always go by voice.
VOICE_PER_HOUR = 3
VOICE_PER_DAY = 12


def is_floor(kind: str, urgency: str | None) -> bool:
    """A time-critical item: leaving now, or an event about to start (a `now` reminder)."""
    return kind == "reminder" and urgency == "now"


def voice_allowed(floor: bool, last_hour: int, today: int) -> bool:
    """May this unprompted item be spoken, given how many were spoken (floors aside)?"""
    return floor or (last_hour < VOICE_PER_HOUR and today < VOICE_PER_DAY)


def interrupting(text: str, lang: str) -> str:
    """A `now` item opens with "Scusa, un attimo:" (rule 29): the user hears why Pisa cuts in."""
    prefix = NOW_PREFIX.get(lang, NOW_PREFIX["en-US"])
    return text if text.startswith(prefix) else prefix + text


def defers(words: str) -> bool:
    """ "Dopo", "non ora", "later": the user puts Pisa's unprompted item off, once (rule 30)."""
    plain = re.sub(r"[^\w' ]", " ", fold_apostrophe(words)).strip()
    return plain in _DEFER or plain.replace(" dai", "") in _DEFER


LATER = {"it-IT": "Ok, dopo.", "en-US": "OK, later."}  # the user's "dopo" to what Pisa said unasked


# --- the conversation eval: the rules a reply is held to, numbers only ------------

_MOVED_TO_9 = re.compile(r"(spostat\w*|ho spostato|sposto|moved?)\b.{0,40}\b(9|9:00|nove)\b", re.IGNORECASE)
_DINNER = re.compile(r"\b(cena|dinner|paola)\b", re.IGNORECASE)
# a change of mind pointed out, or the old things kept anyway (the "fresh start")
_CONTRADICTS = re.compile(
    r"\b(hai detto|avevi detto|mi avevi chiesto|prima volevi|you said|you asked me|le tengo comunque|"
    r"sei sicuro|are you sure)\b",
    re.IGNORECASE,
)

# the `no_label` check. A reply never attributes to the user an emotional state
# their own words did not contain, with or without a "?"; a reply that names an observable of how they speak
# (pace, loudness, a whisper, a noisy place) must ask, so they can wave it away.
_LABELS = (
    r"stressat\w*|ansios\w*|arrabbiat\w*|trist[ei]\w*|frustrat\w*|nervos\w*|preoccupat\w*|agitat\w*|"
    r"stanc\w*|esaust\w*|tes[oaie]\b|di cattivo umore|giù(?=\s*(?:[.,;:!?]|$|di morale))|stressed|anxious|"
    r"angry|upset|sad|frustrated|nervous|worried|tired|annoyed|exhausted|overwhelmed|in a bad mood|"
    r"down(?=\s*(?:[.,;:!?]|$))|depressed|low(?=\s*(?:[.,;:!?]|$))"
)
_LABEL_SAID = re.compile(
    r"\b(?:sei|sembri|mi sembri|ti sento|ti vedo|ti senti|hai l'aria|you're|you are|are you|you seem|"
    r"you sound|you look|you feel|do you feel|feeling)"
    rf"\s+(?:[\w']+\s+){{0,2}}?({_LABELS})",
    re.IGNORECASE,
)
# "Hai una voce stanca", "your voice sounds tired": a label on their voice is one on them
_LABEL_VOICE = re.compile(
    rf"\b(?:hai (?:una|la) voce|your voice (?:sounds|is))\s+(?:[\w']+\s+){{0,1}}?({_LABELS})", re.IGNORECASE
)
# "Stressato? …", "Stanco morto: …": a lone label opening the reply is said to them, with no verb; never
# "Stanchi i ragazzi?", "Upset stomach?", "Tired of waiting?"
_LABEL_OPENS = re.compile(rf"^\W*({_LABELS})(?:\s+mort[oaie])?\s*(?:[,.:;!?…—-]|$)", re.IGNORECASE)
# Their own words that say it of themselves, first person (never "Laura è triste", never "non
# sono preoccupato"): "sono stanco morto", "mi sento giù", "I'm exhausted", or a lone label opening their
# words
_HIS_LABEL = re.compile(
    r"\b(?:sono|son|mi sento|sto|i'm|i am|i feel|i've been|feeling)\s+((?:[\w']+\s+){0,2}?)"
    rf"({_LABELS})",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"\b(?:non|not|never|mai|nient'affatto|per niente)\b|n't\b", re.IGNORECASE)
_OBSERVABLE = re.compile(
    r"\b(?:parli (?:veloce|velocemente|in fretta|piano|forte|sottovoce|a bassa voce)|sussurri|"
    r"ti sento (?:di corsa|affannat\w*|senza fiato|lontano)|c'è (?:molto )?rumore|sei in un posto rumoroso|"
    r"you(?:'re| are) (?:talking|speaking) (?:fast|quickly|quietly|softly|loudly)|you(?:'re| are) whispering|"
    r"you sound (?:rushed|out of breath|far away)|it's (?:very )?noisy)\b",
    re.IGNORECASE,
)


def _stem(label: str) -> str:
    word = " ".join(label.lower().split())
    if " " in word or word in ("giù", "sad"):
        return word
    return word[:-1] if len(word) > 3 else word


def _his_labels(owner_words: str) -> set[str]:
    """The feelings their own words say of themselves, as stems: first person and not negated."""
    found = set()
    for m in _HIS_LABEL.finditer(owner_words):
        before = owner_words[max(0, m.start() - 12) : m.start()]
        if not _NEGATED.search(before) and not _NEGATED.search(m.group(1)):
            found.add(_stem(m.group(2)))
    if opening := _LABEL_OPENS.match(owner_words):
        found.add(_stem(opening.group(1)))
    return found


# Pisa reporting a message's words is not Pisa labelling them, but only when
# the sentence names who said them: one of `senders` (the outside items Pisa read this turn: from the tool
# output,
# never from the reply) is the subject right before a reporting verb ("Paolo ti chiede se sei stressato"), or
# a
# quote follows that attribution ("Paolo Neri, oggi: «Sei stressato?»"). With no senders (every live rail:
# voice2's questions and opinions, attend's words, the night's lines) nothing is exempt. A quote alone, a
# voice
# or a tone that "says" it, an unnamed person: all Pisa's own.
_QUOTE_CHARS = '«»“”"'
_QUOTES = re.compile(r"«[^»]*»|“[^”]*”|\"[^\"]*\"")
_REPORT_VERB = (
    r"(?:(?:ti|gli|le|mi|you)\s+)?(?:chiede|domanda|scrive|dice|ha\s+chiesto|ha\s+domandato|ha\s+scritto|"
    r"ha\s+detto|vuole\s+sapere|voleva\s+sapere|si\s+chiede|si\s+domanda|asks|asked|wants\s+to\s+know|"
    r"wanted\s+to\s+know|writes|wrote|says|said|wonders|wondered)\b"
)
_AFTER_SENDER = re.compile(rf"\s*,?\s*{_REPORT_VERB}", re.IGNORECASE)
# between the sender and the quote: at most two words of time or place ("oggi", "nella mail"), a verb, a colon
_TO_QUOTE = re.compile(
    rf"\s*(?:,\s*[\w']+(?:\s+[\w']+)?(?:\s+\d{{1,2}}[:.]\d{{2}})?\s*)?(?:{_REPORT_VERB})?\s*[:,]?\s*",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?…]+(?:\s|$)")


def _quoted_spans(reply: str) -> list[tuple[int, int]]:
    return [m.span() for m in _QUOTES.finditer(reply)]


def _sentence_start(reply: str, at: int, quotes: list[tuple[int, int]]) -> int:
    """Where the sentence holding `at` begins (a "?" inside a quote ends no sentence)."""
    start = 0
    for m in _SENTENCE_END.finditer(reply, 0, at):
        if not any(a <= m.start() < b for a, b in quotes):
            start = m.end()
    return start


def _sender_names(senders: Iterable[str]) -> list[str]:
    """Each sender's full name, and its first name (3 letters or more)."""
    out: list[str] = []
    for sender in senders:
        name = " ".join(str(sender).split())
        if len(name) >= 3:
            out.append(name)
            first = name.split()[0]
            if first != name and len(first) >= 3:
                out.append(first)
    return out


def reported(reply: str, at: int, senders: Iterable[str] = ()) -> bool:
    """The words at `at` are a named sender's, not Pisa's: in the same sentence, one of `senders` right before
    a reporting verb that comes before them, or a quote holding them that follows such an attribution."""
    names = _sender_names(senders)
    if not names:
        return False
    quotes = _quoted_spans(reply)
    start = _sentence_start(reply, at, quotes)
    quote = next(((a, b) for a, b in quotes if a <= at < b), None)
    for name in names:
        for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", reply[start:at], re.IGNORECASE):
            end = start + m.end()
            if _AFTER_SENDER.match(reply, end) is not None:
                return True
            if quote is not None and quote[0] >= end and _TO_QUOTE.fullmatch(reply, end, quote[0]):
                return True
    return False


def no_label(reply: str, owner_words: str = "", senders: Iterable[str] = ()) -> list[str]:
    """`no_label`: "label" when the reply says how they feel as a fact (or as a guess about them) and
    their own words did not say it of themselves ("Sei stressato, ti sposto la cena." fails; "Stanco morto: …"
    passes after their "sono stanco morto"); "observable without a question" when it names how they speak and
    does not ask ("Parli veloce."). Quote marks hide nothing (`Sei "stressato".` is a label). A label Pisa
    reports
    from a message is exempt only when the sentence names its sender, one of `senders`."""
    his = _his_labels(owner_words)
    bare = "".join(" " if c in _QUOTE_CHARS else c for c in reply)  # same positions, no quote marks
    said = (*_LABEL_SAID.finditer(bare), *_LABEL_VOICE.finditer(bare), *_LABEL_OPENS.finditer(bare))
    said = tuple(m for m in said if not reported(reply, m.start(1), senders))
    out = []
    if any(_stem(m.group(1)) not in his for m in said):
        out.append("label")
    if _OBSERVABLE.search(reply) and "?" not in reply:
        out.append("observable without a question")
    return out


def violations(
    reply: str,
    lang: str,
    *,
    asked_long: bool = False,
    after_yes: bool = False,
    check: str | None = None,
    owner_words: str = "",
) -> list[str]:
    """The rules a reply breaks (rule names only, no judgement): tu, no filler, at most 25 words unless asked
    for more, at most 3 options, no "C'è altro?", no question after a yes; `check` adds a case's own rule:
    "newest" (a "sì" answers the check-in, never the older question), "train" (the time by the move verb,
    never the train's) or "fresh" (a change of mind is a fresh start: never pointed out or second-guessed).
    Every reply is held to `no_label` against the user's own words."""
    out = no_label(reply, owner_words)
    if lang == "it-IT" and formal_it(reply):
        out.append("formal")
    if "Certamente!" in reply or "Certainly!" in reply:
        out.append("filler")
    if reply.rstrip().endswith(("C'è altro?", "Anything else?", "altro?")):
        out.append("anything else")
    if not asked_long and word_count(reply) > 25:
        out.append("over 25 words")
    if sum(reply.count(f"{n})") for n in range(1, 10)) > 3:
        out.append("over 3 options")
    if after_yes and "?" in reply:
        out.append("a question after a yes")
    if check == "newest" and _DINNER.search(reply):
        out.append("answered the older question")
    if check == "train" and _MOVED_TO_9.search(reply):
        out.append("moved to the train's time")
    if check == "fresh" and _CONTRADICTS.search(reply):
        out.append("pointed out a change of mind")
    return out


# --- the body -----------------------------------------------------------------------------------------------

Route = Literal["speaker", "receiver", "airpods", "headphones", "car", "glasses"]
ROUTES: frozenset[str] = frozenset({"speaker", "receiver", "airpods", "headphones", "car", "glasses"})
# where the user hears Pisa without looking at the phone: Pisa speaks one sentence, the rest on screen
SHORT_ROUTES = frozenset({"airpods", "glasses", "car"})
# the routes where Pisa may ask to open a talk for its own line (the user wears it, or drives)
SPEAK_FIRST_ROUTES = frozenset({"airpods", "glasses", "car"})
Speak = Literal["leave_now", "reply_waiting", "approval_lapsing", "check_in"]
SPEAKS: frozenset[str] = frozenset({"leave_now", "reply_waiting", "approval_lapsing", "check_in"})
ASKS_PER_HOUR = 4  # talk.ask_to_open at most this often per device
SHOWN = {"it-IT": "Te lo mostro.", "en-US": "It's on your screen."}  # Pisa's one line for what it shows
# a tap can't approve what needs Face ID: the card stays on the phone
TAP_FACE_ID = {
    "it-IT": "Questa va confermata con Face ID sul telefono.",
    "en-US": "This one needs Face ID on the phone.",
}

_UNITS = {"uno": 1, "una": 1, "due": 2, "tre": 3, "tré": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7,
          "otto": 8, "nove": 9}  # fmt: skip
_TEENS = {
    "zero": 0, "dieci": 10, "undici": 11, "dodici": 12, "tredici": 13, "quattordici": 14, "quindici": 15,
    "sedici": 16, "diciassette": 17, "diciotto": 18, "diciannove": 19, "mezzanotte": 0, "mezzogiorno": 12,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "noon": 12, "midnight": 0,
}  # fmt: skip
_TENS = {"venti": 20, "vent": 20, "trenta": 30, "trent": 30, "quaranta": 40, "quarant": 40, "cinquanta": 50,
         "cinquant": 50, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}  # fmt: skip
_EN_UNITS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}


def _number(word: str) -> int | None:
    if word in _TEENS:
        return _TEENS[word]
    if word in _UNITS:
        return _UNITS[word]
    if word in _TENS:
        return _TENS[word]
    for tens, value in _TENS.items():  # "trentadue", "ventuno", "quarantotto"
        if word.startswith(tens) and (unit := _UNITS.get(word[len(tens) :])) is not None:
            return value + unit
    return None


def echo_words(text: str) -> list[str]:
    """The words of a line with every number as digits, so Pisa's "alle due e trentadue"
    and the transcript's "2:32" read the same; a clock's ":00" is no word ("2:00" is "2")."""
    out: list[str] = []
    for piece in re.findall(r"\d+|[^\W\d_]+", text.casefold()):
        if piece.isdigit():
            if not (piece == "00" and out and out[-1].isdigit()):
                out.append(str(int(piece)))
            continue
        value = _number(piece)
        if value is None:
            out.append(piece)
        elif (
            out and out[-1].isdigit() and int(out[-1]) in _TENS.values() and value < 10 and piece in _EN_UNITS
        ):
            out[-1] = str(int(out[-1]) + value)  # "thirty two" → 32
        else:
            out.append(str(value))
    return out


SHORT_MIN_WORDS = 4  # "Fatto." alone says nothing of what was done: the next sentence joins it


def _first(reply: str) -> str:
    """The reply's first sentence, with the next ones while it is under SHORT_MIN_WORDS words."""
    out = ""
    for sentence in (s.strip() for s in _SENTENCE.findall(reply.strip()) if s.strip()):
        out = f"{out} {sentence}".strip()
        if word_count(out) >= SHORT_MIN_WORDS:
            break
    return out


def shown_line(reply: str, lang: str) -> str:
    """What Pisa's voice says when the turn showed something: its first sentence, and "Te lo mostro." unless
    that sentence already points at the screen. Never the whole object read aloud."""
    first = _first(reply)
    shown = SHOWN.get(lang, SHOWN["en-US"])
    if not first:
        return shown
    if re.search(r"\b(?:mostr\w*|schermo|ecco|guarda|screen|showing|here's|here is)\b", first, re.I):
        return first
    return f"{first} {shown}"


def short_line(reply: str) -> str:
    """On AirPods, the glasses or in the car Pisa says one sentence; the rest is on the screen."""
    return _first(reply) or reply.strip()


# "Pisa, what's next?" in about 2 seconds: the plan's next item from Pisa's own data, no model.
# only the whole short question, after an optional "Pisa,", "ok", "e", "allora", "senti":
# "cosa c'è dopo la palestra?", "what's next week?", "sposta il prossimo appuntamento" are other requests.
_NEXT_LEAD = r"(?:(?:pisa|ok|okay|e|allora|senti|dimmi|hey)\b[\s,.:;!]*)*"
_NEXT_ASK = (
    r"(?:cosa |che |cos')(?:c'e|ho) dopo(?: oggi)?"
    r"|(?:qual e|qual'e|cos'e|cosa e) il prossimo(?: impegno)?(?: (?:di )?oggi)?"
    r"|(?:il )?prossimo impegno"
    r"|e (?:dopo|adesso|ora)(?: cosa(?: c'e| ho)?)?"
    r"|what(?:'s| is) (?:up )?next(?: on my (?:plan|list|schedule|agenda))?(?: (?:today|now))?"
    r"|what do i have next(?: today)?"
    r"|what(?:'s| is) my next(?: (?:today|now))?"
    r"|next up"
)
_NEXT = re.compile(rf"{_NEXT_LEAD}(?:{_NEXT_ASK})[\s?!.]*")
_NEXT_LINE = {"it-IT": "Il prossimo è {item}.", "en-US": "Next is {item}."}
_NOTHING_NEXT = {"it-IT": "Per oggi non hai altro in programma.", "en-US": "Nothing else is planned today."}


def asks_next(words: str) -> bool:
    """The user asks for the plan's next item ("Pisa, cosa ho dopo?", "what's next?")."""
    said = " ".join(fold_apostrophe(words).split())  # the transcript's curly apostrophe and accents folded
    return bool(_NEXT.fullmatch(said))


def next_line(item: str, lang: str) -> str:
    """Pisa's own line for the next item (`Fresh.next_item`: "Dentista alle 15:30"), or that none is left."""
    table = _NEXT_LINE if item.strip() else _NOTHING_NEXT
    return table.get(lang, table["en-US"]).format(item=item.strip())


# when they say Pisa got it wrong, Pisa's first words own it before anything else
_BLAMES = re.compile(
    r"\b(?:hai\s+sbagliato|ti\s+sei\s+sbagliat\w*|hai\s+capito\s+male|non\s+hai\s+capito|hai\s+fatto\s+un\s+errore|"
    r"you\s+got\s+it\s+wrong|you\s+made\s+a\s+mistake|you\s+messed\s+(?:it\s+)?up|that'?s\s+wrong)\b",
    re.IGNORECASE,
)
_OWNED = re.compile(
    r"\b(?:ho\s+sbagliato|mi\s+sono\s+sbagliat\w*|errore\s+mio|colpa\s+mia|hai\s+ragione|scusa(?:mi)?|"
    r"my\s+(?:mistake|bad)|you'?re\s+right|sorry|i\s+got\s+it\s+wrong)\b",
    re.IGNORECASE,
)
OWN_IT = {"it-IT": "Ho sbagliato: ", "en-US": "I got that wrong: "}  # the voice bible's words


def owned(sentence: str, owner_words: str, lang: str) -> str:
    """The reply's first sentence, owning the mistake first when they say Pisa got it wrong and it doesn't."""
    if not sentence.strip() or not _BLAMES.search(owner_words) or _OWNED.search(sentence):
        return sentence
    lead = len(sentence) - len(sentence.lstrip())
    body = sentence[lead:]
    keep_case = body[:2].isupper() or body.split(" ", 1)[0] in ("Pisa", "I")
    return (
        sentence[:lead]
        + OWN_IT.get(lang, OWN_IT["en-US"])
        + (body if keep_case else body[0].lower() + body[1:])
    )


# after answering their question, a
# closing offer nobody asked for ("Vuoi che lo aggiunga?", "…, o ti preparo un messaggio?") is one more
# question for them to answer; it goes, unless a tool asked Pisa to offer (a starter skill's one offer) or
# Pisa holds a change
_OFFER = re.compile(
    r"^\W*(?:e\s+)?(?:vuoi\s+che|ti\s+(?:preparo|cerco|mando|metto|aggiungo|segno|ricordo|mostro|faccio)|"
    r"(?:lo|la|li|le|ne)\s+(?:aggiungo|metto|segno|preparo|cerco|tengo)|preferisci\s+che|se\s+vuoi|"
    r"(?:gli|le)\s+(?:rispondo|scrivo)|"  # "Gli rispondo?"
    r"posso\s+(?:aggiungerl|metterl|segnarl|preparart|cercart|mandart|ricordart)\w*|devo\s+(?:aggiungerl|"
    r"metterl|segnarl)\w*|want\s+me\s+to|shall\s+i|should\s+i|would\s+you\s+like\s+me\s+to|"
    r"(?:do\s+you\s+)?want\s+me\s+to)\b[^.!?]*\?\s*$",
    re.IGNORECASE,
)


_DATA_BLOCK = re.compile(r"<([a-z-]+)-([0-9a-f]+)>.*?</\1-\2>", re.DOTALL)


def asks_to_offer(result: str, marker: str) -> bool:
    """A tool's result asks Pisa to offer, in Pisa's own words outside every data block
    (never an "offer" in an email, a page or a provider's text)."""
    return marker in _DATA_BLOCK.sub("", result)


def closing_offer(sentence: str) -> bool:
    """A closing offer Pisa wasn't asked for: a question that offers one more thing."""
    return bool(_OFFER.search(sentence.strip()))


_OFFERED = re.compile(r'skill named "([^"]+)"')


def offered_name(result: str) -> str:
    """The starter skill a tool's result asked Pisa to offer ("" when it names none)."""
    found = _OFFERED.search(_DATA_BLOCK.sub("", result))
    return found.group(1) if found else ""


def without_closing_offer(
    rest: str, said: str, owner_words: str, *, keep: tuple[str, ...] = (), answered: bool = False
) -> str:
    """The reply's last part without a closing offer, when they asked a question and Pisa answered it before
    it (`said` streamed, or the sentences before it here); never the reply's only sentence. `keep`: the skills
    a tool asked Pisa to offer (that offer stays, naming the skill or "skill"; any other goes).
    `answered`: their words asked for what the reply gives, though not as a question ("chiudiamo")."""
    if not (owner_words.strip().endswith("?") or answered):
        return rest

    def asked_for(sentence: str) -> bool:
        low = sentence.casefold()
        return bool(keep) and ("skill" in low or any(k and names_skill(k, low) for k in keep))

    def unasked(sentence: str) -> bool:
        return closing_offer(sentence) and not asked_for(sentence)

    # a quote that ends its sentence («…Pausa caffè domani?») ends it here too
    sentences = [s for s in re.split(r"(?<=[.!?…])\s+|(?<=[.!?…][»”\"])\s+", rest.strip()) if s]
    kept = list(sentences)
    while kept and unasked(kept[-1]):
        kept.pop()  # "Vuoi che lo aggiunga? Vuoi che lo aggiunga?": every closing offer
    # the offer as the last clause, after a colon ("Non risulta nemmeno un
    # evento: vuoi che lo aggiunga?"): its answer stays, a full stop for the question
    if (
        kept
        and len(kept) == len(sentences)
        and not asked_for(kept[-1])
        and (answer := _before_offer(kept[-1]))
    ):
        kept[-1] = answer
        return " ".join(kept)
    if len(kept) == len(sentences) or not (kept or said.strip()):
        return rest
    return " ".join(kept)


def names_skill(name: str, low: str) -> bool:
    """The offer names the skill when it says the name, or every word of it."""
    words = re.findall(r"\w{3,}", name.casefold())
    return name.casefold() in low or (bool(words) and all(re.search(rf"\b{w}", low) for w in words))


def _before_offer(sentence: str) -> str | None:
    """A sentence's answer when its last clause, after a colon, is a closing offer; else None."""
    head, colon, tail = sentence.rpartition(": ")
    if not colon or not re.search(r"\w", head) or not closing_offer(tail):
        return None
    return head.rstrip(" ,;:") + "."


_DENY = frozenset(
    {"non", "not", "no", "mai", "never", "nessun", "nessuno", "nessuna", "nothing", "niente", "t"}
)
REPEAT_SAID = 78  # rapidfuzz ratio: two sentences this alike, with the same numbers, say the same thing


def said_before(sentence: str, said: list[str]) -> bool:
    """The sentence repeats one Pisa already said this turn (its answer written
    with a read, then again after it: "non hai nessun dentista…" / "non ho trovato nessun dentista…")."""
    from rapidfuzz import fuzz

    def flat(text: str) -> str:
        return " ".join(re.findall(r"\w+", text.casefold()))

    def denied(text: str) -> list[str]:  #  3: "non" (or "not", "mai") makes another sentence
        return [w for w in flat(text).split() if w in _DENY]

    def marks(text: str) -> set[str]:
        """Its numbers, quoted titles and names (a capital past the first word): what makes it another."""
        quoted = {flat(q) for q in re.findall(r"«([^»]+)»|\"([^\"]+)\"", text) for q in q if q}
        names = {w.casefold() for w in re.findall(r"(?<!^)(?<![.!?]\s)\b[A-ZÀ-Ý]\w+", text.strip())}
        return set(re.findall(r"\d+", text)) | quoted | names

    words = flat(sentence)
    if len(words.split()) < 5:
        return False
    return any(
        fuzz.ratio(words, flat(s)) >= REPEAT_SAID
        and marks(s) == marks(sentence)
        and denied(s) == denied(sentence)
        for s in said
    )


REPEAT_MEANT = 70  # rapidfuzz token_set_ratio: a sentence whose words are mostly said already, same numbers


def repeats(sentence: str, said: Iterable[str]) -> bool:
    """The sentence says again what one Pisa already
    said this turn, in other words ("Il dentista è giovedì alle 17." after "Hai il dentista giovedì alle
    17."): its words mostly among a said sentence's, and no number, "non" or name of its own."""
    from rapidfuzz import fuzz

    words = flat(sentence)
    if not words:
        return True
    numbers = set(re.findall(r"\d+", sentence))
    denies = {w for w in words.split() if w in _DENY}
    for s in said:
        for one in _SENTENCE.findall(s) or [s]:
            other = flat(one)
            if not other:
                continue
            if numbers - set(re.findall(r"\d+", one)) or denies - {w for w in other.split() if w in _DENY}:
                continue
            if fuzz.token_set_ratio(words, other) >= REPEAT_MEANT or said_before(sentence, [one]):
                return True
    return False


def split_sentences(text: str) -> list[str]:
    """The text's sentences (whitespace kept with the sentence it ends). The lead's splitter
    (a decimal, a clock time, an initial never ends one: "4.5 stelle" is not "4.")."""
    return [m for m in WHOLE_SENTENCE.findall(text) if m.strip()] or ([text] if text.strip() else [])


def adds(sentence: str, sent: Iterable[str], held: Iterable[str], *, ask: bool) -> bool:
    """After the model's reply was said, a later run's sentence is said only when it adds
    something: the question for a change waiting for their yes, or what no said sentence says."""
    if not sentence.strip():
        return False
    if ask and sentence.rstrip().endswith("?"):
        return True
    return not repeats(sentence, [*sent, *held])


def flat(text: str) -> str:
    """Words only, folded (case and punctuation aside)."""
    return " ".join(re.findall(r"\w+", text.casefold()))


# the time the "now" tool told the model, and whether a reply says it
_TIME_TOLD = re.compile(r"\bThe time: \w+ (\d{1,2}):(\d{2})\b")


def time_told(result: str) -> str:
    """The "HH:MM" the "now" tool's first line says ("The time: Friday 18:40 (America/New_York)."), or ""."""
    found = _TIME_TOLD.search(result)
    return f"{int(found.group(1)):02d}:{found.group(2)}" if found else ""


def says_clock(reply: str, hhmm: str) -> bool:
    """The reply says this time, in digits ("18:40", "18.40", "18 e 40") or by its hour ("le 18", "6:40")."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    hours = "|".join(sorted({str(hour), f"{hour:02d}", str(hour % 12 or 12)}, key=len, reverse=True))
    mins = f"(?:{minute:02d}|{minute})" if minute else "00"
    low = reply.casefold()
    if re.search(rf"\b(?:{hours})\s*(?::|\.|\s+e\s+|\s+and\s+)\s*{mins}\b", low):
        return True
    if minute == 0 and re.search(rf"\b(?:le|alle|ore|at|it'?s)\s+(?:{hours})\b", low):
        return True
    words = {0: "", 15: "un quarto", 30: "mezza", 45: "tre quarti"}
    return (
        minute in words
        and minute != 0
        and re.search(rf"\b(?:{hours})\s+e\s+{words[minute]}", low) is not None
    )


def it_is(hhmm: str, lang: str) -> str:
    """ "Sono le 18:40." / "It's 18:40." ("È l'1:05.")."""
    if lang != "it-IT":
        return f"It's {hhmm}."
    return f"È l'{hhmm[1:]}." if hhmm.startswith("01:") else f"Sono le {hhmm}."
