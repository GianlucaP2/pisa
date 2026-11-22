"""What Pisa knows about the user: claims from the user's own words, their confidence and
lifetime, which go into a prompt, and the first talk's script. Pure rules, no I/O.

A claim comes from what the user said: provenance `said`, or `confirmed` when the user edits it in the
app. Learning from stays and choices adds `observed` evidence to the same tables.

**Confidence** = support / (support + against + 2): said adds 1.0 (a hard limit 3.0); confirmed sets a floor
of 0.8. A claim the user said is used at once, with its confidence shown, so
the model weighs a taste said once lightly and applies a hard limit as a rule.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pisa.domain.text import fold_words

Kind = Literal[
    "like", "dislike", "routine", "constraint", "goal", "style", "mobility", "source", "sensitivity"
]
KINDS: tuple[str, ...] = (
    "like",
    "dislike",
    "routine",
    "constraint",
    "goal",
    "style",
    "mobility",
    "source",
    "sensitivity",
)
Provenance = Literal["said", "observed", "inferred", "confirmed"]
SCOPE_KEYS = {
    "day": ("weekday", "weekend"),
    "company": ("alone", "others"),
    "where": ("home", "travel"),
    "time": ("morning", "afternoon", "evening", "night"),
}
SAID_WEIGHT = 1.0
HARD_WEIGHT = 3.0
CONFIRMED_FLOOR = 0.8
CORE_MAX = 25  # claims in a prompt
STATEMENT_MAX = 200
TOPIC_MAX = 80
# How long a claim lasts without new evidence or a confirmation. None: until changed.
LIFETIME: dict[str, timedelta | None] = {
    "constraint": None,
    "goal": timedelta(days=90),
    "like": timedelta(days=365),
    "dislike": timedelta(days=365),
    "routine": timedelta(days=60),
    "mobility": timedelta(days=182),
    "style": None,
    "source": timedelta(days=365),
    "sensitivity": None,
}
# Health-adjacent, diet, belief, identity and habit words: a claim naming one is sensitive (stored only on the
# user's yes, never said aloud unprompted). Matched as prefixes of accent-free lowercase words.
# RAIL: sensitive topics are code's list, never the model's call.
SENSITIVE_STEMS = (
    # health, diet
    "allerg",
    "intoller",
    "intoler",
    "celiac",
    "celiach",
    "glutin",
    "gluten",
    "lattosio",
    "lactos",
    "diabet",
    "vegetar",
    "vegan",
    "incint",
    "pregnan",
    "salute",
    "health",
    "medic",
    "farmac",
    "malatt",
    "illness",
    "pharma",
    "cancro",
    "cancer",
    "tumor",
    "dieta",
    "diet",
    # mental health, disability
    "depress",
    "ansia",
    "ansios",
    "anxiet",
    "anxious",
    "terapi",
    "therap",
    "psicolo",
    "psychol",
    "psichiat",
    "psychiat",
    "bipolar",
    "adhd",
    "autis",
    "disabil",
    "rotelle",
    "wheelchair",
    "cieco",
    "cieca",
    "blind",
    "sordo",
    "sorda",
    "deaf",
    # belief, faiths by name
    "relig",
    "pregh",
    "pray",
    "fede",
    "faith",
    "chiesa",
    "church",
    "messa",
    "moschea",
    "mosque",
    "sinagog",
    "synagog",
    "tempio",
    "temple",
    "cattolic",
    "catholic",
    "cristian",
    "christian",
    "musulman",
    "muslim",
    "islam",
    "ebrai",
    "ebreo",
    "jewish",
    "buddh",
    "hindu",
    "induis",
    "ortodoss",
    "orthodox",
    "evangel",
    "protestan",
    "ateo",
    "atea",
    "atheis",
    "halal",
    "kosher",
    "ramadan",
    "quaresim",
    # orientation, identity
    "gay",
    "lesbic",
    "lesbian",
    "bisessual",
    "bisexual",
    "omosessual",
    "homosexual",
    "transgend",
    "queer",
    "lgbt",
    "orientam",
    # places of health, worship and politics (an observed "Gli piace «Ospedale…»")
    "ospedal",
    "clinic",
    "kliniek",
    "ambulator",
    "poliambulator",
    "fisioterap",
    "physiotherap",
    "chiropr",
    "osteopat",
    "hospital",
    "ziekenhuis",
    "huisarts",
    "tandarts",
    "apothe",
    "parrocch",
    "parish",
    "basilic",
    "cattedral",
    "cathedral",
    "santuari",
    "kerk",
    "moske",
    "partij",
    "sindacat",
    "poliklin",
    "fysio",
    "consultori",
    "diagnost",
    "oncolog",
    "neurolog",
    "urolog",
    "geova",
    "jehovah",
    "gurdwar",
    "mandir",
    # politics
    "politic",
    "partito",
    "elezion",
    "election",
    # alcohol
    "alcol",
    "alcoh",
    "astemi",
    "sobri",
    "sober",
)
# Words that carry no content of their own, in a claim's statement or the user's quote: what is matched is
# the rest (the things, places, foods and qualities).
FUNCTION_WORDS = frozenset(
    {
        "alla", "alle", "allo", "agli", "dalla", "dalle", "dallo", "dagli", "della", "delle", "dello",
        "degli", "nella", "nelle", "nello", "negli", "sulla", "sulle", "sullo", "sugli", "questo",
        "questa", "quello", "quella", "sempre", "anche", "molto", "tanto", "troppo", "quando", "dove",
        "come", "perche", "cosa", "sono", "sei", "essere", "avere", "fare", "dice", "detto", "ogni",
        "tutti", "tutto", "tutte", "niente", "nulla", "nessun", "nessuno", "nessuna", "mai", "senza",
        "ama", "amare", "adora", "vuole", "vorrebbe", "preferisce", "piace", "piacciono", "odia",
        "detesta", "mangia", "mangiare", "beve", "bere", "andare", "evita", "proprietario", "with",
        "without", "always", "never", "nothing", "every", "when", "where", "what", "that", "this",
        "these", "those", "they", "them", "their", "have", "loves", "likes", "wants", "prefers", "hates",
        "eats", "drinks", "avoids", "owner", "some", "also", "very", "really", "much", "about", "from",
        "into", "only",
    }
)  # fmt: skip
QUOTE_MIN_WORDS = 2  # their words that ground a claim (a memory, a note's rewrite): at least two
NEGATIONS = frozenset({"non", "no", "senza", "niente", "mai", "nessun", "nessuno", "nessuna", "not", "never",
                       "without", "nothing"})  # fmt: skip


# A claim's yes or no: a negation or an avoid-word in the statement must be in the quote too, and the other
# way round ("Niente pesce" is not "il pesce invece sì"; "Evita il Bistrot" is not "Com'è il Bistrot?").
AVOID_WORDS = NEGATIONS | frozenset({"evita", "evitare", "evito", "odia", "odio", "detesta", "detesto",
                                     "avoid", "avoids", "hate", "hates", "dislike", "dislikes"})  # fmt: skip
# The user's words must ask for it: forget / never learn need one of these (a word starting with it).
FORGET_CUES = ("dimentic", "scord", "cancell", "togli", "elimina", "forget", "delete", "remove", "erase")
NEVER_CUES = ("non", "mai", "never", "smetti", "basta", "stop", "dont")


def says_no(text: str) -> bool:
    return any(w in AVOID_WORDS for w in plain(text).split())


def asks(owner_words: str, cues: tuple[str, ...]) -> bool:
    """The user's own message contains one of the cues (a word starting with it)."""
    return any(w.startswith(cue) for w in plain(owner_words).replace("don t", "dont").split() for cue in cues)


def plain(text: str) -> str:
    """Lowercase, no accents, no punctuation, one space: for matching the user's words."""
    return fold_words(text)


def negated(text: str) -> bool:
    """The statement says no ("Non è vegetariano", "never runs", "don't")."""
    words = set(plain(text).replace("'", " ").split())
    return bool(words & (NEGATIONS | {"dont", "don", "doesn", "doesnt", "isn", "isnt", "won", "nope"}))


# verbs that say the opposite of each other: never one claim ("Ama correre la mattina" vs
# "Odia correre la mattina"), whatever their stems in common.
_ANTONYMS = (
    (
        frozenset({"ama", "amo", "amano", "adora", "adoro"}),
        frozenset({"odia", "odio", "odiano", "detesta", "detesto"}),
    ),
    (frozenset({"piace", "piacciono"}), frozenset({"detesta", "detesto", "detestano", "odia"})),
    (frozenset({"preferisce", "preferisco", "preferiscono"}), frozenset({"evita", "evito", "evitano"})),
    (
        frozenset({"like", "likes", "love", "loves", "prefer", "prefers"}),
        frozenset({"hate", "hates", "dislike", "dislikes", "avoid", "avoids"}),
    ),
)


def opposed(one: str, other: str) -> bool:
    """The two statements hold the two sides of an antonym pair (ama / odia, piace /
    detesta, preferisce / evita, likes / hates, loves / hates, prefers / avoids)."""
    a, b = set(plain(one).split()), set(plain(other).split())
    return any((yes & a and no & b) or (no & a and yes & b) for yes, no in _ANTONYMS)


def stems(text: str) -> set[str]:
    """The content of a text, as 5-letter stems of its words of 4 letters or more ("catene" ~ "catena")."""
    return {w[:5] for w in plain(text).split() if len(w) >= 4 and w not in FUNCTION_WORDS}


def in_words(text: str, owner_words: str) -> bool:
    """The text is, word for word, in what the user said."""
    t = plain(text)
    return bool(t) and f" {t} " in f" {plain(owner_words)} "


def ungrounded(statement: str, quote: str, owner_words: str, *, hard: bool) -> str | None:
    """Why the claim isn't the user's own words, or None. The quote is at least
    two of the user's words this turn, and says what the statement says: the statement's content words
    share their stems with it (all of them for a hard limit, at least one otherwise)."""
    if len(plain(quote).split()) < QUOTE_MIN_WORDS:
        return "the quote must be at least two of the user's own words"
    if not in_words(quote, owner_words):
        return "the quote isn't in what the user said in this message"
    said, claimed = stems(quote), stems(statement)
    if not claimed:
        return "the statement says nothing the quote says"
    if hard and not claimed <= said:
        missing = ", ".join(sorted(claimed - said))
        return f"a hard limit must say only what the user said (not in the quote: {missing})"
    if not claimed & said:
        return "the statement doesn't say what the quote says"
    if says_no(statement) != says_no(quote):
        return "the statement's yes or no isn't the user's (a negation must be in both, or in neither)"
    return None


# their "sì, ricordalo" to Pisa asking whether to keep what they just said (their routine): the whole
# message, a yes and a keep verb at most ("sì, ricordalo", "ricordatelo pure", "tienilo a mente", "yes,
# remember it")
_KEEP_IT = re.compile(
    r"^\W*(?:(?:s[iì]|ok|okay|certo|yes|sure|va\s+bene|dai)\W+)*(?:ricordal[oaie]|ricordatel[oaie]|"
    r"ricordatelo|memorizzal[oaie]|salval[oaie]|tienil[oaie]\s+a\s+mente|segnatel[oaie]|ricorda(?:telo)?|"
    r"remember\s+(?:it|that|this)|keep\s+(?:it|that)\s+in\s+mind|save\s+it)(?:\W+(?:pure|grazie|please|pisa))*\W*$",
    re.IGNORECASE,
)


def keeps_it(words: str) -> bool:
    """Their whole message only asks Pisa to keep what they said before ("sì, ricordalo")."""
    return _KEEP_IT.match(words.strip()) is not None


def is_sensitive(kind: str, statement: str) -> bool:
    words = plain(statement).split()
    return kind == "sensitivity" or any(w.startswith(stem) for w in words for stem in SENSITIVE_STEMS)


# The user's word for a kind of place (Italian, Dutch) → how map providers file it (Google's categories are
# English: "gym", "night_club"…), so an off-limits "palestra" or "sportschool" covers a place filed as `gym`
# (question 1). Each alternative is a phrase: all its words must be there.
CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    # sport, nightlife, gambling, drink and smoke
    "palestra": ("gym", "fitness"), "palestre": ("gym", "fitness"), "sportschool": ("gym", "fitness"),
    "discoteca": ("night club", "nightclub"), "discoteche": ("night club", "nightclub"),
    "discotheek": ("night club", "nightclub"), "nachtclub": ("night club", "nightclub"),
    "casino": ("casino",), "scommesse": ("casino", "betting"), "gokken": ("casino", "betting"),
    "kroeg": ("bar", "pub"), "birreria": ("pub", "brewery", "bar"), "enoteca": ("wine bar", "liquor store"),
    "alcolici": ("liquor store", "bar"), "slijterij": ("liquor store",), "tabacchi": ("tobacco",),
    "tabaccheria": ("tobacco",), "tabakszaak": ("tobacco",), "fumo": ("tobacco",),
    # health
    "ospedale": ("hospital",), "ospedali": ("hospital",), "ziekenhuis": ("hospital",),
    "farmacia": ("pharmacy", "drugstore"), "apotheek": ("pharmacy", "drugstore"), "medico": ("doctor",),
    "dottore": ("doctor",), "huisarts": ("doctor",), "dentista": ("dentist",), "tandarts": ("dentist",),
    # worship
    "chiesa": ("church", "place of worship"), "chiese": ("church", "place of worship"),
    "kerk": ("church", "place of worship"), "moschea": ("mosque",), "moskee": ("mosque",),
    "sinagoga": ("synagogue",), "synagoge": ("synagogue",),
    # care, shops, leisure
    "parrucchiere": ("hair",), "kapper": ("hair",), "estetista": ("beauty salon",),
    "schoonheidssalon": ("beauty salon",), "piscina": ("swimming pool",), "zwembad": ("swimming pool",),
    "supermercato": ("supermarket", "grocery"), "supermarkt": ("supermarket", "grocery"),
    "centro commerciale": ("shopping mall",), "winkelcentrum": ("shopping mall",),
    "albergo": ("hotel", "lodging"), "alberghi": ("hotel", "lodging"),
}  # fmt: skip


def _words5(text: str) -> set[str]:
    return {w[:5] for w in plain(text).split()}


def touches(topic: str, kind: str, statement: str) -> bool:
    """An off-limits topic covers the claim: its kind, or every content word of the topic in the statement,
    by stems ("posti" doesn't cover "compostiera"). A word for a kind of place also covers
    the provider's category for it, in English (CATEGORY_WORDS: "palestra" covers `gym`)."""
    t = plain(topic)
    if not t:
        return False
    if t == kind:
        return True
    have = stems(statement) | _words5(statement)
    if t in CATEGORY_WORDS and any(_words5(alt) <= have for alt in CATEGORY_WORDS[t]):
        return True
    words = [w for w in t.split() if len(w) >= 4 and w not in FUNCTION_WORDS] or t.split()
    return bool(words) and all(
        w[:5] in have or any(_words5(alt) <= have for alt in CATEGORY_WORDS.get(w, ())) for w in words
    )


def similar(a: str, b: str) -> bool:
    """The same meaning, roughly: their content stems mostly overlap (asked once, by meaning)."""
    sa, sb = stems(a), stems(b)
    return bool(sa and sb) and len(sa & sb) / len(sa | sb) >= 0.5


def subject_hash(kind: str, statement: str, secret: bytes) -> str:
    """A tombstone's only trace of a forgotten claim: an HMAC with the server's secret, no content
    (a plain hash of a short sentence can be guessed)."""
    return hmac.new(secret, f"{kind}|{plain(statement)}".encode(), hashlib.sha256).hexdigest()


def clean_scope(scope: dict[str, Any] | None) -> dict[str, str]:
    """Only the known keys and values; anything else is dropped (null = any)."""
    out: dict[str, str] = {}
    for key, value in (scope or {}).items():
        if key in SCOPE_KEYS and value in SCOPE_KEYS[key]:
            out[key] = value
    return out


def confidence(support: float, against: float, confirmed: bool) -> float:
    value = support / (support + against + 2)
    return round(max(value, CONFIRMED_FLOOR) if confirmed else value, 3)


def expires_at(kind: str, now: datetime) -> datetime | None:
    lifetime = LIFETIME.get(kind)
    return now + lifetime if lifetime else None


def time_of_day(local: datetime) -> str:
    hour = local.hour
    return "morning" if 5 <= hour < 12 else "afternoon" if hour < 18 else "evening" if hour < 23 else "night"


def scope_fits(scope: dict[str, str], local: datetime, travelling: bool = False) -> bool:
    """False when the claim's day, time or where (home / travel) says another moment;
    company is unknown to Pisa now, so a claim scoped to it stays (its scope is shown)."""
    day = "weekend" if local.isoweekday() >= 6 else "weekday"
    if scope.get("day") and scope["day"] != day:
        return False
    if scope.get("where") and scope["where"] != ("travel" if travelling else "home"):
        return False
    return not (scope.get("time") and scope["time"] != time_of_day(local))


@dataclass(frozen=True)
class Claim:
    id: str
    kind: str
    statement: str
    scope: dict[str, str]
    provenance: str
    confidence: float
    sensitive: bool
    hard: bool
    updated_at: datetime
    subject_key: str | None = None  # what an observed or extracted claim is about (relevance)
    state: str = "active"


def core_lines(claims: list[Claim], local: datetime, travelling: bool = False) -> list[str]:
    """The claims for a prompt: those whose scope fits now, hard limits first, then style, then by
    confidence; at most 25; one line each with provenance, confidence and scope."""
    fitting = [c for c in claims if scope_fits(c.scope, local, travelling)]
    order = sorted(
        fitting, key=lambda c: (not c.hard, c.kind != "style", -c.confidence, -c.updated_at.timestamp())
    )
    lines = []
    for c in order[:CORE_MAX]:
        tags = [f"{c.provenance} {c.confidence:.2f}"]
        if c.hard:
            tags.append("HARD LIMIT")
        if c.sensitive:
            tags.append("sensitive: use silently, never say it unless the user asks")
        if c.scope:
            tags.append("only " + ", ".join(f"{k} {v}" for k, v in sorted(c.scope.items())))
        lines.append(f"[{'; '.join(tags)}] {c.kind}: {c.statement}")
    return lines


# --- the first talk -------------------------------------------------------------

FIRST_TALK_MINUTES = 8  # past this Pisa offers to finish later


@dataclass(frozen=True)
class Section:
    title: str
    produces: str
    it: tuple[str, ...]
    en: tuple[str, ...]


SECTIONS: tuple[Section, ...] = (
    Section(
        "why and how",
        "nothing: ask whether to start",
        (
            "Vorrei conoscerti un po', così i miei consigli ti somigliano. Circa cinque minuti. Puoi dire "
            "'salta' "
            "o 'dopo' quando vuoi. Il resto lo imparo da quello che fai davvero, e puoi sempre vedere e "
            "cambiare "
            "quello che ho imparato. Iniziamo?",
        ),
        (
            "I'd like to get to know you a little, so my suggestions fit you. About five minutes. You can "
            "say "
            "'skip' or 'later' any time. I'll learn the rest from what you actually do, and you can always "
            "see and "
            "change what I've learned. Shall we?",
        ),
    ),
    Section(
        "where you live",
        "the user's home with set_home (never a claim; skip it if they'd rather not say)",
        ("Per cominciare: dove abiti? Basta la città o il quartiere, o la via se vuoi.",),
        ("To start: where do you live? The city or the neighbourhood is enough, or the street if you like.",),
    ),
    Section(
        "what you want from me",
        "goal claims",
        ("E poi: cosa mi renderebbe davvero utile per te? Una o due cose.",),
        ("And then: what would make me really useful to you? One or two things.",),
    ),
    Section(
        "places you love, and one you'd never go back to",
        "like / dislike claims, with the reasons as what they like (quiet, real espresso, touristy)",
        (
            "Dimmi un posto che ami qui intorno: un bar, un ristorante, qualsiasi cosa.",
            "Cosa ti piace di quel posto?",
            "Un altro, magari per un momento diverso?",
            "E un posto dove non torneresti mai? Perché?",
        ),
        (
            "Tell me a place you love around here: a café, a restaurant, anything.",
            "What do you like about it?",
            "Another one, maybe for a different moment?",
            "And a place you'd never go back to? Why not?",
        ),
    ),
    Section(
        "hard limits",
        "constraint claims with hard=true; an allergy or a diet only after asking once",
        (
            "C'è qualcosa che devo rispettare sempre quando ti consiglio cibo o posti? Per esempio qualcosa "
            "che "
            "non mangi, o un tipo di posto che non vuoi mai.",
        ),
        (
            "Anything I must always respect when I suggest food or places? For example something you don't "
            "eat, "
            "or a kind of place you never want.",
        ),
    ),
    Section(
        "how I should behave",
        "style claims, quiet times (a style claim), never_learn_about for what to leave",
        (
            "Come preferisci che ti parli: breve e diretto, o un po' più chiacchierone?",
            "Ci sono momenti in cui devo stare zitto, oltre alla notte?",
            "C'è qualcosa di cui preferisci che non parli mai, o che non impari?",
        ),
        (
            "How do you like me to talk: short and to the point, or a bit more chatty?",
            "Are there times I should stay quiet, apart from the night?",
            "Is there anything you'd rather I never bring up or learn about?",
        ),
    ),
    Section(
        "getting around",
        "a mobility claim (never ask how far they walk: Pisa measures it)",
        (
            "Quando vai da qualche parte in città, come ti muovi di solito: a piedi, in bici, in "
            "monopattino, coi "
            "mezzi?",
        ),
        (
            "When you go somewhere in town, how do you usually move: on foot, bike, scooter, public "
            "transport?",
        ),
    ),
    Section(
        "readback and close",
        "corrections only",
        (
            "Ecco cosa ricorderò. …",
            "Grazie. Il resto lo imparo guardando cosa scegli. Trovi tutto nell'app, in "
            "'Su di te', e puoi dire 'dimentica' quando vuoi.",
        ),
        (
            "Here's what I'll remember. …",
            "Thanks. The rest I'll learn by watching what you choose. You'll find "
            "all of it in the app under 'About you', and you can say 'forget that' any time.",
        ),
    ),
)
READBACK = len(SECTIONS) - 1  # the last section

FIRST_TALK_RULES = (
    "First talk (the user asked Pisa to get to know them). One short question at "
    "a "
    "time, then wait. Accept short answers. At most one clarifying question per answer. Never ask about "
    "health, religion, relationships or politics; the user may volunteer limits. 'Salta' / 'skip' moves on "
    "(call first_talk with action 'skip'); 'dopo' / 'later' stops and keeps the place (action 'later'); "
    "'basta' / 'enough' too. Store what the user says with remember_about_me, quoting their words. When a "
    "section is done, call first_talk with action 'next'."
)


# The user's words that start or resume the first talk (never untrusted text: not
# "get to know the area", not "continue looking"). A phrase about the two of them starts it anywhere in the
# message; a bare "let's meet" or "continuiamo" counts only as the whole message (after a few fillers).
START_PHRASES = (
    "conosciamoci", "conoscerci", "conoscermi", "get to know each other", "get to know me",
    "let s get acquainted", "facciamo conoscenza",
)  # fmt: skip
START_ALONE = ("let s meet", "meet me")
RESUME_ALONE = (
    "continuiamo", "riprendiamo", "andiamo avanti", "let s continue", "continue", "carry on", "go on",
    "let s carry on", "resume",
)  # fmt: skip
RESUME_NAMES_TALK = ("chiacchierata", "conversazione", "domande", "talk", "chat", "questions")
FILLERS = frozenset({
    "pisa", "dai", "ok", "okay", "allora", "pure", "ora", "adesso", "now", "si", "yes", "va", "bene", "beh",
    "ehi", "hey", "ciao", "please", "per", "favore", "well", "sure", "certo", "the", "la", "le", "il",
})  # fmt: skip
# The actions inside a talk need the user's own words for them: a word alone as the whole message, or a
# phrase anywhere (never a stem inside an answer, as in "passo dal bar", "salto il
# pranzo", "pausa pranzo", "dopo il lavoro").
SKIP_ALONE = frozenset({
    "salta", "saltala", "saltiamo", "skip", "skippa", "next", "prossima", "avanti", "passo", "pass",
    "passiamo",
})  # fmt: skip
SKIP_PHRASES = (
    "salta questa", "saltiamo questa", "salta la domanda", "saltala pure", "prossima domanda",
    "domanda successiva", "passiamo oltre", "passa oltre", "andiamo avanti", "preferisco non rispondere",
    "non voglio rispondere", "skip this", "skip it", "skip that", "let s skip", "next question",
    "move on", "i ll pass", "ill pass", "rather not say", "d rather not answer",
)  # fmt: skip
LATER_ALONE = frozenset({
    "dopo", "later", "basta", "enough", "stop", "pausa", "pause", "domani", "tomorrow",
})  # fmt: skip
LATER_PHRASES = (
    "facciamo dopo", "continuiamo dopo", "finiamo dopo", "piu tardi", "un altra volta", "un altro giorno",
    "basta cosi", "basta per ora", "basta per oggi", "per ora basta", "per oggi basta", "fermiamoci",
    "ci fermiamo", "facciamo una pausa", "continuiamo domani", "domani continuiamo", "another time",
    "some other time", "that s enough", "enough for now", "enough for today", "let s stop", "stop here",
    "stop for now", "continue later", "continue tomorrow", "let s pause", "take a break",
)  # fmt: skip


def says(owner_words: str, alone: frozenset[str], phrases: tuple[str, ...]) -> bool:
    """The user's own message is one of the words (fillers aside), or holds one of the phrases."""
    said = f" {plain(owner_words)} "
    return _core_words(owner_words) in alone or any(f" {p} " in said for p in phrases)


def _core_words(owner_words: str) -> str:
    return " ".join(w for w in plain(owner_words).split() if w not in FILLERS)


def asked_for_talk(owner_words: str, *, resume: bool) -> bool:
    """The user's own words ask to start (or, with resume, to continue) the first talk."""
    said, core = f" {plain(owner_words)} ", _core_words(owner_words)
    if any(f" {p} " in said for p in START_PHRASES) or core in START_ALONE:
        return True
    if not resume:
        return False
    verb = any(f" {r} " in said for r in RESUME_ALONE)
    return core in RESUME_ALONE or (verb and any(f" {n} " in said for n in RESUME_NAMES_TALK))


# The question Pisa asks before a profile write that waits for the user's yes: the server says it, with the
# exact statement, so the user's yes is to these words and no others.
QUESTIONS = {
    "sensitive": ("Vuoi che ne tenga sempre conto: «{}»?", "Shall I always take this into account: «{}»?"),
    "remember": ("Vuoi che mi ricordi che «{}»?", "Shall I remember that «{}»?"),
    "forget": ("Vuoi che dimentichi «{}»?", "Shall I forget «{}»?"),
    "never_learn": ("Vuoi che non impari mai nulla su «{}»?", "Shall I never learn anything about «{}»?"),
    "home": ("Vuoi che ricordi che abiti a «{}»?", "Shall I remember that you live at «{}»?"),
    "change": ("Posso {}?", "Shall I {}?"),  # a plan change held in a turn that read outside text
}


def confirm_question(write: dict[str, Any], lang: str) -> str:
    op = write["op"]
    if op == "note":  # the note's own question, with the very lines it adds (pisa.domain.note)
        return str(write["what"])
    if op == "remember":
        key, text = ("sensitive" if write.get("sensitive") else "remember"), write["statement"]
    elif op == "home":
        key, text = op, write["name"]
    elif op in ("change", "value"):  # a write carrying an outside value (`value`)
        key, text = "change", write["what"]
    else:
        key, text = op, write["what"] if op == "forget" else write["topic"]
    it, en = QUESTIONS[key]
    return (it if lang == "it-IT" else en).format(text)


def opening(lang: str) -> str:
    return (SECTIONS[0].it if lang == "it-IT" else SECTIONS[0].en)[0]


def section_question(index: int, lang: str) -> str:
    """Where a resumed first talk picks up: the first question of its section."""
    section = SECTIONS[max(0, min(index, len(SECTIONS) - 1))]
    return (section.it if lang == "it-IT" else section.en)[0]


# a claim against making tasks from emails at all ("non creare task dalle email")
_TASK_WORDS = re.compile(r"\b(tasks?|compit\w*|attivit\w*|promemoria|to-?do)\b", re.IGNORECASE)
_EMAIL_WORDS = re.compile(r"\b(e-?mail\w*|mail\w*|posta)\b", re.IGNORECASE)
_NEVER = re.compile(r"\b(non|mai|never|no|don'?t|stop|smetti\w*|evita\w*)\b", re.IGNORECASE)


# a sender, a kind or a "sometimes" narrows it: "dalle email di Amazon", "dalle newsletter",
# "dalle email promozionali", "non sempre": the user's own scope, never the general rule
_SENDER = re.compile(
    r"\b(?:di|da|from|of|by)\s+(?!tutt\w*\b|all\b|any\b|le\b|the\b|mie\b|my\b)[A-Za-z0-9]|@", re.IGNORECASE
)
_KIND = re.compile(
    r"\b(?:sempre|always|newsletter\w*|promozional\w*|pubblicit\w*|marketing|spam|notific\w*|"
    r"automatic\w*|ricevut\w*|receipts?|banca|bank|amazon|google|github)\b",
    re.IGNORECASE,
)


# "Non trasformare ogni email in un task: valuta contenuto
# e semantica…": "not every email" (or "judge by meaning") is how Pisa already works, never "no tasks
# at all". Only "not every" (a negation, then ogni/every/each) or a word asking Pisa to judge, and
# never with a word of the general ban ("mai", "any", "tutte", "solo se te lo dico io", "I'll judge myself")
# "non… tutte le email" / "not all" is "not every" too (nessuna/none/any is the ban)
_NOT_EVERY = re.compile(r"\b(?:non|not|don'?t|do not)\b.*\b(?:ogni|every|each|tutt[ei]|all)\b", re.IGNORECASE)
_JUDGE = re.compile(r"\b(?:valut\w*|giudic\w*|judg\w*|evaluat\w*|semantic\w*|contenuto)\b", re.IGNORECASE)
_BAN = re.compile(
    r"\b(?:mai|nessun\w*|never|any|none|no email\w*|qualunque|qualsiasi|"
    r"lo dico io|decido io|giudico io|valuto io|"
    r"i'?ll|i will|myself|me stesso)\b",
    re.IGNORECASE,
)


def _by_meaning(statement: str) -> bool:
    return bool(_NOT_EVERY.search(statement) or _JUDGE.search(statement)) and not _BAN.search(statement)


def no_email_tasks(statement: str) -> bool:
    """The statement forbids tasks from emails in general (emails become tasks by meaning).
    Only the general scope: a sender or a kind named ("dalle email di Amazon") is a rule of theirs."""
    if not (_TASK_WORDS.search(statement) and _EMAIL_WORDS.search(statement) and _NEVER.search(statement)):
        return False
    if _by_meaning(statement):  # "not every email…, judge them by meaning" is itself
        return False
    after = _EMAIL_WORDS.split(statement, maxsplit=1)[-1]
    return not _SENDER.search(after) and not _KIND.search(statement)
