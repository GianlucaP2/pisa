"""The reflex's free reading of the user's partial words, when no model answers in time (none picked,
a failure, over its budget): which of Pisa's own reads the words point to, and the day they name. Italian and
English. A wrong guess costs one read of Pisa's own data, never a write.
"""

from dataclasses import dataclass, field

from pisa.domain.speculation import words as split

_CHANGE = frozenset(
    "metti mettimi aggiungi aggiungimi crea sposta spostami cancella annulla elimina togli ricordami segna "
    "manda scrivi rimanda anticipa posticipa add put move cancel delete remove remind schedule send book "
    "reschedule".split()
)
_QUESTION = frozenset(
    "cosa che quando quanto quanti quale quali dove chi ho devo c'è ce what when how which where who do does "
    "is are am have".split()
)
_AGENDA = frozenset(
    "calendario agenda impegni appuntamento appuntamenti riunione riunioni call evento eventi dentista "
    "medico cena pranzo palestra calendar meeting meetings appointment appointments event events "
    "schedule".split()
)
_TASKS = frozenset("attività task tasks cose fare lista todo to-do compiti scadenze scadenza".split())
_PLAN = frozenset("piano programma giornata prossimo prossima dopo plan next".split())
_TRAVEL = frozenset(
    "arrivare arrivo strada traffico tragitto metterci quanto_ci travel drive traffic get".split()
)
# the forecast (an outside read: only on the final words)
_WEATHER = frozenset(
    "meteo tempo piove pioggia pioverà sole caldo freddo ombrello temperatura gradi weather rain sunny cold "
    "hot umbrella forecast".split()
)
_WAITING = frozenset("chiesto confermare conferma aspetti asked confirm waiting".split())
_TODAY = frozenset({"oggi", "stasera", "stamattina", "stanotte", "today", "tonight"})
_TOMORROW = frozenset({"domani", "domattina", "tomorrow"})
_AFTER = frozenset({"dopodomani"})


@dataclass(frozen=True)
class RulesRead:
    intent: str  # question, change, chat, unclear
    goal: str
    reads: list[str] = field(default_factory=list)
    day: str | None = None
    place: str | None = None


def rules_read(text: str) -> RulesRead:
    w = split(text)
    if len(w) < 3:
        return RulesRead("unclear", "")
    have = set(w)
    joined = " ".join(w)
    change = bool(have & _CHANGE)
    question = text.rstrip().endswith("?") or w[0] in _QUESTION or bool(have & {"cosa", "quando", "what"})
    reads: list[str] = []
    day = (
        "tomorrow"
        if have & _TOMORROW
        else ("after" if have & _AFTER else ("today" if have & _TODAY else None))
    )
    if have & _AGENDA or (day is not None and (change or question)):
        reads.append("agenda")
    if have & _TASKS:
        reads.append("tasks")
    if have & _PLAN and "agenda" not in reads:
        reads.append("plan")
    if have & _TRAVEL or "quanto ci" in joined or "how long" in joined:
        reads.append("travel")
    if have & _WAITING:
        reads.append("waiting")
    if have & _WEATHER and ("tempo" not in have or "che tempo" in joined or "tempo fa" in joined):
        reads.append("weather")
    intent = "change" if change else ("question" if question or reads else "chat")
    return RulesRead(intent, " ".join(w[:12]), reads[:3], day)
