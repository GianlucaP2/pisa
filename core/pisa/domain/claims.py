"""Pisa never claims a change it didn't make. One rule for both voices: economy's reply (a sentence that
says Pisa did or is doing a change, with no successful change in the turn, is replaced by the truth) and the
talker's grounding check ("fatto" with no `done` whisper).

A reply can say "Sono sul 31. Li sposto al 30." with no tool called, or "Li metto oggi" with nothing put
anywhere. RAIL: the words of a reply prove nothing; only code's record of the change does.
"""

import re
from dataclasses import dataclass

from pisa.domain import effects
from pisa.domain.lexicon.numbers import decimal_point
from pisa.domain.text import fold_apostrophe, strip_marks

_PARTICIPLES = (
    r"spostat[oaie]|mess[oaie]|aggiunt[oaie]|segnat[oaie]|cancellat[oaie]|annullat[oaie]|creat[oaie]|"
    r"mandat[oaie]|inviat[oaie]|tolt[oaie]|eliminat[oaie]|fissat[oaie]|prenotat[oaie]|modificat[oaie]|"
    r"inserit[oaie]|programmat[oaie]|sistemat[oaie]|aggiornat[oaie]"
)
_PRESENT = (
    r"sposto|metto|aggiungo|segno|cancello|annullo|creo|mando|invio|tolgo|elimino|fisso|prenoto|modifico|"
    r"inserisco|sistemo|aggiorno"
)
_EN_DONE = r"moved|added|put|cancell?ed|created|sent|booked|scheduled|removed|deleted|changed|updated"
_EN_DOING = (
    r"moving|adding|putting|cancell?ing|creating|sending|booking|scheduling|removing|deleting|changing"
)
# "Ok, sposto tutto", "Sì, spostati!": the opening words before a bare verb.
_LEAD = r"^(?:(?:ok|okay|si|allora|va bene|bene|certo|perfetto|subito)\W+)*"
# Done: Pisa says the change is made ("Fatto.", "Ho spostato la cena.", "L'ho aggiunto.", "Sono sul 31.").
_DONE = re.compile(
    rf"\b(?:fatto|fatta|fatti|fatte|ecco fatto|tutto fatto)\b(?!\s+sta\b)"
    # "Spostata la cena alle 21.", "Aggiunto!", "Messo in calendario." ("Prenotate il venerdì" is advice: Pisa
    # says "tu", so a leading "-ate" with more words is the plural imperative)
    rf"|{_LEAD}(?!\w+ate\s+\w)(?:{_PARTICIPLES})\b"
    rf"|\b(?:e|sono)\s+stat[oaie]\s+(?:{_PARTICIPLES})\b"
    rf"|\b(?:ho|abbiamo|l'ho|li ho|le ho|gli ho|l'abbiamo)\s+(?:gia\s+)?(?:{_PARTICIPLES})\b"
    rf"|\b(?:done|all done|it's done|that's done)\b(?!\s+(?:is|was|by|with)\b)"  # "Done is better…" is none
    rf"|\bsono\s+(?:gia\s+)?(?:sul|sulla|sui|sulle)\s+\d"  # "sono sul 31": where they are now
    rf"|\b(?:i've|i have|we've)\s+(?:already\s+)?(?:{_EN_DONE})\b"
    rf"|\bi\s+(?:{_EN_DONE})\b"
)
# Doing: Pisa says it is making the change now ("Li sposto al 30.", "Te la segno fatta.").
_DOING = re.compile(
    rf"\b(?:lo|la|li|le|ci|ne|te\s?lo|te\s?la|te\s?li|te\s?le|glielo|gliela)\s+(?:{_PRESENT})\b"
    rf"|{_LEAD}(?:{_PRESENT})\b"
    rf"|\b(?:i'll|i will|now i'll)\s+(?:move|add|put|cancel|create|send|book|schedule|remove|delete|change)\b"
    rf"|\b(?:sto|stiamo)\s+(?:spostando|mettendo|aggiungendo|segnando|cancellando|creando|mandando)\b"
    rf"|\b(?:i'm|i am)\s+(?:{_EN_DOING})\b"
)
# idioms with the same verbs that change nothing: "Ho messo in fila…", "Lo metto
# in conto", "la segno come idea"; and an offer ("La sposto io se vuoi") is a question, not a claim.
_IDIOM = re.compile(r"\s*(?:in fila|in conto|in ordine|in dubbio|in chiaro|come idea|come nota)\b")
_OFFER = re.compile(r"\bse (?:vuoi|preferisci|ti va|serve|ti serve)\b|\bif you (?:want|like|prefer)\b")
# Showing is no change: "Te lo metto sul telefono" puts a card on the screen.
# A sentence that opens with its condition ("se era una svista, …", "if it was a mistake, …")
# a
# present act under a sentence-opening "when" is a promise too, as under "if"; a past one there is a claim
_CONDITION = re.compile(r"^\W*(?:se|if|quando|when|appena|non\s+appena|as\s+soon\s+as)\b")
_SCREEN = re.compile(r"\b(?:sul telefono|sullo schermo|sul tuo telefono|on (?:your|the) (?:phone|screen))\b")
_NEGATED = re.compile(
    r"\b(?:non|not|haven't|hasn't|isn't|didn't|ancora da|yet to|niente|nothing|never|mai)\b"
)
# Someone else did it ("Oggi hai fatto 8000 passi", "you've added"): no claim of Pisa's.
_OTHERS = frozenset({"hai", "ha", "hanno", "avete", "have", "has", "you", "you've", "he", "she", "they"})
# A negation counts only just before the claim ("Non l'ho ancora fatto", "non è ancora stato spostato"), never
# anywhere in the sentence: "Fatto, non preoccuparti: sono sul 30." is a claim.
_NEAR = 4


def _plain(text: str) -> str:
    return " ".join(fold_apostrophe(text).split())


# Someone else's words, reported: «…», "…", or what follows "ha scritto:" / "dice:" / "says:"
# ("Marco ha scritto: ho spostato la riunione" is Marco's claim, never Pisa's).
_QUOTED = re.compile(r"«[^»]*»?|\"[^\"]*\"?|\u201c[^\u201d]*\u201d?")
_REPORTED = re.compile(
    r"\b(?:scrive|scrivono|ha scritto|hanno scritto|dice|dicono|ha detto|hanno detto|risponde|ha risposto|"
    r"says|said|writes|wrote|replied|replies)\b[^:]{0,40}:.*$"
)


def _sentences(text: str) -> list[str]:
    """The text's sentences, plain, with quoted and reported words (someone else's) taken out."""
    return [
        _REPORTED.sub("", p).strip()
        for p in re.findall(r"[^.!?\u2026]+[.!?\u2026]*", _QUOTED.sub(" ", _plain(text)))
    ]


def claim(text: str) -> str | None:
    """What a sentence of the text claims Pisa did: "doing" (it is making a change now: "Li sposto al 30.",
    "Sono sul 31.") before "done" (it made one: "Fatto.", "Ho spostato la cena.", "I've moved it."), or None.
    A question ("Sposto la cena alle 21?") or a negated sentence ("Non l'ho ancora fatto") claims nothing."""
    found = None
    parts = _sentences(text)
    for n, part in enumerate(parts):
        if part.endswith("?") or _SCREEN.search(part) or _OFFER.search(part):
            continue
        if (team := team_claim(part)) is not None and not _CONDITION.match(part):
            # "lo segnalo al team", "l'ho proposto agli sviluppatori": a note Pisa keeps for the team
            if team == "doing":
                return "doing"
            found = "done"
            continue
        # "Se era una svista, lo annullo entro 24 ore" is a promise under a
        # condition, never a change made now; a past one in such a sentence ("se serve, l'ho spostata") is.
        # "Sposto la call alle 16 e la palestra alle 17. Dimmi sì e lo faccio.": the act waits for
        # the yes the next sentence asks: a proposal, never a change made now
        # "Dimmi sì e le sposto; non è ancora fatto." asks it in the
        # same sentence
        waits = (yes_follows(parts, n) or _yes_covers(part)) and not _said(_DONE, part)
        if _said(_DOING, part) and not _CONDITION.match(part) and not waits:
            return "doing"
        if _said(_DONE, part):
            found = "done"
    return found


# A sentence that asks their yes for what Pisa just said it would do ("Dimmi sì e lo faccio", "serve il tuo
# sì"); never negated ("Non serve il tuo sì")
_ASKS_YES = re.compile(
    r"\b(?:dimmi\s+(?:di\s+)?si|dammi\s+(?:il\s+tuo\s+)?(?:si|ok)|"
    r"(?:aspett(?:o|a|ano)|serve|servono|attend(?:o|e|ono))\s+(?:solo\s+|ancora\s+)?il\s+tuo\s+(?:si|ok)|"
    r"con\s+il\s+tuo\s+si|(?:say|tell\s+me)\s+yes|(?:i\s+need|(?:wait|waits|waiting)\s+for)\s+your\s+(?:yes|ok))\b"
)


# "Sposto la call con Anna alle 16 e la palestra
# alle 17. Va bene?":
# a question that is only their yes to go on (it names nothing, asks nothing else) asks their yes for what
# Pisa just said it would do, as "Dimmi sì" does. "Ti serve altro?" or "A che ora?" ask something else: never
# one.
_CONFIRMS = re.compile(
    r"^(?:e\s+|allora\s+|quindi\s+|so\s+|then\s+)?(?:(?:ti|vi|le)\s+)?(?:"
    r"va\s+bene|torna|sta\s+bene|(?:mi\s+)?confermi|confermo|procedo|posso\s+procedere|vado|"
    r"(?:lo|la|li|le)\s+(?:faccio|sposto|metto|aggiungo|segno)|faccio|ok|okay|d'accordo|sei\s+d'accordo|"
    r"che\s+ne\s+dici|sounds?\s+good|(?:is\s+that\s+)?ok(?:ay)?\s+(?:with|for)\s+you|"
    r"shall\s+i(?:\s+(?:go\s+ahead|do\s+it|proceed))?|should\s+i(?:\s+(?:go\s+ahead|do\s+it|proceed))?|"
    r"(?:can|may)\s+i\s+(?:go\s+ahead|proceed)|go\s+ahead|does\s+that\s+work(?:\s+for\s+you)?|"
    r"is\s+that\s+(?:ok(?:ay)?|fine|all\s+right)|all\s+right)"
    r"(?:\s*,?\s*(?:cosi|per\s+te|allora|then|for\s+you|with\s+you))?"
    r"(?:\s*,?\s*(?:si\s+o\s+no|yes\s+or\s+no))?\s*\?$"
)
# a question that points back at what Pisa just said ("così", "like this")
_BACK = re.compile(
    r"\b(?:cosi|in\s+questo\s+modo|come\s+(?:ti\s+)?ho\s+detto|like\s+(?:this|that)|this\s+way|"
    r"as\s+i\s+said)\b"
)


def points_back(sentence: str) -> bool:
    """The question asks about what Pisa said before it: a bare one to go on ("Va bene?") or one
    pointing back at it ("Ti sposto così?")."""
    return confirms(sentence) or _BACK.search(_plain(sentence)) is not None


def confirms(sentence: str) -> bool:
    """The sentence is a bare yes/no question to go on ("Va bene?", "Procedo?", "Shall I?")."""
    return _CONFIRMS.match(_plain(sentence.strip())) is not None


def asks_yes(sentence: str) -> bool:
    if confirms(sentence):
        return True
    return any(not _NEGATED.search(" ".join(m.string[: m.start()].split()[-_NEAR:]))
               for m in _ASKS_YES.finditer(_plain(sentence)))  # fmt: skip


_ACTS = re.compile(rf"\b(?:{_PRESENT})\b")
_JOINED_ACT = re.compile(rf"\s*(?:e|and)\s+(?:non\s+)?(?:(?:lo|la|li|le|ne|ci)\s+|l')?(?:{_PRESENT})\b")
_OTHER_THING = re.compile(r"\b(?:per|for)\s+(?:il|la|lo|l'|i|gli|le|the|un|una)?\s*\w")


def _yes_covers(part: str) -> bool:
    """The sentence's one act waits for the yes it asks: joined right after the yes
    ("Dimmi sì e le sposto") or in its clause; never when the yes names another thing ("dimmi sì per la
    palestra", "per la cena aspetto il tuo sì") or another change verb is in the sentence."""
    if len(_ACTS.findall(part)) != 1:
        return False
    for m in _ASKS_YES.finditer(part):
        if _NEGATED.search(" ".join(part[: m.start()].split()[-_NEAR:])):
            continue
        cut = re.search(r"[,;:]", part[m.end() :])
        clause = part[
            max(part.rfind(p, 0, m.start()) for p in ",;:") + 1 : m.end()
            + (cut.start() if cut else len(part))
        ]
        if _OTHER_THING.search(clause):
            continue
        if _JOINED_ACT.match(part, m.end()) or _ACTS.search(clause):
            return True
    return False


def yes_follows(sentences: list[str], n: int) -> bool:
    """A later
    sentence of the same reply asks their yes, with no question between (a question asks its own)."""
    for later in sentences[n + 1 :]:
        if asks_yes(later):
            return True
        if later.strip().endswith("?"):
            return False
    return False


def waits_for_yes(sentence: str, following: str | list[str]) -> bool:
    """An act Pisa says it will do, with the next sentence asking their yes for it, and no change
    claimed as made in the same sentence ("Sposto la palestra e ho mandato la mail. Dimmi sì…"
    claims the mail): a proposal, never a claim. `following` may be the sentences after it, the
    yes asked by any of them before another question."""
    later = [following] if isinstance(following, str) else following
    return (
        claim(sentence) == "doing"
        and not _said(_DONE, _plain(sentence))
        and yes_follows([sentence, *later], 0)
    )


def _said(pattern: re.Pattern[str], part: str) -> bool:
    """A match of the pattern with no negation in the few words before it (or inside it), not said of
    someone else."""
    for match in pattern.finditer(part):
        words = part[: match.start()].split()
        before = " ".join(words[-_NEAR:])
        if words and words[-1] in _OTHERS:
            continue
        if _IDIOM.match(part, match.end()):
            continue
        if not _NEGATED.search(before) and not _NEGATED.search(match.group(0)):
            return True
    return False


# Pisa says it passes something on to the team (the architect, the developers): a claim, backed only
# by the note `tell_the_team` stored this turn
TEAM_WORDS = r"(?:team|sviluppator\w*|developers?|architett[oa]|architect|chi\s+mi\s+sviluppa|my\s+makers)"
_TEAM_DOING = re.compile(
    r"\b(?:(?:lo|la|li|le|ne|glielo|gliela|te\s?lo)\s+)?(?:segnalo|propongo|riferisco|giro|passo|comunico|"
    r"scrivo|dico|riporto|faccio\s+presente|(?:i'll|i\s+will|let\s+me)\s+(?:tell|flag|pass|report|suggest|"
    r"raise|mention))\b[^.!?]{0,60}?\b(?:a[il]?|agli|all'|alla|to)\s*(?:the\s+|my\s+|mio\s+|nostro\s+|"
    rf"tuo\s+)?{TEAM_WORDS}"
)
_TEAM_DONE = re.compile(
    r"\b(?:(?:l'|lo\s+|la\s+|li\s+|le\s+|gli\s+)?(?:ho|abbiamo)\s+(?:gia\s+)?(?:segnalat|propost|riferit|"
    r"girat|passat|comunicat|scritt|dett|riportat)\w*|(?:segnalat|propost|girat|passat|riferit)[oaie]|"
    r"(?:i've|i\s+have|i)\s+(?:already\s+)?(?:told|flagged|passed|reported|suggested|raised|mentioned))"
    rf"\b[^.!?]{{0,60}}?\b(?:a[il]?|agli|all'|alla|to)\s*(?:the\s+|my\s+|mio\s+|nostro\s+|tuo\s+)?{TEAM_WORDS}"
    # noted for the team
    # ("per gli sviluppatori", "per il tuo team", "Ho segnato la richiesta per il team")
    rf"|\b(?:l'|lo\s+|la\s+)?(?:ho\s+)?segnat[oaie]\b[^.!?]{{0,60}}?\bper\s+(?:(?:il|i|gli|lo|la)\s+)?"
    rf"(?:(?:tuo|tuoi|mio|miei|nostro|nostri)\s+)?{TEAM_WORDS}"
)


def team_claim(sentence: str) -> str | None:
    """ "doing" ("lo segnalo al team") or "done" ("l'ho segnalato al team"), or None; never a
    negated one ("non posso segnalarlo al team")."""
    plain = _plain(sentence)
    for kind, pattern in (("doing", _TEAM_DOING), ("done", _TEAM_DONE)):
        found = pattern.search(plain)
        if found is not None and not _NEGATED.search(" ".join(plain[: found.start()].split()[-_NEAR:])):
            return kind
    return None


TEAM_TOOL = "tell_the_team"
TEAM_TRUTH = {
    "it-IT": "Non l'ho ancora segnalato al team: se vuoi, dimmi «segnalalo al team» e lo tengo per il loro "
    "prossimo rapporto.",
    "en-US": "I haven't passed it to the team yet: say «tell the team» and I'll keep it for their next "
    "report.",
}

# a bare acknowledgement ("Segnato.", "Ok, fatto!", "Me lo segno.") names no change of its own:
# in a reply to words that asked no change it claims nothing, and is never rewritten into the rail's words
_BARE_FILLER = frozenset(
    "ok okay si va bene perfetto certo ecco tutto me te lo la li le l ne grazie allora bene subito".split()
)


def bare(sentence: str) -> bool:
    """The sentence is only an acknowledgement word, with filler and a pronoun at most."""
    words = [w for w in re.findall(r"[a-z]+", _plain(sentence)) if w not in _BARE_FILLER]
    return claim(sentence) is not None and len(words) <= 1 and team_claim(sentence) is None


def claims_change(text: str) -> bool:
    """A sentence of the text says Pisa did, or is doing now, a change."""
    return claim(text) is not None


# the talker's "Sì, ho appena confermato il promemoria…".
# These verbs are its grounding's only: in the brain's own replies "Ho confermato che il
# negozio è aperto", "Ho completato la ricerca", "Ho registrato un calo…" report what Pisa found, never a
# change
_TALKER_PARTICIPLES = r"confermat[oaie]|completat[oaie]|registrat[oaie]|impostat[oaie]"
_TALKER_DONE = re.compile(
    rf"\b(?:ho|abbiamo|l'ho|li ho|le ho|gli ho|l'abbiamo)\s+(?:(?:gia|appena|ora|adesso)\s+)?"
    rf"(?:{_PARTICIPLES}|{_TALKER_PARTICIPLES})\b"
    rf"|{_LEAD}(?!\w+ate\s+\w)(?:{_TALKER_PARTICIPLES})\b"
    rf"|\b(?:e|sono)\s+stat[oaie]\s+(?:{_TALKER_PARTICIPLES})\b"
    rf"|\b(?:i've|i have|we've)\s+(?:(?:already|just)\s+)?(?:{_EN_DONE}|confirmed|saved|completed)\b"
    rf"|\bi\s+(?:confirmed|saved|completed)\b"
)


def talker_claims_change(text: str) -> bool:
    """The talker's grounding: `claims_change`, and also "ho (appena) confermato / completato /
    registrato / impostato…", "I've just confirmed…": the talker acts on nothing, so any of these with no
    `done` whisper is its own claim. Never for the brain's claim rail (`claim`)."""
    if claims_change(text):
        return True
    return any(
        _said(_TALKER_DONE, part)
        for part in _sentences(text)
        if not (part.endswith("?") or _SCREEN.search(part) or _OFFER.search(part) or _REPORTS.search(part))
    )


# "Ho confermato che il negozio è aperto", "Ho completato la ricerca: …",
# "I've confirmed the shop is open": a fact Pisa found, said over a SAY whisper, is no claim of a change
_REPORTS = re.compile(
    r"\b(?:confermat[oaie]|completat[oaie]|registrat[oaie]|confirmed|completed)\b\s*"
    r"(?:che\b|that\b|:|la\s+ricerca|the\s+search|un\s+calo|a\s+drop)",
    re.IGNORECASE,
)


NOT_DONE_YET = {
    "it-IT": "Non l'ho ancora fatto: non ho cambiato niente.",
    "en-US": "I haven't done it yet: nothing has changed.",
}
WAITS_FOR_YES = {
    "it-IT": "Non è ancora fatto: aspetta il tuo sì.",
    "en-US": "It isn't done yet: it waits for your yes.",
}


def truth(lang: str, *, waiting: bool) -> str:
    """What Pisa says instead of a claim it can't back: nothing changed, or it waits for the user's yes."""
    table = WAITS_FOR_YES if waiting else NOT_DONE_YET
    return table.get(lang, table["en-US"])


# --- the screen: "it's on your phone" only when the phone has it (one place) ----------

# Pisa's words that something is on the user's screen ("ti ho messo il link sul telefono")
# more phrasings ("te l'ho mandato sul telefono", "I've sent it to your phone", "check your
# phone")
# what Pisa shows (a link, a map, a card…): "è"/"sono"/"c'è" count only with one of them as subject
# ("Il numero di Marco è nel telefono, nella rubrica" is no screen); "ho messo" with another object neither
# ("Ho messo la sveglia sul telefono alle 7"); new: "sul telefono trovi…", "l'ho messa sul telefono", "guarda
# il telefono", "nell'app"
_SHOWN = (
    r"\b(?:link|mapp[ae]|foto|schede?|scheda|list[ae]|pagin[ae]|card|tabell[ae]|immagin[ei]|indicazioni|"
    r"percorso|risultati|dettagli|tutto|tutti|tutte)\b"
)
_ON_PHONE = (
    r"(?:\b(?:sul|sullo|nel|nello)\s+|\bsull['\u2019]\s*)(?:tuo\s+)?(?:telefono|schermo|cellulare|iphone)\b|"
    r"\bnell['\u2019]\s*app\b|\bnella\s+app\b"
)
_PUT = r"(?:aperto|mess[oaie]|mandat[oaie]|inviat[oaie]|mostrat[oaie]|lasciat[oaie]|apert[oaie])\b"
ON_SCREEN = re.compile(
    # "ho aperto la scheda sul telefono" counts; "vedi Marco al telefono" (a call) never does: the
    # screen is named after sul/nello…, never "al telefono"
    # to them ("ti ho mandato l'indirizzo sul telefono"), any object is the claim
    rf"\b(?:ti\s+|te\s+l[aoei]?\s*['\u2019]?\s*)(?:ho|li ho|le ho)\s+{_PUT}[^.!?]*?(?:{_ON_PHONE})|"
    rf"\b(?:l['\u2019]\s*|gliel[aoei]\s*['\u2019]?\s*)?(?:ho|li ho|le ho)\s+{_PUT}"
    rf"(?:[^.!?]*?{_SHOWN}[^.!?]*?)?\s*(?:{_ON_PHONE})|"
    rf"\bte li ho messi\b[^.!?]*?(?:{_ON_PHONE})|"
    rf"\b(?:trovi|vedi|ecco|guarda|controlla|apri)\b[^.!?]*?(?:{_ON_PHONE})|"
    # "Ecco il link." says it is on the screen as much as "è sul telefono"
    rf"\becco\s+(?:il|la|lo|i|le|gli|l['\u2019])\s*{_SHOWN}|"
    rf"{_SHOWN}[^.!?]*?\b(?:è|sono)\b[^.!?]*?(?:{_ON_PHONE})|"
    rf"\b(?:c['\u2019]è|ci sono)\b[^.!?]*?{_SHOWN}[^.!?]*?(?:{_ON_PHONE})|"
    rf"(?:{_ON_PHONE})\s*,?\s*(?:trovi|vedi|c['\u2019]è|ci sono|hai)\b|"
    r"\bguarda\s+(?:il\s+|lo\s+)?(?:tuo\s+)?(?:telefono|schermo|cellulare|iphone)\b|"
    r"\b(?:on|to|at) your (?:phone|screen|iphone)\b|\b(?:on|to) the screen\b|\bcheck your (?:phone|screen)\b|"
    r"\bi'?ve put\b",
    re.IGNORECASE,
)
# A clause inside a sentence ("Non hai il dentista domani, ti ho messo il link sul telefono."): the claim is
# cut, the answer kept (the real model wrote one sentence and dropped its answer too).
# never ":" between digits ("15:30"), nor "," ("21,99 €" came out "21,
# 99 €"); "e"/"and" only before a clause with its own verb ("Te l'ho mandato sul telefono e chiude alle 19";
# never "il link e la mappa sul telefono")
_OWN_VERB = (
    r"\b(?:è|e'|era|sarà|sono|c['\u2019]è|ci sono|ha|hai|ho|hanno|abbiamo|va|vai|può|puoi|devi|deve|serve|"
    r"servono|"
    r"chiude|chiudono|apre|aprono|inizia|comincia|finisce|parte|partono|arriva|costa|costano|dura|piove|fa|"
    r"trovi|vedi|ti|te|is|are|was|were|has|have|had|will|can|opens|closes|starts|ends|costs|it['\u2019]s|"
    r"there['\u2019]s|you['\u2019]ll|i['\u2019]ve)\b"
)
_CLAUSE_SPLIT = re.compile(
    rf"\s*(?:;|(?<!\d),|,(?!\d)|(?<!\d):(?!\d)|\s+(?:ma|but)\s+|\s+(?:e|and)\s+(?=[^,;]*?{_OWN_VERB}))\s*",
    re.IGNORECASE,
)
# a lead word alone is no answer ("Ecco." once the claim is out)
LEAD_ONLY = re.compile(
    r"^\W*(?:(?:ecco|ok|okay|allora|certo|perfetto|bene|va bene|s[iì]|here|there|sure|alright|right)\W*)*$",
    re.IGNORECASE,
)


def split_screen_claim(sentence: str) -> tuple[str, str]:
    """(the sentence without its "it's on your phone" clauses, the claim); ("", sentence) when it is all
    claim."""
    if not ON_SCREEN.search(sentence):
        return sentence, ""
    end = sentence.rstrip()[-1:] if sentence.rstrip()[-1:] in ".!?…" else "."
    body = sentence.strip().rstrip(".!?…")
    parts = [p for p in _CLAUSE_SPLIT.split(body) if p.strip()]
    kept = [p for p in parts if not ON_SCREEN.search(p)]
    if not kept or len(kept) == len(parts) or LEAD_ONLY.match(" ".join(kept)):  # "Ecco, …"
        return "", sentence.strip()
    claim = " ".join(p for p in parts if ON_SCREEN.search(p))
    text = ", ".join(kept)
    # the claim is a
    # statement, never the question's "?"
    return text[:1].upper() + text[1:] + end + (" " if sentence.endswith(" ") else ""), claim[
        :1
    ].upper() + claim[1:] + ("." if end == "?" else end)


NOT_SHOWN = {
    "it-IT": "Non sono riuscito a mostrartelo sul telefono.",
    "en-US": "I couldn't show it on your phone.",
}


# --- "done" only from records, and what really happened when there's none -----------------------------------

# The present tense Pisa uses for a change it is making, to its participle ("li sposto" → "spostato")
_PARTICIPLE_OF = {
    "sposto": "spostato", "metto": "messo", "aggiungo": "aggiunto", "segno": "segnato",
    "cancello": "cancellato", "annullo": "annullato", "creo": "creato", "mando": "mandato",
    "invio": "inviato", "tolgo": "tolto",
    "elimino": "eliminato", "fisso": "fissato", "prenoto": "prenotato", "modifico": "modificato",
    "inserisco": "inserito", "sistemo": "sistemato", "aggiorno": "aggiornato",
}  # fmt: skip
_EN_PARTICIPLE = {
    "moving": "moved", "adding": "added", "putting": "put", "cancelling": "cancelled",
    "canceling": "canceled", "creating": "created", "sending": "sent", "booking": "booked",
    "scheduling": "scheduled",
    "removing": "removed", "deleting": "deleted", "changing": "changed", "move": "moved", "add": "added",
    "put": "put", "cancel": "cancelled", "create": "created", "send": "sent", "book": "booked",
    "schedule": "scheduled", "remove": "removed", "delete": "deleted", "change": "changed",
}  # fmt: skip
_IT_VERB = re.compile(rf"\b({_PARTICIPLES}|{_PRESENT})\b")
_EN_VERB = re.compile(
    rf"\b({_EN_DONE}|{_EN_DOING}|move|add|cancel|create|send|book|schedule|remove|delete)\b"
)


# a participle after "con" and a noun with its article describes the noun
# ("con i dati aggiornati"); it names no change Pisa claims. "Fatto: la palestra spostata alle 17" still does.
_DESCRIBES = re.compile(r"\bcon\s+(?:il|lo|la|i|gli|le|l'|un|una|uno)\s*\w+\s+$")


def verb_of(sentence: str, lang: str) -> str | None:
    """The change a sentence claims, as a participle ("spostato", "mandata"; "moved"), or None."""
    plain = _plain(sentence)
    if lang == "it-IT":
        found = next((m for m in _IT_VERB.finditer(plain) if not _DESCRIBES.search(plain[: m.start()])), None)
        if found is None:
            return None
        word = found.group(1)
        after = plain[found.end() :].split()[:1] or [""]
        # the object after it names the gender: "ho mandato la mail" → "mandata", "sposto le cene" →
        # "spostate"
        by_object = {"la": "a", "una": "a", "le": "e", "i": "i", "gli": "i", "il": "o", "lo": "o", "un": "o"}
        if word in _PARTICIPLE_OF:  # "li sposto" → "spostati", "la mando" → "mandata"
            verb = _PARTICIPLE_OF[word]
            before = plain[: found.start()].split()[-1:] or [""]
            ending = {"li": "i", "le": "e", "la": "a"}.get(before[0].replace("te", "").strip() or before[0])
            ending = ending or by_object.get(after[0])
            return verb[:-1] + ending if ending else verb
        if word.endswith("o") and (ending := by_object.get(after[0])) is not None:
            return word[:-1] + ending
        return word
    found = _EN_VERB.search(plain)
    return None if found is None else _EN_PARTICIPLE.get(found.group(1), found.group(1))


_WHY = {
    "it-IT": {
        "waiting": "aspetta il tuo sì",
        "failed": "non ci sono riuscito",
        "none": "non ho cambiato niente",
        "unverified": "non riesco a verificarlo",
    },
    "en-US": {
        "waiting": "it waits for your yes",
        "failed": "it didn't work",
        "none": "nothing has changed",
        "unverified": "I can't verify it",
    },
}


def truth_for(
    sentence: str,
    lang: str,
    *,
    waiting: bool,
    failed: bool,
    changed: bool = False,
    unverified: bool = False,
) -> str:
    """What really happened, in place of a claim the record doesn't back: the claimed verb, negated, and why
    ("Non l'ho spostato: aspetta il tuo sì.", "Non l'ho mandata: non ci sono riuscito."); never dropped
    silently. Without a verb to name, the general line (`truth`). `changed`: the turn made another change
    ("Lo metto in lista prima di partire" after the country was set): this one
    isn't done yet, and "nothing has changed" would be false. `unverified`: it ran, but the
    provider's read-back didn't confirm it ("Non posso dirti che l'ho spostato: non riesco a
    verificarlo.")."""
    if team_claim(sentence) is not None:
        return TEAM_TRUTH.get(lang, TEAM_TRUTH["en-US"])
    verb = verb_of(sentence, lang)
    if unverified and not waiting and not failed:
        why = _WHY.get(lang, _WHY["en-US"])["unverified"]
        if verb is None:
            return f"{why[:1].upper()}{why[1:]}."
        if lang == "it-IT":
            pronoun = "li " if verb.endswith("i") else "le " if verb.endswith("e") else "l'"
            return f"Non posso dirti che {pronoun}ho {verb}: {why}."
        return f"I can't confirm it was {verb}: {why}."
    if changed and not waiting and not failed:
        if verb is None:
            return "Questo non l'ho ancora fatto." if lang == "it-IT" else "That I haven't done yet."
        if lang == "it-IT":
            pronoun = "li " if verb.endswith("i") else "le " if verb.endswith("e") else "l'"
            return f"Non {pronoun}ho ancora {verb}."
        return f"I haven't {verb} it yet."
    why = _WHY.get(lang, _WHY["en-US"])["waiting" if waiting else "failed" if failed else "none"]
    if verb is None:
        return truth(lang, waiting=waiting) if not failed else f"{why[:1].upper()}{why[1:]}."
    if lang == "it-IT":  # the pronoun agrees with the participle ("le mail" → "Non le ho mandate")
        if verb.endswith("i"):
            return f"Non li ho {verb}: {why}."
        if verb.endswith("e"):
            return f"Non le ho {verb}: {why}."
        return f"Non l'ho {verb}: {why}."
    return f"I haven't {verb} it: {why}."


# The reply's own words that a change wasn't (or can't be) made: a change verb under a negation or an
# inability ("I couldn't add lunch with Sara", "Non riesco ad aggiungerlo", "Non posso metterlo in
# calendario", "It wasn't added"); each verb named by the participle `tools_for` reads
_NOT_DONE_EN = re.compile(
    r"\b(?:couldn't|could\s+not|can't|cannot|can\s+not|wasn't\s+able\s+to|was\s+unable\s+to|unable\s+to|"
    r"not\s+able\s+to|failed\s+to|haven't|have\s+not|didn't|did\s+not|wasn't|was\s+not|isn't|is\s+not)\s+"
    r"(?:\w+\s+){0,2}?(?:been\s+)?"
    r"(add|added|put|move|moved|create|created|schedule|scheduled|book|booked|cancel|cancell?ed|remove|"
    r"removed|delete|deleted|send|sent|change|changed|update|updated)\b"
)
_NOT_DONE_IT = re.compile(
    r"\bnon\s+(?:\S+\s+){0,3}?"
    r"(aggiung|aggiunt|mett|mess[oaie]\b|spost|crea|inser|segn|cancell|elimin|togl|tolt|mand|invi|fiss|"
    r"prenot|modific|aggiorn|annull|programm|sistem)\w*"
)
_IT_STEM = {
    "aggiung": "aggiunto", "aggiunt": "aggiunto", "mett": "messo", "mess": "messo", "spost": "spostato",
    "crea": "creato", "inser": "inserito", "segn": "segnato", "cancell": "cancellato", "elimin": "eliminato",
    "togl": "tolto", "tolt": "tolto", "mand": "mandato", "invi": "inviato", "fiss": "fissato",
    "prenot": "prenotato", "modific": "modificato", "aggiorn": "aggiornato", "annull": "annullato",
    "programm": "programmato", "sistem": "sistemato",
}  # fmt: skip


def _kinds(verb: str | None) -> set[int]:
    """The kinds of change a participle names (moved, added, removed, sent, noted), by `_TOOLS_OF`'s rows."""
    return set() if verb is None else {n for n, (pattern, _) in enumerate(_TOOLS_OF) if pattern.match(verb)}


def says_not_done(sentence: str, reply: str, lang: str) -> bool:
    """The reply already says the change the sentence claims wasn't made ("I couldn't add lunch with
    Sara…" beside "Connect Google and I'll put it on your calendar"): the rail's line for that claim
    ("I haven't put it: it didn't work.") would only repeat it. The same kind of change (an add, a move…);
    a claim naming none is matched by any change said as not made."""
    if team_claim(sentence) is not None:
        return False  # the team's note has its own line (how to ask for it)
    claimed = _kinds(verb_of(sentence, lang))
    plain = _plain(reply)
    found = [(m.group(1), _IT_STEM) for m in _NOT_DONE_IT.finditer(plain)]
    found += [(m.group(1), {}) for m in _NOT_DONE_EN.finditer(plain)]
    for word, stems in found:
        stem = next((s for s in stems if word.startswith(s)), None)
        said = _kinds(stems[stem] if stem is not None else _EN_PARTICIPLE.get(word, word))
        if said and (not claimed or claimed & said):
            return True
    return False


def failure(tool: str, result: str) -> str | None:
    """A tool's result that says the thing wasn't done ("Not changed: …", Pisa's own error status), for the
    turn's record; None when it was done or isn't a write's."""
    head = result.lstrip()
    if head.startswith("Not "):
        return f"{tool}: {head.splitlines()[0][:160]}"
    if '"status": "error"' in head:
        return f"{tool}: error"
    return None


ANY = "*"  # a change Pisa's record holds without naming its tool (one made by code, Pisa's record read back)

# the tools that make the change a claim's verb names ("spostata" is a move, never an add)
_MOVES = frozenset({
    "move_event", "reschedule_task", "apply_option", "plan_stop", "replan", "undo_last_change", "rename_task",
    "set_task_duration", "set_task_notes", "set_task_priority", "set_task_place", "set_task_repeat",
    "tasks_bulk", "answer_proposal",
})  # fmt: skip
_ADDS = frozenset({
    "create_event", "add_task", "apply_option", "plan_stop", "remember_to", "tasks_bulk", "answer_proposal",
})  # fmt: skip
_REMOVES = frozenset({
    "cancel_event", "delete_task", "dismiss_task", "tasks_bulk", "skip_item", "undo_last_change",
    "answer_proposal",
})  # fmt: skip
# "Segnato: la raccomandata si ritira solo in Italia" after
# set_task_country was rewritten "Non l'ho segnato": what Pisa notes on a task (its country, deadline, place…)
# is marked too. A claim still needs its write in the turn.
_NOTED = frozenset({
    "complete_task", "mark_done", "add_task", "tasks_bulk", "set_task_country", "set_task_deadline",
    "set_task_duration", "set_task_notes", "set_task_place", "set_task_priority", "set_task_repeat",
    "set_task_start",
})  # fmt: skip
_TOOLS_OF: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (re.compile(r"^(?:spostat|modificat|aggiornat|sistemat|moved|changed|updated|rescheduled)"), _MOVES),
    (re.compile(r"^(?:aggiunt|mess|creat|inserit|fissat|programmat|prenotat|added|put|created|scheduled|"
                r"booked)"), _ADDS),
    (re.compile(r"^(?:cancellat|annullat|tolt|eliminat|removed|deleted|cancell?ed)"), _REMOVES),
    (re.compile(r"^(?:mandat|inviat|sent)"), frozenset({"send_draft", "compose_message"})),
    (re.compile(r"^segnat"), _NOTED),
)  # fmt: skip


def tools_for(sentence: str, lang: str) -> frozenset[str] | None:
    """The tools that make the change the sentence claims; None when it names no change ("Fatto.")."""
    if team_claim(sentence) is not None:
        return frozenset({TEAM_TOOL})  # only the team's note backs it
    verb = verb_of(sentence, lang)
    if verb is None:
        return None
    found = [tools for pattern, tools in _TOOLS_OF if pattern.match(verb)]
    return frozenset().union(*found) if found else None


@dataclass(frozen=True)
class Record:
    """What code's record holds for a turn's claims ."""

    done: frozenset[str] = frozenset()  # the tools whose change was done in this turn (ANY: made by code)
    failed: frozenset[str] = frozenset()  # the tools whose change failed in this turn and stayed failed
    lately: frozenset[str] = frozenset()  # changes Pisa made for this device in the last 2 h (ANY: read back)
    blocked: bool = False  # this turn asked for a yes or a change failed: the past never backs a claim then
    # they asked what was done ("l'hai fatto?"): a bare "Fatto" may then be about the past
    asked: bool = False
    # outward acts that ran but whose provider read-back didn't confirm them: they back no claim
    unverified: frozenset[str] = frozenset()


def _matches(tools: frozenset[str] | None, names: frozenset[str]) -> bool:
    return bool(names) if tools is None or ANY in names else bool(tools & names)


def failed_for(sentence: str, lang: str, record: Record) -> bool:
    """The claim's own change failed in this turn (never any failure of the turn)."""
    tools = tools_for(sentence, lang)
    return bool(record.failed) if tools is None else bool(tools & record.failed)


def unverified_for(sentence: str, lang: str, record: Record) -> bool:
    """The claim's own act ran but its read-back didn't confirm it."""
    tools = tools_for(sentence, lang)
    return bool(record.unverified) if tools is None else bool(tools & record.unverified)


def backed(sentence: str, lang: str, record: Record) -> bool:
    """RAIL: a claim is backed by code's record of the same change. Making
    one now ("li sposto") needs it done in this turn; one said as made ("l'ho spostata", "yes, I added it")
    in this turn or, when this turn asked no yes and nothing in it failed, in the recent turns' log. A
    failure of the same change in this turn backs nothing ("ho spostato A e B" with A's move failed)."""
    said = claim(sentence)
    if said is None:
        return True
    tools = tools_for(sentence, lang)
    failed = failed_for(sentence, lang, record)
    # RAIL: "fatto" only on a read-back that confirms it (never the past). The merge rule:
    # another act confirmed in the turn (`ANY`) never backs this one, nor "Fatto"
    if unverified_for(sentence, lang, record) and not (
        tools is not None and _matches(tools, record.done - {ANY})
    ):
        return False
    if not failed and _matches(tools, record.done):
        return True
    # a bare "Fatto" names no change; it is about the past only when they asked
    past = tools is not None or record.asked
    return said == "done" and past and not failed and not record.blocked and _matches(tools, record.lately)


# A change Pisa only proposes: `plan_task` puts the task in Pisa's plan for their yes;
# "Ho messo il ritiro alle 10" over it is said as the proposal it is
# every tool that only proposes or prepares (their yes, their tap or their Face ID does it): a hunch
# asked once, a claim letter they send, a cancel page or a bill they contest, a skill to switch on, a merge
# they tap, a button they open. Never a done change, whatever its words say.
PROPOSES = effects.of("proposal")  # the manifest's proposals
_PROPOSAL = {
    "it-IT": "Ti propongo «{title}» alle {at}: va bene?",
    "en-US": "I suggest «{title}» at {at}: does that work?",
}


def proposal_truth(lang: str, title: str, at: str) -> str:
    return _PROPOSAL.get(lang, _PROPOSAL["en-US"]).format(title=title, at=at)


_SENTENCES = re.compile(r"[^.!?…]+[.!?…]*")


def unbacked(text: str, lang: str, record: Record) -> list[str]:
    """Every sentence of a reply that claims a change the record doesn't back (the replay
    checks each final reply with it; the rail rewrites them before they are said)."""
    out: list[str] = []
    sentences = [s.strip() for s in _SENTENCES.findall(text)]
    for n, sentence in enumerate(sentences):
        if waits_for_yes(sentence, sentences[n + 1 :]):
            continue  # an act the next sentence asks their yes for is a proposal
        if sentence and not sentence.endswith("?") and not backed(sentence, lang, record):
            out.append(sentence)
    return out


_ASKS_DONE = re.compile(
    r"\b(?:l'hai|li hai|le hai|hai gia|hai davvero|hai fatto|hai messo|hai spostato|hai aggiunto|hai mandato|"
    r"have you|did you)\b",
    re.IGNORECASE,
)


def asks_what_was_done(words: str) -> bool:
    """The user asks whether Pisa did something ("l'hai messo davvero?", "did you add it?")."""
    plain = strip_marks(words.replace("\u2019", "'"))
    return "?" in words and bool(_ASKS_DONE.search(plain))


# --- what is (or isn't) on their lists: said only after reading them this turn ------------------

# Their lists change outside the conversation (a tick in the app, another device): "Nothing on your list
# matches milk" said from the conversation's memory, with "Buy milk" open, is a claim about the store with no
# read behind it. The tools that read the lists (tasks, the plan, intentions, place notes), or find in them
# what their words name (complete_task, mark_done and every task edit answer "no open task matches" from the
# store itself).
STATE_READS = frozenset(
    {"get_tasks", "get_agenda", "get_plan", "what_now", "get_briefing", "low_priority_list", "tasks_bulk",
     "complete_task", "mark_done", "my_intentions", "list_place_notes", "delete_task", "dismiss_task",
     "restore_task", "rename_task", "plan_task", "reschedule_task", "set_task_country", "set_task_deadline",
     "set_task_duration", "set_task_notes", "set_task_place", "set_task_priority", "set_task_start",
     "set_task_repeat"}
)  # fmt: skip
_LIST_EN = r"(?:list|lists|tasks|to-?dos?|reminders)"
_LIST_IT = r"(?:lista|liste|elenco|cose\s+da\s+fare|promemoria)"
_STATE = re.compile(
    # already there: "It's already on your list", "I already have it", "That's already done"
    rf"\balready\s+(?:on|in)\s+(?:your|the)\s+(?:\w+\s+)?{_LIST_EN}\b"
    r"|\b(?:it's|it\s+is|that's|that\s+is|they're|they\s+are|is|are|was|were)\s+already\s+(?:there|listed|done|"
    r"ticked|checked|marked|completed|crossed|added|on\s+it|in\s+it)\b"
    r"|\bi(?:'ve|\s+have)?\s+already\s+(?:have|had|got|added|put|listed|noted|ticked|marked)\b"
    # not there: "Nothing on your list matches milk", "Milk isn't on your list", "There's no task called …"
    rf"|\b(?:not|isn't|aren't|wasn't|weren't|nothing|no|none)\s+(?:\w+\s+){{0,4}}(?:on|in)\s+your\s+(?:\w+\s+)?"
    rf"{_LIST_EN}\b"
    r"|\bno\s+(?:open\s+)?(?:tasks?|items?|reminders?|to-?dos?)\s+"
    r"(?:match(?:es)?|called|named|like|for)\b"
    rf"|\b(?:can't|cannot|couldn't|don't|do\s+not)\s+(?:find|see)\b[^.!?]{{0,60}}\b{_LIST_EN}\b"
    # it: "Il passaporto è già in lista", "Ce l'ho già", "È già segnato", "Non è nella tua lista",
    # "Niente nella tua lista corrisponde"
    rf"|\bgia\s+(?:in|nella|sulla|nel|tra\s+le)\s+(?:tua\s+|tue\s+)?{_LIST_IT}\b"
    r"|\b(?:e|sono|e\s+stat[oaie]|sono\s+stat[ie])\s+gia\s+(?:li|presente|present[ie]|segnat[oaie]|fatt[oaie]|"
    r"complet\w+|spuntat[oaie]|aggiunt[oaie]|inserit[oaie]|mess[oaie]|chius[oaie])\b"
    r"|\bce\s+l'ho\s+gia\b|\bc'e\s+gia\b|\bci\s+sono\s+gia\b"
    r"|\b(?:l'|li\s+|le\s+)?ho\s+gia\s+(?:aggiunt|segnat|mess|inserit|spuntat|annotat)\w*"
    rf"|\bnon\s+(?:e|c'e|sono|ci\s+sono|risulta|risultano|trovo|vedo|ho)\s+(?:\w+\s+){{0,4}}(?:in|nella|sulla|nel|"
    rf"tra\s+le)\s+(?:tua\s+|tue\s+)?{_LIST_IT}\b"
    rf"|\b(?:niente|nulla|nessun\w*)\s+(?:\w+\s+){{0,4}}(?:in|nella|sulla|nel|tra\s+le)\s+(?:tua\s+|tue\s+)?"
    rf"{_LIST_IT}\b"
    rf"|\b(?:nella|in|sulla)\s+(?:tua\s+)?{_LIST_IT}\s+non\b"
    r"|\bnessun[ao]?\s+(?:attivita|compito|cosa\s+da\s+fare|promemoria)\s+(?:\w+\s+){0,2}corrispond\w*"
)
# The writes to their lists: each reads the store as it writes (add_task answers "already on the list"
# from it, complete_task "already done"), so a turn that made one has touched the lists.
LIST_WRITES = frozenset(
    {"add_task", "keep_intention", "remember_to", "remember_at_place", "answer_intention", "resume_intention",
     "confirm_intention", "close_intention", "tasks_bulk"}
)  # fmt: skip
# app_help: a turn about Pisa's own screens names them ("Attività", "Promemoria") from its help, not their
# lists
LIST_TOUCH = STATE_READS | LIST_WRITES | {"app_help"}
# the reflex's reads (`domain.state.Fact.read`) that read their lists for these words: code's read, a read too
CODE_LIST_READS = frozenset({"tasks", "plan", "agenda", "waiting"})
# A sentence about their lists or an item's state on them (the structural rule's trigger):
# a list word, or a state word about an item ("già fatto", "è segnata come pagata", "risulta chiusa",
# "already done", "marked as paid"). Plain on purpose: a false positive costs one read round, never a wrong
# word.
_LIST_WORD = re.compile(
    r"\b(?:lists?|to-?dos?|reminders?|tasks?|lista|liste|elenco|elenchi|promemoria|attivita|cose\s+da\s+fare)\b"
)
_ITEM_STATE = re.compile(
    r"\balready\s+(?:done|ticked|checked|marked|set|there|added|completed|paid|closed|listed|on|in|have|got|"
    r"kept|noted|saved)\b|\bmarked\s+(?:as|done|paid|complete)\b|\bticked\s+off\b"
    r"|\bgia\s+(?:fatt|segnat|impostat|chius|aggiunt|mess|complet|spuntat|pagat|annotat|salvat|tenut|"
    r"inserit|present|in\b|nella\b|li\b)\w*"
    r"|\bsegnat[oaie]\s+come\b|\brisult(?:a|ano)\b|\bce\s+l'ho\s+gia\b|\bc'e\s+gia\b"
)


# the state words themselves and the little words: never what names the item
_NOT_A_NAME = frozenset(
    "gia fatto fatta fatti fatte segnato segnata segnati segnate come risulta risultano stato stata pagato "
    "pagata chiuso chiusa impostato impostata della dello delle degli alle sono already done marked ticked "
    "with that this have from your their there".split()
)


def _names(plain: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", plain) if w not in _NOT_A_NAME}


def refers_to_lists(sentence: str, grounded: str = "") -> bool:
    """The sentence refers to their lists or an item's state on them (a question asks, never states).
    Narrowed: a state word with no list word, about an item this turn's results name (`grounded`: "Pisa ha
    già segnato il volo AZ5287" after get_trips), is about that read, not their lists."""
    plain = _plain(sentence)
    if plain.rstrip().endswith("?"):
        return False
    if _LIST_WORD.search(plain):
        return True
    if not (_ITEM_STATE.search(plain) or _STATE.search(plain)):
        return False
    return not (grounded and _names(plain) & _names(_plain(grounded)))


# Their day: what is planned (or that nothing is), what they have to do today. "Non hai niente in programma
# oggi", "You have nothing planned for today", "Oggi hai: latte, pane e uova" state their lists and plan as
# much as "it's on your list" does, with no list word in them.
_DAY_STATE = re.compile(
    r"\b(?:nothing|no\s+(?:tasks?|plans?|events?|appointments?))\s+(?:else\s+)?(?:\w+\s+){0,2}"
    r"(?:planned|scheduled|due|to\s+do|on|left)\b[^.!?]{0,30}\b(?:today|tonight|this\s+(?:morning|afternoon|"
    r"evening))\b"
    r"|\b(?:planned|scheduled|due|to\s+do)\s+(?:for\s+)?(?:today|tonight)\b"
    r"|\b(?:today|tonight|this\s+(?:morning|afternoon|evening))\s*,?\s+(?:you\s+(?:have|'ve\s+got|need\s+to|"
    r"should|could|must)|you're\s+free|you\s+are\s+free|is\s+(?:free|clear|empty))\b"
    r"|\byou\s+(?:have|'ve\s+got|got)\s+(?:nothing|no\s+\w+|\w+\s+(?:things?|tasks?))\s+(?:\w+\s+){0,2}"
    r"(?:today|planned|scheduled|due)\b"
    r"|\b(?:your|the)\s+(?:plan|day|agenda|schedule)\s+(?:for\s+)?today\b|\byour\s+day\s+is\s+(?:free|clear|"
    r"empty|open)\b"
    r"|\b(?:oggi|stamattina|stasera|oggi\s+pomeriggio|per\s+oggi)\s*,?\s+(?:non\s+)?(?:hai|ti\s+(?:aspetta|"
    r"aspettano|resta|restano)|devi|dovresti|potresti|sei\s+liber[oa])\b(?!\s+\w+(?:at|ut|it)[oaie]\b)"
    r"|\b(?:non\s+hai|niente|nulla|nessun\w*)\s+(?:\w+\s+){0,3}(?:in\s+programma|da\s+fare|in\s+agenda|"
    r"di\s+previsto|in\s+piano|pianificat\w*|programmat\w*)\b[^.!?]{0,30}\b(?:oggi|stamattina|stasera)\b"
    r"|\b(?:in\s+programma|da\s+fare|previst[oaie]|pianificat[oaie])\s+(?:per\s+)?oggi\b"
    r"|\b(?:piano|programma|giornata|agenda)\s+(?:di|per)\s+oggi\b|\bgiornata\s+libera\b"
)
# Their words ask what their day holds or what to do now: every sentence of the answer states their plan
# "now", "today", "next" or nothing, then the end: "What should I do in Lisbon?" asks something else, and
# "che faccio stasera?" may ask for an idea for the evening
_WHEN_NOW = r"(?:\s+(?:(?:right\s+)?now|today|next|first|adesso|ora|oggi))?\s*[?.!]*\s*$"
_ASKS_DAY = re.compile(
    rf"\bwhat\s+(?:should|do|can|must|shall)\s+i\s+do{_WHEN_NOW}"
    r"|\bwhat(?:'s|\s+is)\s+(?:on\s+)?(?:my\s+)?(?:plan|agenda|schedule)\b(?:\s+(?:for\s+)?today)?\s*[?.!]*\s*$"
    r"|\bwhat(?:'s|\s+is)\s+(?:on\s+|for\s+)today\b|\bwhat(?:'s|\s+is)\s+my\s+day\b"
    r"|\bwhat\s+do\s+i\s+have\s+(?:to\s+do\s+)?(?:today|on\s+today|now|planned|next)\b"
    r"|\b(?:anything|something)\s+(?:planned|scheduled|to\s+do)\s+(?:for\s+)?today\b"
    rf"|\b(?:cosa|che|che\s+cosa)\s+(?:devo|dovrei|posso|ho\s+da)\s+fare{_WHEN_NOW}"
    rf"|\b(?:cosa|che|che\s+cosa)\s+faccio{_WHEN_NOW}"
    r"|\b(?:cosa|che|che\s+cosa)\s+ho\s+(?:oggi|in\s+programma|da\s+fare|in\s+agenda|dopo)\b"
    r"|\b(?:il\s+)?(?:programma|piano)\s+(?:di|per)\s+oggi\b|\bche\s+(?:c'e|ce)\s+oggi\b"
)


def asks_day(words: str) -> bool:
    """Their words ask what their day holds or what to do now ("Cosa devo fare oggi?", "What should I do
    now?"): the answer can only come from their lists and plan as they are."""
    return bool(_ASKS_DAY.search(_plain(words)))


def states_day(sentence: str) -> bool:
    """The sentence says what their day holds, or that it holds nothing (a question asks, never states)."""
    plain = _plain(sentence)
    return not plain.rstrip().endswith("?") and bool(_DAY_STATE.search(plain))


UNREAD_TRUTH = {
    "it-IT": "Non ho riletto la tua lista adesso, quindi non posso dirti se c'è.",
    "en-US": "I haven't checked your list just now, so I can't say whether it's there.",
}
UNREAD_DAY_TRUTH = {
    "it-IT": "Non ho riletto la tua giornata adesso, quindi non posso dirti cosa c'è in programma.",
    "en-US": "I haven't checked your day just now, so I can't say what's planned.",
}


def unread_state(sentence: str, read: bool, grounded: str = "", *, day: bool = False) -> str | None:
    """RAIL: in a turn that touched no list (`read` false: no call of `LIST_TOUCH`),
    any sentence that refers to their lists or an item's state on them waits for a read (None: it may be
    said). Not a list of false wordings (they kept slipping through: "I don't have milk on your list",
    "risulta già chiusa"): the turn's own record decides. A false positive costs one extra read round,
    which is fine. Their day is their lists too: a sentence that says what today holds (or that it holds
    nothing), and, when their words ask what the day holds (`day`), every sentence of the answer."""
    if read:
        return None
    if day and not _plain(sentence).rstrip().endswith("?") and _names(_plain(sentence)):
        return "answers what their day holds, and no list or plan was read this turn"
    if states_day(sentence):
        return "says what their day holds, and no list or plan was read this turn"
    if not refers_to_lists(sentence, grounded):
        return None
    return "refers to their lists, and no list was read or written this turn"


def unread_truth(lang: str, *, day: bool = False) -> str:
    table = UNREAD_DAY_TRUTH if day else UNREAD_TRUTH
    return table.get(lang, table["en-US"])


# --- only their words reach them
# -------------------------------------------------------------------------------

# Pisa's own notes about its rails, tools and screen, which the model sometimes says as if they were for them:
# "Non è in lista: ignoralo.", "la parola 'conferma' nell'etichetta è vietata", "Non sono riuscito
# a mostrarti la card di Vico sul telefono". A sentence like these is never said: it goes back to the
# model to rewrite (`conversation`), once, then it is left out.
_INTERNAL = (
    (
        # "non è in lista" alone is a fair answer about their own list ("Il latte non è in lista"),
        # and "ignoralo" alone is fair advice (an SMS that asks to pay): only the two together are Pisa's note
        re.compile(
            r"\bnon\s+(?:e|e')\s+(?:in|nella)\s+(?:lista|elenco)\b[^.!?]{0,30}\bignoral[oaie]\b"
            r"|\bignoral[oaie]\b[^.!?]{0,30}\bnon\s+(?:e|e')\s+(?:in|nella)\s+(?:lista|elenco)\b"
            r"|\bnot\s+(?:in|on)\s+the\s+list\b[^.!?]{0,30}\bignore\s+(?:it|this|that)\b"
            r"|\bignore\s+(?:it|this|that)\b[^.!?]{0,30}\bnot\s+(?:in|on)\s+the\s+list\b"
        ),
        "a note about what Pisa shows, not words for them",
    ),
    (
        # only about a drawing ("Champagne sull'etichetta è vietata" is an answer about wine)
        re.compile(
            r"(?=[^.!?]*\b(?:disegn\w*|drawing|diagramm?\w*|schema|nodo|nodi|node|canvas)\b)"
            r"(?:[^.!?]*\b(?:parol[ae]|etichett\w*|word|label)\b[^.!?]{0,60}\b(?:vietat\w*|proibit\w*|forbidden|"
            r"not\s+allowed|non\s+(?:e|e')\s+passat\w*)|[^.!?]*\b(?:vietat\w*|proibit\w*|forbidden|not\s+allowed)\b"
            r"[^.!?]{0,40}\b(?:parol[ae]|etichett\w*|word|label|disegno|drawing)\b)"
        ),
        "a note about Pisa's own drawing rules",
    ),
    (
        re.compile(
            # "Non sono riuscito a metterti la sveglia sul telefono" is a failed change they must
            # hear: only showing counts as Pisa's plumbing; putting, only a card or a table
            r"\bnon\s+sono\s+riuscit[oa]\s+a\s+(?:mostrar|far(?:ti|gli|le)?\s+veder)\w*[^.!?]*"
            r"(?:telefono|schermo|card|scheda|tabella|carta)|\bnon\s+sono\s+riuscit[oa]\s+a\s+metter\w*[^.!?]*"
            r"\b(?:card|scheda|tabella)\b|\b(?:i\s+)?(?:couldn't|could\s+not|wasn't\s+able\s+to)\s+"
            r"(?:show|display)\b[^.!?]*\b(?:phone|screen|card|table)\b|\b(?:i\s+)?(?:couldn't|could\s+not|"
            r"wasn't\s+able\s+to)\s+put\b[^.!?]*\b(?:card|table)\b"
        ),
        "a note about what Pisa couldn't show on the phone",
    ),
    (
        re.compile(
            # a road's guardrail and a train's rail are their world, not Pisa's plumbing
            r"\b(?:tool\s+result|data\s+block|system\s+prompt|my\s+instructions|le\s+mie\s+istruzioni|"
            r"(?:il\s+mio|i\s+miei|my)\s+(?:guard)?rails?|nonce)\b"
        ),
        "a note about Pisa's tools or instructions",
    ),
)
# "non ho cambiato niente", said on a turn they asked no change in ("Non l'ho messo: non ho cambiato
# niente.")
_NO_CHANGE = re.compile(
    r"\bnon\s+ho\s+cambiato\s+niente\b|\bnon\s+l['\u2019]?ho\s+(?:ancora\s+)?(?:messo|messa|fatto|segnato|spostat\w)\b"
    r"|\bnothing\s+(?:has\s+)?changed\b|\bi\s+haven't\s+(?:done|changed|moved|added)\b"
)


# their words ask Pisa to act, though no move or cancel verb says so ("Mi ricordi di chiamare mamma
# alle 6?", "Remind me at 6", "Scrivi a Giulia che arrivo", "La palestra di domani va alle 20"): a change
# claimed there with none made keeps the rail's truth ("Non l'ho ancora fatto"), never left out unsaid
_ASKS_ACT = re.compile(
    r"\b(?:ricord(?:ami|amelo|armi|ati|i|a)|promemoria|sveglia|timer|scriv(?:i|imi|igli|ile)|mand(?:a|ami|agli|"
    r"ale|alo)|invia\w*|rispond(?:i|igli|ile)|prenot\w*|segn(?:a|ami|alo|ati)|annot\w*|aggiung\w*|mett(?:i|imi|"
    r"ilo|ila)|togli\w*|impost\w*|spost\w*|anticip\w*|posticip\w*|rimand\w*|cancell\w*|annull\w*|disdic\w*|"
    r"va\s+(?:alle|al|a)\s+\d+|remind\w*|reminder|alarm|send|text|write|reply|book|schedule|add|put|set|move|"
    r"cancel)\b"
)


def asks_act(words: str) -> bool:
    """Their words ask Pisa to do something to their day, their lists or their messages."""
    return bool(_ASKS_ACT.search(_plain(words)))


def internal(sentence: str, *, asked_change: bool = False) -> str | None:
    """Why a sentence is Pisa's own note rather than words for them, or None: a note about its lists, its
    drawing rules, what it couldn't show, its tools; or "nothing changed" on a turn they asked no change
    in."""
    plain = _plain(sentence)
    for pattern, why in _INTERNAL:
        if pattern.search(plain):
            return why
    if not asked_change and _NO_CHANGE.search(plain):
        return "says nothing changed, but they asked for no change"
    return None


# A sentence that corrects an earlier answer ("Una correzione: …", "Ho sbagliato …", "non avevo verificato …")
# (never Pisa owning a slip in the answer at hand, "Ho sbagliato: …" first, as the voice bible asks)
_CORRECTS = re.compile(
    r"\b(?:una\s+correzione|correggo|ti\s+correggo|non\s+(?:l['\u2019]?)?avevo\s+verificat\w*|"
    r"non\s+ho\s+verificato|nella\s+risposta\s+(?:sul|su|di|precedente)|ho\s+dato\s+[^.!?]{0,40}\bche\s+non\s+"
    r"avevo|non\s+va\s+considerat\w|non\s+fidarti|a\s+correction|correction:|i\s+hadn't\s+(?:checked|verified)|"
    r"i\s+didn't\s+(?:check|verify)|earlier\s+i\s+said|in\s+my\s+(?:last|earlier)\s+answer)\b"
)


def corrects(sentence: str) -> bool:
    """The sentence corrects (or takes back) something Pisa said before."""
    return bool(_CORRECTS.search(_plain(sentence)))


# a figure is "held" by the sources only with its unit (a price, a percentage, a distance, a time,
# a rating), or with its decimals: "12 €" is never held by "apre alle 12" on some page
_MEASURE = re.compile(
    r"(?:€\s*(?P<pre>\d+(?:[.,]\d+)?))|(?P<num>\d+(?:[.,]\d+)?)\s*(?P<unit>€|euro\b|eur\b|%|per\s*cento\b|km\b|"
    r"chilometr\w*|metri\b|m\b|minut\w*|min\b|stell\w*|stars?\b|ore\b|hours?\b|anni\b|years?\b)?"
)
_UNITS = (("€", "euro", "eur"), ("%", "per"), ("km", "chilom"), ("metri", "m"), ("minut", "min"),
          ("stell", "star"), ("ore", "hour"), ("anni", "year"))  # fmt: skip
# their words that question the answer just before ("Sei sicuro del prezzo?"): a correction may follow it
_CHALLENGE = re.compile(
    r"\b(?:sei\s+sicur[oa]|davvero|ne\s+sei\s+cert[oa]|ricontrolla|controlla|verifica|e\s+giusto|"
    r"e\s+vero|are\s+you\s+sure|really|double[-\s]?check|check\s+again|is\s+that\s+(?:right|true|correct))\b"
)


def _measures(text: str) -> set[tuple[str, str]]:
    """(value, unit) for each figure with a unit or decimals: ("7.99", "€"), ("36", "€"), ("1.5", "km")."""
    out: set[tuple[str, str]] = set()
    for m in _MEASURE.finditer(_plain(text)):
        if m.group("pre") is not None:
            out.add((decimal_point(m.group("pre")), "€"))
            continue
        value, unit = decimal_point(m.group("num")), (m.group("unit") or "").strip()
        kind = next((k[0] for k in _UNITS if unit.startswith(k)), "")
        if kind or "." in value:
            out.add((value, kind))
    return out


FOLLOW_UP_WORDS = 2  # their words with fewer content words than this follow the answer before ("e adesso?")
# … and one that takes something back ("non va considerato", "non fidarti", "non l'avevo verificato")
_RETRACTS = re.compile(
    r"\b(?:non\s+va\s+considerat\w*|non\s+(?:l['\u2019]?)?ho\s+lett\w|non\s+fidarti|non\s+(?:l['\u2019]?)?avevo\s+verificat\w*|"
    r"non\s+ho\s+verificato|non\s+(?:e|e')\s+(?:vero|corretto|giusto)|ritiro|don't\s+trust|not\s+verified|"
    r"hadn't\s+(?:checked|verified)|didn't\s+(?:check|verify)|take\s+(?:it|that)\s+back|wasn't\s+(?:read|right))\b"
)
_STOP = frozenset(
    "il lo la i gli le un una uno di del della dei degli delle da dal dalla in nel nella con su sul per tra "
    "fra e o ma che non ho hai ha sono era ero mi ti si ci se come anche piu gia ancora questo questa quello "
    "quella "
    "the a an of to in on for and or but that not i you it is was are were my your this with from as at by "
    "correzione correggo sbagliato verificato verificata considerato risposta letto letta pagina fidarti "
    "correction wrong checked verified answer said page".split()
)


def _topic(text: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[a-z0-9]+", _plain(text)) if len(w) >= 4 and w not in _STOP}


def stray_correction(sentence: str, words: str, evidence: str, previous: str = "") -> str | None:
    """Why a correction
    must not be said here, or None. It never travels to a turn about something else (it shares no word of
    weight with their words nor with Pisa's answer just before, `previous`: the one a follow-up may correct),
    and it never takes back a figure this turn's or the kept findings' evidence holds (`evidence`: their
    text)."""
    if not corrects(sentence):
        return None
    figures = _measures(sentence)
    held = _measures(evidence or "")
    if figures and _RETRACTS.search(_plain(sentence)) and figures & held:
        return "takes back a figure the sources read hold"
    # a follow-up ("e adesso?") stays on the answer before it, and so does a question about it ("Sei sicuro
    # del prezzo?"); words with a topic of their own start a new one
    follows = len(_topic(words)) < FOLLOW_UP_WORDS or bool(_CHALLENGE.search(_plain(words)))
    if not _topic(sentence) & (_topic(words) | (_topic(previous) if follows else set())):
        return "corrects an earlier answer on another topic"
    return None
