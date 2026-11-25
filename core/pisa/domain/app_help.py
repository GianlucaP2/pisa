"""What Pisa's own app shows (the build 64+ layout), so Pisa can answer about its screens and buttons.
Pisa's own words about Pisa's own app: never outside text. Pure: no I/O.

The app sends its screen map with each run (`forwardedProps.pisa.app`, `pisa.domain.app_map`): `app_help`
answers from it, as untrusted data; `HELP` is the fallback, for an app that sends none. Kept short on purpose:
the tool returns it only when their words ask about the screen (`asks`).
"""

import re

from pisa.domain import app_map as app_map_rules

HELP = {
    "it-IT": (
        "L'app di Pisa ha tre schede, in basso: Pisa, Oggi e Impostazioni. Non c'è un menu laterale.\n"
        "- Pisa: la conversazione. Le sue frasi a destra nei fumetti, quelle di Pisa come testo; dentro "
        "compaiono i posti, le schede e le richieste di conferma. In basso il campo «Scrivi a Pisa» per "
        "scrivere, e l'ovale per parlare: toccalo e parla, Pisa risponde a voce; il suo colore segue Pisa "
        "(ascolta, pensa, parla).\n"
        "- Oggi: il riepilogo della giornata (quello che resta da fare oggi, aggiornato quando chiudi o "
        "aggiungi un'attività), il piano e le attività.\n"
        "- Impostazioni: le sezioni dell'app, tra cui Memoria (cosa Pisa ricorda di te) e AI e privacy "
        "(i modelli, il consenso, i dati).\n"
        "- La richiesta di conferma (Approva / Rifiuta): quando Pisa vuole fare qualcosa fuori da Pisa (un "
        "evento, una mail) o cambiare un'impostazione aspetta il tuo sì lì o a voce; alcune chiedono "
        "Face ID; scade dopo 10 minuti.\n"
        "- «Fatto» con Annulla compare dopo una modifica di Pisa, per annullarla."
    ),
    "en-US": (
        "Pisa's app has three tabs, at the bottom: Pisa, Today (Oggi) and Settings (Impostazioni). There is "
        "no side menu.\n"
        "- Pisa: the conversation. Their words on the right in bubbles, Pisa's as text; places, sheets and "
        "approval requests show up inside it. At the bottom, the «Message Pisa» field to type, and the oval "
        "to talk: tap it and speak, Pisa answers by voice; its colour follows Pisa (listening, thinking, "
        "speaking).\n"
        "- Today: the day's riepilogo (what is left to do today, updated when a task is closed or added), "
        "the plan and the tasks.\n"
        "- Settings: the app's sections, among them Memory (what Pisa remembers about them) and AI and "
        "privacy (the models, the consent, the data).\n"
        "- The approval request (Approve / Reject): when Pisa wants to do something outside Pisa (an event, "
        "an email) or change a setting it waits for their yes there or by voice; some need Face ID; it "
        "expires after 10 minutes.\n"
        "- «Done» with Undo shows after a change Pisa made, to undo it."
    ),
}

# Their words ask about Pisa's own screen: a button, an icon, the screen, the app, a corner of it
_ASKS = re.compile(
    r"\b(?:pulsant\w*|bottone\w*|tast[oi]|icon\w*|schermat\w*|schermo|interfaccia|cerchi(?:o|etto)|"
    r"l'app\b|nell'app|dell'app|quest'app|menu|in\s+alto\s+a\s+(?:sinistra|destra)|in\s+basso|"
    r"button|icon|screen|the\s+app|top\s+(?:left|right)|bottom|waveform|onda|ovale|oval|sched[ae]|tabs?|"
    r"impostazioni|settings)\b",
    re.IGNORECASE,
)
_WHAT = re.compile(
    r"\b(?:cosa|che\s+cosa|a\s+cosa|come|perch[eé]|dove|what|how|why|where|serve|fa|does|do)\b|\bcos'\s?[eè]",
    re.IGNORECASE,
)


def asks(words: str) -> bool:
    """Their words ask what something on Pisa's screen is or does ("cosa fa il pulsante in alto a
    sinistra?")."""
    return bool(_ASKS.search(words)) and bool(_WHAT.search(words))


def text(lang: str) -> str:
    """Pisa's own description of the build 64+ app: the fallback when the app sent no screen map."""
    return HELP.get(lang, HELP["en-US"])


def from_map(app: app_map_rules.AppMap) -> str:
    """The app's own map as the lines of `app_help`'s data block (the caller marks it untrusted)."""
    return app_map_rules.lines(app)
