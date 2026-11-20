"""health-sensitive places (pharmacy, clinic, hospital, doctor, dentist, lab). Pure rules, no I/O.

Pisa answers their own direct question about one from its stays ("ieri sono stato in farmacia?" → "Sì: ieri
eri alla Farmacia Centrale, 18:02-18:20.") and never brings one up by itself: `learning.sensitive_place` (the
rail every unprompted path checks: episodes, claims, hunches, walk recaps, ideas, the map, the talk's "where
you are") counts these as sensitive. Never a diagnosis: only the place and the time.
"""

import re

from pisa.domain.profile import plain

# kind -> the stems of the provider's type (Apple and Google, snake case) and of their words (it, en, nl)
HEALTH_KINDS: dict[str, tuple[str, ...]] = {
    "pharmacy": ("farmac", "pharmac", "drugstore", "apothe", "apotek"),
    "hospital": ("ospedal", "hospital", "soccors", "ziekenhuis", "emergency", "spoedeisend"),
    "clinic": ("clinic", "clinica", "kliniek", "poliklin", "ambulator", "poliambulator", "consultori"),
    "doctor": ("medic", "doctor", "physician", "huisarts", "dokter"),
    "dentist": ("dentist", "dental", "odontoiatr", "tandarts"),
    "lab": ("analisi", "diagnost", "prelie"),
    # the specialists they may visit
    "psychologist": ("psicolog", "psycholo", "psicoterap", "psychotherap", "psichiatr", "psychiatr"),
    "physiotherapist": ("fisioterap", "physiotherap", "physio", "fysiotherap", "osteopat"),
    "specialist": (
        "ginecolog",
        "gynecolog",
        "gynaecolog",
        "cardiolog",
        "cardiolo",
        "dermatolo",
        "oculist",
        "ophthalmolog",
        "oogarts",
        "ortoped",
        "orthop",
        "pediatr",
        "paediatr",
        "kinderarts",
        "neurolo",
        "urolog",
        "uroloog",
        "otorin",
        "logoped",
        "ostetric",
        "obstetric",
        "endocrinolog",
        "gastroenterolog",
        "oncolog",
        "oncoloog",
    ),
}
# … and in their question only (a place named "Laboratorio di pasticceria" or "Dott. Rossi, commercialista" is
# no health place; "sono andato dal dottore?" asks about one)
ASKED_KINDS: dict[str, tuple[str, ...]] = {"doctor": ("dottor",), "lab": ("laborator",)}
# what Pisa calls each kind when it answers ("eri in farmacia", "you were at a pharmacy")
KIND_SAID: dict[str, tuple[str, str]] = {
    "pharmacy": ("in farmacia", "at a pharmacy"),
    "hospital": ("in ospedale", "at a hospital"),
    "clinic": ("in una clinica", "at a clinic"),
    "doctor": ("dal medico", "at a doctor's"),
    "dentist": ("dal dentista", "at a dentist's"),
    "lab": ("in un laboratorio di analisi", "at a lab"),
    "psychologist": ("da uno psicologo", "at a psychologist's"),
    "physiotherapist": ("da un fisioterapista", "at a physiotherapist's"),
    "specialist": ("da uno specialista", "at a specialist's"),
}


def health_kinds(text: str | None, *, asked: bool = False) -> set[str]:
    """The health kinds a text names: a place's name and type, or (`asked`) their words."""
    words = plain((text or "").replace("_", " ")).split()
    kinds = {kind for kind, stems in HEALTH_KINDS.items() if any(w.startswith(stems) for w in words)}
    if asked:
        kinds |= {kind for kind, stems in ASKED_KINDS.items() if any(w.startswith(stems) for w in words)}
    return kinds


# their own first-person visit, the verb next to the place word. A first-person visit verb
# ("sono stato/andato/arrivato/uscito/rimasto…", "mi sono fermato", "siamo stati", "ero", "ho visto", "was I",
# "did I go/leave/make it", "have I been", "ik ben / ben ik"), then only little words (an article, a
# preposition, a day) up to the place word, a few tokens at most: "dove ero quando Marco era in ospedale?",
# "sono andato a pranzo col dottor Bianchi?", "I was wondering if Marco went to the hospital?" name someone or
# something else first and fall through.
_PARTICIPLES = r"(?:stat|andat|passat|entrat|arrivat|uscit|rimast|fermat|tornat|recat)"
_VISIT = re.compile(
    r"\b(?:(?:mi |ci )?sono (?:gia |poi |mai |ancora )?" + _PARTICIPLES + r"[oa]"
    r"|(?:ci )?siamo (?:gia |poi |mai |ancora )?" + _PARTICIPLES + r"[ie]"
    r"|ero|eravamo|ho visto|abbiamo visto|ho visitato"
    r"|was i|were we|i was|we were|i went|we went|i left|i got|i made it|i ve been|i have been|have i been"
    r"|did i (?:go|visit|stop|pass|drop|leave|get|arrive|see|make it|stay)|have we been"
    r"|did we (?:go|visit|stop|pass|drop|leave|get|arrive|see|make it|stay)"
    r"|ben ik|was ik|ging ik|ik ben|ik was|ik ging|zijn we|waren we|we zijn|we waren)\b"
)
# the little words that may sit between the verb and the place word (it, en, nl)
_BETWEEN = frozenset(
    "in a al alla allo ai alle all da dal dalla dallo dai dalle il lo la l i gli le un una uno un del della "
    "nel nella presso ieri oggi stamattina stasera stamani poi gia mai ancora "
    "dall dell nell sull coll mio mia miei mie nostro nostra suo sua pronto fare tanto ritardo tardi presto "
    "to the at an by into from of it back over yesterday today earlier this morning my our go see visit "
    "naar de het een bij op gisteren vandaag vanochtend mijn onze".split()
)
# words that start like a place kind and are not one: their medicine is no doctor ("ho preso le medicine?",
# "did I get the medicine?")
_NOT_A_PLACE = ("medicin", "medicament", "medication")
# "ho visto il dottor House in tv?": what they saw on a screen is no visit
_ON_A_SCREEN = re.compile(
    r"\b(?:in tv|alla tv|in televisione|on tv|on the tv|op tv|in a film|in un film|serie|series)\b"
)
_SAW = re.compile(r"^(?:ho visto|abbiamo visto|did i see|did we see)$")
_NEAR = 4  # at most this many little words between the verb and the place word


def _kinds_of(word: str, asked: bool) -> set[str]:
    found = {kind for kind, stems in HEALTH_KINDS.items() if word.startswith(stems)}
    if asked:
        found |= {kind for kind, stems in ASKED_KINDS.items() if word.startswith(stems)}
    return found


def visit_kinds(words: str | None) -> set[str]:
    """The health kinds their own question asks whether they visited (none: not such a question)."""
    text = plain((words or "").replace("'", " ").replace("\u2019", " "))
    kinds: set[str] = set()
    for verb in _VISIT.finditer(text):
        if _SAW.match(verb.group(0)) and _ON_A_SCREEN.search(text):
            continue
        after = text[verb.end() :].split()
        for n, word in enumerate(after[: _NEAR + 1]):
            if word.startswith(_NOT_A_PLACE):
                break
            found = _kinds_of(word, asked=True)
            if found:
                kinds |= found
                break
            if word not in _BETWEEN or n == _NEAR:
                break
    return kinds


def health_place(name: str | None, category: str | None) -> bool:
    """A health-sensitive place: its provider type or its name names a health kind."""
    return bool(health_kinds(f"{name or ''} {category or ''}"))
