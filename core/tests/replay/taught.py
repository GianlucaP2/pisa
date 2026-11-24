# ruff: noqa: E501
"""core-taught-1: the taught variant of the depth set. The same 20 scenarios (`depth.SCENARIOS`), each
preceded by one, two or three evenings of their telling Pisa how they like their answers, one conversation each
(`harness.teach`): nothing reaches the scenario but the standing instructions Pisa kept (Memoria).

The evenings are the organism experiment's own (`organism/probes/run.py`: HOW_HE_LIKES_IT, WHAT_HE_MEANT,
AND_TWO_MORE_THINGS), synthetic, written by the engineer, never the user's words; copied here word for word (the
depth job's container sees `core/tests` only), and a test checks the copies against the experiment's.

On the stub the scripted model keeps their own sentences as standing instructions (`KEPT`) and plays each
scenario's deep script; on the paid run (maintenance → brain-depth, `REPLAY_TAUGHT=1|2|3`) Pisa's real brain hears
the evenings and keeps its own.
"""

from typing import Any

from tests.replay import depth
from tests.replay.scenarios import c

TOOL = "remember_instruction"

HOW_HE_LIKES_IT = (
    "Una cosa su come mi piacciono le risposte, vale da domani in poi. A voce dimmi poco: cosa hai trovato, quale "
    "sceglieresti e perché, in una o due frasi. Il resto mettilo sul telefono: per una scelta, una tabella con "
    "almeno tre posti a confronto. Quando ti chiedo dove andare non fermarti alla mappa: leggi anche cosa dicono "
    "le guide. Per una domanda veloce, rispondi e basta."
)
WHAT_HE_MEANT = (
    "Due cose sulle risposte di oggi. Quando dico di leggere le guide intendo aprire la pagina della guida e "
    "leggerla, non fermarti al riassunto della ricerca. E prima di scegliere guarda più posti, almeno sei. A voce "
    "ancora più corto: due frasi brevi, il posto che sceglieresti e il motivo."
)
AND_TWO_MORE_THINGS = (
    "Ancora due cose. Se ti chiedo una cosa veloce, un orario, se un posto è aperto, dove compro un biglietto, "
    "rispondi subito: niente ricerche lunghe e niente tabella. E i numeri dammeli come li trovi, senza "
    "convertirli e senza arrotondarli."
)
EVENINGS = (HOW_HE_LIKES_IT, WHAT_HE_MEANT, AND_TWO_MORE_THINGS)

# The stub's Pisa: the standing instructions it keeps each evening (Memoria), their own words word for word (so
# kept at once, no yes asked); a later evening's line is newer, and the newer wins where two disagree.
KEPT = (
    (
        "A voce dimmi poco: cosa hai trovato, quale sceglieresti e perché, in una o due frasi.",
        "Il resto mettilo sul telefono: per una scelta, una tabella con almeno tre posti a confronto.",
        "Quando ti chiedo dove andare non fermarti alla mappa: leggi anche cosa dicono le guide.",
        "Per una domanda veloce, rispondi e basta.",
    ),
    (
        "Quando dico di leggere le guide intendo aprire la pagina della guida e leggerla, non fermarti al "
        "riassunto della ricerca.",
        "Prima di scegliere guarda più posti, almeno sei.",
        "A voce ancora più corto: due frasi brevi, il posto che sceglieresti e il motivo.",
    ),
    (
        "Se ti chiedo una cosa veloce, un orario, se un posto è aperto, dove compro un biglietto, rispondi "
        "subito: niente ricerche lunghe e niente tabella.",
        "i numeri dammeli come li trovi, senza convertirli e senza arrotondarli.",
    ),
)
CHANGES = (
    "D'accordo: a voce una o due frasi, il resto sul telefono, e leggo le guide.",
    "D'accordo: due frasi, almeno sei posti, e apro la guida.",
    "D'accordo: domande veloci subito, e i numeri come li trovo.",
)


def kept(evenings: int) -> list[str]:
    """Every standing instruction after `evenings` evenings, oldest first."""
    return [line for lines in KEPT[: max(0, evenings)] for line in lines]


def taught(scenario: dict[str, Any], evenings: int) -> dict[str, Any]:
    """The scenario after `evenings` (1 to 3) evenings of his telling; 0: the scenario as it is."""
    if evenings <= 0:
        return scenario
    n = min(evenings, len(EVENINGS))
    steps = [[[c(TOOL, text=line) for line in KEPT[i]], CHANGES[i]] for i in range(n)]
    return scenario | {
        "id": f"{scenario['id']}+taught{n}",
        "taught": list(EVENINGS[:n]),
        "taught_script": steps,
    }


def scenarios(evenings: int) -> list[dict[str, Any]]:
    """The depth set, taught `evenings` times (0: untaught)."""
    return [taught(s, evenings) for s in depth.SCENARIOS]
