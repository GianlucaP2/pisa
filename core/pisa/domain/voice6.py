"""Il prurito, the miss
becomes a question, Pisa's traits in the turn. Pure rules.

- **Il prurito**: their question that Pisa's own reply could not answer (Pisa says it doesn't know, or that
  the turn couldn't check) is a gap. It opens one row of the ignorance map, in Pisa's words (never theirs,
  never a health word, never a name of their people: `voice2.private`), with a same-day deadline. Pisa says
  one short line, once a day and only when the answer could change something for them (their question is about
  a decision or a day of theirs): "Questa mi ha incuriosito: ci guardo e ti dico solo se cambia qualcosa."
  Only their words open one: Pisa's reply after outside text in the turn never does.
- **The miss becomes a question**: a prediction that missed with confidence (its Brier term at least
  `CONFIDENT²`, `pisa.domain.curious.confident_miss`) already opens "perché mi sono sbagliato?";
  the night now looks for its cause in their own records (a trip of their covering the day) and
  keeps it as the reason, a record reference. It reaches them only as a knock's part, a question they can wave
  away.
- **Pisa's traits**: their approved traits are one short line each in the instructions' fixed part
  (`character_block`), at most three.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from pisa.domain import claims, talk, voice2
from pisa.domain.night import name_parts
from pisa.domain.text import words_keep_apostrophe
from pisa.domain.voice1 import PART_MAX

# The line Pisa says when a gap opens and the answer could change something for them (it/en/nl).
LINE = {
    "it-IT": "Questa mi ha incuriosito: ci guardo e ti dico solo se cambia qualcosa.",
    "en-US": "That made me curious: I'll look into it and tell you only if it changes something.",
    "nl-NL": "Daar ben ik nieuwsgierig naar: ik zoek het uit en zeg het je alleen als het iets verandert.",
}
# the model's answers are held to these by their schemas (`brain.voice6.Phrased`, `Judged`)
QUESTION_MAX = 200  # Pisa's question, as it notes it for itself
QUERY_MAX = 120  # the web search Pisa makes of it
ANSWER_MAX = 160
# any outside text the talk read (a mail, an invite's title, a feed, a tool's output, a
# web page or a search result; an earlier turn's restored by the history included) means no row and no line
# (`pisa.services.voice6.outside_kinds` names them). Only their words open a gap.
RAW_RUN = 5  # a run of this many of their words in a row is their raw words, never Pisa's question
WAVE_WITHIN_S = 600  # their "lascia stare" to a miss's reason: right after Pisa's reading, within 10 minutes
# the look after a reply keeps the old allowance
# as its own, minus what the turn's research used; a long research turn leaves its question to the night
LOOK_CALLS = 12
LOOK_SECONDS = 25.0
LOOK_MIN_S = 2.0  # the turn's research time left under this: the question goes to the night


def private(text: str, people: Sequence[str] = ()) -> bool:
    """`voice2.private` on their people's full names and on each part of them ("Nino" of "Nino Bianchi")."""
    return voice2.private(text, (*people, *name_parts(people)))


def lang_key(lang: str) -> str:
    return lang if lang in LINE else "en-US"


# Pisa's own reply says it doesn't know, or that the turn couldn't settle it.
# the real model rarely says "non lo so"; it says it has no access to the
# data, that it can't check or see it, that it has no way to know ("Non ho accesso agli orari", "Non riesco a
# vedere gli orari di domani", "non posso dirti quale arriva prima", "I don't have access to live
# timetables"). Only verbs of knowing, finding and checking: "non riesco a mostrarti la tabella" (the phone
# isn't connected) is no gap.
_KNOW_IT = (
    r"(?:trovar\w*|verificar\w*|controllar\w*|capir\w*|veder\w*|saper\w*|dir\w*|cercar\w*|"
    r"consultar\w*|accedere|recuperar\w*|leggere|confermar\w*|fornir\w*|dar\w*|garantir\w*)"
)
_KNOW_EN = r"(?:find|check|verify|tell|see|look (?:it |that |this )?up|access|know|confirm|get|provide|give)"
_PISA_GAP = re.compile(
    r"\b(non lo so|non so (?:se|quando|quanto|quanti|quante|come|dove|chi|perch\w|cosa|quale|dirti)|"
    r"non saprei|non lo sapr\w+|"
    r"non ne ho traccia|non ho (?:trovato|questa informazione|quell\w informazion\w|dati su)|"
    r"non sono riuscit\w ad? " + _KNOW_IT + r"|non riesco ad? " + _KNOW_IT + r"|"
    r"non (?:posso|potrei) " + _KNOW_IT + r"|non sono in grado di " + _KNOW_IT + r"|"
    r"non ho (?:modo|la possibilit\w|gli strumenti) (?:di|per) " + _KNOW_IT + r"|"
    r"non ho (?:\w+ ){0,2}accesso|"
    r"non (?:ho|dispongo di|conosco) (?:(?:gli|i|le|l'|gli ultimi|i dati sugli) ?)?"
    r"(?:orari|dati|informazioni|dettagli|notizie)|non ho (?:informazioni|notizie)|"
    r"non (?:ne )?sono sicur\w|non so (?:\w+ ){0,2}(?:se|quale|quando|quanto|come|dove|chi)\b|"
    r"non (?:conosco|ho|dispongo (?:di|degli|dei|delle)) (?:(?:l'|gli |i |le )?)orar(?:io|i)\b|non so dir\w*|"
    r"non ho a disposizione|non ho idea|non mi (?:è|e) possibile|"
    # "Non vedo gli orari dei treni da qui", "non vedo i dati in tempo reale"
    r"non vedo (?:(?:gli|i|le|l') ?)?(?:orari|dati|informazioni|dettagli|tempi)\b|"
    r"i don't know|i do not know|i'm not sure|i am not sure|i couldn't " + _KNOW_EN + r"|"
    r"i (?:can't|cannot|can not|am unable to|'m unable to) " + _KNOW_EN + r"|i'm unable to " + _KNOW_EN + r"|"
    r"i have no (?:record|information|data|access|way to " + _KNOW_EN + r")|"
    r"i don't have (?:[\w-]+ ){0,3}(?:access|information|data|details|schedules?|timetables?)|"
    r"i'm not able to " + _KNOW_EN + r"|i am not able to " + _KNOW_EN + r"|"
    r"weet ik niet|ik weet (?:het )?niet|ik kon (?:\w+ ){0,3}niet (?:vinden|controleren|nagaan|zien)|"
    r"ik kan (?:\w+ ){0,3}niet (?:vinden|controleren|nagaan|zien|opzoeken)|"
    r"ik heb geen (?:\w+ ){0,2}(?:toegang|informatie|gegevens)|"
    r"ik weet niet zeker)\b",
    re.IGNORECASE,
)
_ASKS = re.compile(
    r"^(cosa|che cosa|come|quando|quanto|quanti|quante|dove|perch\w|chi|qual\w*|conviene|posso|devo|dovrei|"
    r"è vero|e vero|esiste|c'è|ce|what|how|when|where|why|who|which|is|are|do|does|did|can|could|should|"
    r"will|wat|hoe|wanneer|waar|waarom|wie|welk\w*|kan|moet|is er)\b",
    re.IGNORECASE,
)
# The answer could change something for them: a decision or a day of theirs.
_MATTERS = re.compile(
    r"\b(devo|dovrei|conviene|posso|mi serve|mi conviene|vale la pena|meglio|oggi|domani|dopodomani|stasera|"
    r"stamattina|stanotte|settimana|weekend|fine settimana|luned\w|marted\w|mercoled\w|gioved\w|venerd\w|"
    r"sabato|domenica|should i|do i need|can i|is it worth|better|today|tonight|tomorrow|this week|weekend|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|moet ik|kan ik|is het de moeite|beter|"
    r"vandaag|morgen|vanavond|weekend)\b",
    re.IGNORECASE,
)


def asks(text: str) -> bool:
    """Their words ask something: a "?", or a question word first."""
    words = " ".join(text.split())
    return "?" in words or bool(_ASKS.match(words_keep_apostrophe(words)))


# what reads like a gap and isn't ("non posso dirti di no", "non
# vedo l'ora", "I can't see why not", "I can't get there before 5") or is about Pisa's own reach (a connector
# of theirs: "non ho più accesso a quel calendario"): never a gap, so never "ci guardo e ti dico".
_NOT_GAP = re.compile(
    r"\b(?:dirti di no|di no|veder\w* l'ora|why not|how glad|get there|without your yes|"
    r"calendari\w*|posta|mail|vocal\w*|telefono|calendar|mailbox|voicemail|phone|agenda|e-?mail)\b",
    re.IGNORECASE,
)


_SENTENCE = re.compile(r"[^.!?;\u2026]+[.!?;\u2026]*")
_CLAUSE_END = re.compile(r"[,;:.!?\u2026]")


def _says_gap(sentence: str) -> bool:
    """One sentence says Pisa doesn't know, and what it doesn't know (the clause from each "non so" / "non ho
    accesso" to the next comma) is no turn of phrase and no reach of Pisa's own (`_NOT_GAP`)."""
    found = False
    for m in _PISA_GAP.finditer(sentence):
        end = _CLAUSE_END.search(sentence, m.end())
        if _NOT_GAP.search(sentence[m.start() : end.start() if end else len(sentence)]):
            return False  # "Non ho più accesso a quel calendario, quindi non so se sei libero": Pisa's reach
        found = True
    return found


def pisa_gap(reply: str) -> bool:
    """Pisa's reply says it doesn't know, or that it couldn't check (`_says_gap`, sentence by sentence).

    `_NOT_GAP` read the whole reply, and the
    real model's "don't know" reply almost always tells their day too ("Domani il calendario è libero", "non
    hai niente in agenda"): no gap, so no row, no look and no knock. It reads what Pisa says it doesn't know
    now, clause by clause ("Non ho più accesso a quel calendario." is still no gap)."""
    return bool(gap_sentences(reply))


# Pisa's own reach is out, anywhere in the reply ("Non so se sei
# libero domani: il tuo calendario non è collegato.", "Non ho più accesso a quel calendario. Non so se sei
# libero."). The gap is then Pisa's connector, not the world: "ci guardo e ti dico" would be a hollow promise.
# Only an outage of a reach of Pisa's, never a mention of their calendar ("Domani il calendario è libero").
_REACH = (
    r"(?:calendari\w*|agenda|posta|mail|e-?mail|vocal\w*|telefono|calendar|mailbox|voicemail|phone|inbox)"
)
_REACH_OUT = re.compile(
    r"\b"
    + _REACH
    + r"\b[^.!?\u2026]{0,40}\bnon\s+(?:è|e|sono)\s+(?:più\s+)?(?:collegat\w*|conness\w*|raggiungibil\w*)|"
    r"\bnon\s+(?:ho|riesco\s+ad?)\s+(?:più\s+)?(?:accesso|accedere|leggere|vedere|aprire)\W+"
    r"(?:\w+\W+){0,2}(?:a|al|alla|ai|alle|il|la|i|le)?\s*(?:(?:tuo|tua|tuoi|tue|quel|quella|quei)\s+)?"
    + _REACH
    + r"\b|"
    r"\b(?:your|the)\s+" + _REACH + r"\s+(?:is|isn't|is not)\s+(?:not\s+)?(?:connected|linked|available)\b|"
    r"\bi\s+(?:no\s+longer\s+)?(?:can't|cannot|don't|do not)\s+(?:have\s+)?(?:access|read|open|reach)\s+"
    r"(?:to\s+)?(?:your|the|that)\s+" + _REACH + r"\b|"
    r"\b(?:je|de)\s+" + _REACH + r"\s+is\s+niet\s+(?:gekoppeld|verbonden)\b",
    re.IGNORECASE,
)


def gap_sentences(reply: str) -> list[str]:
    """The sentences of Pisa's reply that say Pisa doesn't know (`_says_gap`); none when the reply says a
    reach of Pisa's own is out (`_REACH_OUT`, anywhere in the reply)."""
    text = " ".join(reply.replace("\u2019", "'").split())
    if _REACH_OUT.search(text):
        return []
    return [s for s in _SENTENCE.findall(text) if _says_gap(s)]


def matters(text: str) -> bool:
    """Their question is about a decision or a day of theirs: the answer could change something for them."""
    return bool(_MATTERS.search(text))


@dataclass(frozen=True)
class Gap:
    matters: bool


def gap(his: str, reply: str, people: Sequence[str] = ()) -> Gap | None:
    """A gap in the conversation: their question, Pisa's reply that doesn't know. None when their words or
    Pisa's reply carry a health word or a name of their people (`voice2.private`: Pisa never notes those)."""
    said = gap_sentences(reply)
    if not his.strip() or not asks(his) or not said:
        return None
    # the private rail reads their words and what Pisa says it doesn't know, never the
    # rest of Pisa's reply: "le 9 ti danno un'ora di sonno in più" kept every real gap from its row, though
    # Pisa's reply never reaches the row (Pisa's question is phrased from their words alone)
    if private(his, people) or any(private(s, people) for s in said):
        return None
    return Gap(matters(his))


def line(lang: str) -> str:
    return LINE[lang_key(lang)]


# the line is appended to Pisa's reply, so it never follows a reply whose end the other rails
# check: a question (their yes or their answer binds to it), a proposal or an approval asked, an act claimed
# ("Fatto."), a recap (the day's close). `quiet_after` reads the reply; the turn's own records (a held change,
# a proposal, a change made, a recap tool) are the conversation's (`Prurito.decide`'s `open_question`).
_PROPOSAL = re.compile(
    r"\b(?:ti propongo|propongo|ti suggerisco|ti consiglio|ti va (?:se|di)|vuoi che|se vuoi|se ti va|"
    r"dimmi s[iì]|se dici s[iì]|se mi dai l'ok|aspetto il tuo s[iì]|serve il tuo s[iì]|"
    r"(?<!non )posso \w+|(?<!non )potrei \w+|fammi sapere|dimmelo|dimmi \w+|decidi tu|"
    r"i propose|i suggest|shall i|should i|do you want me to|want me to|if you like|if you want|say yes|"
    r"i can \w+|i could \w+|let me know|tell me|it's up to you|"
    r"ik stel voor|zal ik|wil je dat ik|als je wilt|zeg ja|laat (?:het )?me weten|"
    # the real model hands the question back ("se mi dici da dove parti, ti dico
    # quale prendere", "Guarda su Trenitalia", "controlla l'app"): their next words answer it, or they look
    r"se mi dici|se mi fai sapere|se mi scrivi|ti dico (?:quale|se|quanto|come|quando)|ti aiuto|"
    r"(?:controlla|guarda|verifica|consulta)(?:lo|la)? (?:su|sul|sulla|sull'|nell'|nella|l'|la|il|tu|"
    r"direttamente)|(?:ti )?conviene controllare|"
    r"if you tell me|check (?:on|the|with)|look it up|als je me (?:vertelt|zegt)|kijk (?:op|in|even))\b",
    re.IGNORECASE,
)
_DONE_WORD = re.compile(
    r"\b(?:fatto|fatta|done|gedaan|klaar|noted|all set|added it|ho annotato|annotato|aggiunt[oa]|"
    r"toegevoegd|genoteerd)\b",
    re.IGNORECASE,
)
_RECAP = re.compile(
    r"\b(?:riepilogo|ricapitol\w+|riassumendo|in sintesi|in breve|ecco (?:com'è andata|la tua giornata)|"
    r"oggi hai fatto|recap|summary|in short|to sum up|here's your day|samenvatting|kort samengevat|"
    r"hier is je dag|vandaag heb je)\b|^(?:oggi|today|vandaag)\s*:",
    re.IGNORECASE,
)


def quiet_after(reply: str) -> str | None:
    """Why the line never follows this reply ("question", "proposal", "act", "recap"), or None."""
    text = " ".join(reply.split()).replace("\u2019", "'")
    if not text:
        return "empty"
    if "?" in text:  # a question anywhere: their next words may answer it
        return "question"
    sentences = re.findall(r"[^.!?\u2026]+[.!?\u2026]*", text)
    if _PROPOSAL.search(text) or any(claims.asks_yes(s) for s in sentences):
        return "proposal"
    if _DONE_WORD.search(text) or claims.claim(text) is not None:
        return "act"
    if _RECAP.search(text):
        return "recap"
    return None


def his_raw(question: str, his: str) -> bool:
    """Pisa's question carries their raw words: a run of `RAW_RUN` of their words in a row, or all of them."""
    q, h = words_keep_apostrophe(question).split(), words_keep_apostrophe(his).split()
    if not h:
        return False
    if " ".join(h) in " ".join(q):
        return True
    runs = {" ".join(h[i : i + RAW_RUN]) for i in range(len(h) - RAW_RUN + 1)}
    text = " ".join(q)
    return any(r in text for r in runs)


def phrased_ok(question: str, his: str, people: Sequence[str] = ()) -> bool:
    """Pisa's question for its map: short, one line, never their raw words, never a health word or a name of
    their people."""
    q = " ".join(question.split())
    return 8 <= len(q) <= QUESTION_MAX and not his_raw(q, his) and not private(q, people)


def fallback(day: date, lang: str) -> str:
    """When no model phrased it: Pisa's own typed note, with nothing of their words."""
    when = day.strftime("%d/%m")
    if lang_key(lang) == "it-IT":
        return f"Una sua domanda del {when} a cui non ho saputo rispondere."
    if lang_key(lang) == "nl-NL":
        return f"Een vraag van hem op {when} die ik niet kon beantwoorden."
    return f"A question of their on {when} I couldn't answer."


def answer_ok(answer: str, people: Sequence[str] = ()) -> bool:
    """An answer Pisa may say back: one sentence that fits the knock's part whole after its lead (
    never cut), no link, never a health word or a name of their people."""
    a = " ".join(answer.split())
    body = a.rstrip(".!?… ")
    return (
        3 <= len(a) <= ANSWER_MAX
        and not re.search(r"[.!?…]\s", body)  # one sentence
        and len(back(a, "it-IT")) <= ANSWER_ROOM  # the knock's part, whole, in the longest language
        and "http" not in a.casefold()
        and not private(a, people)
    )


def judged_ok(judged: Any, results: int, people: Sequence[str] = ()) -> bool:
    """The judgement Pisa may keep: answered, from a result that was there (its index in range), an answer it
    may say back (`answer_ok`: no link, no name, no health word), and no label of a person
    (`talk.no_label`)."""
    return (
        judged is not None
        and bool(judged.answered)
        and isinstance(judged.index, int)
        and 0 <= judged.index < results
        and answer_ok(judged.answer, people)
        and not talk.no_label(judged.answer)
    )


def said_ref(day: date) -> str:
    """The day's line is a keyed row of the map (UNIQUE (source, ref)): once a day."""
    return f"said:{day.isoformat()}"


# --- what reaches them, through the knock only ------------------------------------------------------------

_BACK = {
    "it-IT": "Sulla tua domanda di oggi ci ho guardato: {a}",
    "en-US": "I looked into your question from today: {a}",
    "nl-NL": "Ik heb je vraag van vandaag uitgezocht: {a}",
}
_REASON = {
    "it-IT": ("Credo di aver capito perché mi sono sbagliato il {d}: eri a {c}", ", e ci sei fino al {u}",
              "; da ora lo conto, o lascio stare?"),
    "en-US": ("I think I see why I got {d} wrong: you were in {c}", ", and you're there until {u}",
              "; shall I count it from now, or leave it?"),
    "nl-NL": ("Ik denk dat ik snap waarom ik me op {d} vergiste: je was in {c}", ", en je bent er tot {u}",
              "; reken ik er voortaan mee, of laat ik het?"),
}  # fmt: skip


# the knock says one sentence of at most PART_MAX per part: the answer fits whole after the
# longest lead
ANSWER_ROOM = PART_MAX


def back(answer: str, lang: str) -> str:
    a = " ".join(answer.split()).rstrip(".")
    return _BACK[lang_key(lang)].format(a=a) + "."


def reason(day: date, city: str, until: date | None, lang: str) -> str:
    """The cause the night found, as one question they can wave away (one sentence: the knock says one per
    part); made from the trip's record."""
    first, there, ask = _REASON[lang_key(lang)]
    said = first.format(d=day.strftime("%d/%m"), c=" ".join(city.split()))
    if until is not None and until > day:
        said += there.format(u=until.strftime("%d/%m"))
    return said + ask


def trip_ref(trip_id: str) -> str:
    return f"trip:{trip_id}"


def trip_id(ref: str | None) -> str | None:
    return ref.removeprefix("trip:") if ref and ref.startswith("trip:") else None


# Their answer to the reason, right after Pisa's reading: a closed set only (anything else is the model's).
_WAVE = frozenset({
    "lascia stare", "lascia perdere", "lascia", "non contarlo", "let it go", "leave it", "don't", "laat maar",
    "laat het",
})  # fmt: skip
_KEEP = frozenset({
    "contalo", "sì contalo", "si contalo", "count it", "yes count it", "reken ermee", "ja reken ermee",
})  # fmt: skip
# a bare acknowledgement answers the reason only when the reason was the reading's last part
_BARE_KEEP = frozenset({"sì", "si", "ok", "okay", "va bene", "yes", "ja"})
_BARE_WAVE = frozenset({"no", "no grazie", "no thanks", "nee", "nee dank je"})
WAVED = {"it-IT": "Va bene, lascio stare.", "en-US": "Okay, I'll leave it.", "nl-NL": "Goed, ik laat het."}
COUNTED = {"it-IT": "Bene: da ora lo conto.", "en-US": "Good: I'll count it from now.",
           "nl-NL": "Goed: ik reken er voortaan mee."}  # fmt: skip


def reason_answer(text: str, *, last: bool = False) -> bool | None:
    """True: they keep the reason; False: they wave it away; None: anything else (the model's). A bare "ok",
    "sì" or "no" counts only when the reason was the reading's last part (`last`): after il quarto d'ora's
    offer it answers that offer, never the reason."""
    words = words_keep_apostrophe(text)
    if words.startswith("pisa "):
        words = words[2:]
    if words in _BARE_WAVE:
        return False if last else None
    if words in _BARE_KEEP:
        return True if last else None
    if words in _WAVE:
        return False
    if words in _KEEP:
        return True
    return None


# --- the Sunday card ------------------------------------------------------------------------------------

_CARD = {
    "it-IT": "Curiosità nate parlando: {n} (cambiavano qualcosa {c}, niente {q}).",
    "en-US": "Curiosities from our talks: {n} (changed something {c}, nothing {q}).",
    "nl-NL": "Nieuwsgierigheden uit onze gesprekken: {n} (veranderden iets {c}, niets {q}).",
}


def card_line(changed: int, quiet: int, opened: int, lang: str) -> str | None:
    if opened <= 0:
        return None
    return _CARD[lang_key(lang)].format(n=opened, c=changed, q=quiet)


# --- Pisa's traits in the turn (their yes only)
# ---------------------------------------------------------------

TRAITS_MAX = 3
# One short line per approved trait, in the instructions' fixed part (English, as the system prompt).
TRAIT_LINES = {
    "wrong_first": "- When you got something wrong, say so first, plainly.",
    "short": "- Speak short: the thing first.",
    "days_add_up": "- Keep an eye on whether their days add up.",
}
_TRAITS_HEAD = "\n\nYour character (traits they approved; they shape how you speak, never what you do):\n"


def character_block(approved: Sequence[str]) -> str:
    """The approved traits' lines, at most three, in a fixed order (the cached prefix changes only on their
    yes); "" with none. A trait not in `TRAIT_LINES` is never read."""
    keys = [k for k in TRAIT_LINES if k in set(approved)][:TRAITS_MAX]
    if not keys:
        return ""
    return _TRAITS_HEAD + "\n".join(TRAIT_LINES[k] for k in keys)
