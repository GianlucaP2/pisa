"""What Pisa's own screen does, in the
Essenziale look, so Pisa can answer about its buttons. Pisa's own words
about Pisa's own app: never outside text. Pure: no I/O.

Kept short on purpose: the tool returns it only when their words ask about the screen (`asks`).
"""

import re

HELP = {
    "it-IT": (
        "L'app di Pisa, aspetto Essenziale (Impostazioni → Aspetto: Essenziale o Classico).\n"
        "- In alto a sinistra, il pulsante con le linee: apre il menu laterale (Con Pisa, Oggi, Attività, "
        "Persone, Soldi, Viaggi, Rituali, Nella conversazione, l'interruttore Companion, Impostazioni). "
        "Si chiude toccando fuori, scorrendo a sinistra o con la ✕.\n"
        "- La schermata principale è la conversazione: le sue frasi a destra nei fumetti, quelle di Pisa "
        "come testo; dentro compaiono il riepilogo del mattino, i posti, le schede e le richieste di "
        "conferma.\n"
        "- In basso, il campo «Scrivi a Pisa»: scrivi e invii. Il microfono accanto detta un solo messaggio "
        "a voce. Il pulsante con l'onda apre la modalità vocale; quando hai scritto qualcosa diventa "
        "Invia.\n"
        "- La modalità vocale, a tutto schermo: un cerchio che cambia colore secondo Pisa (blu ascolta, "
        "indaco pensa, arancione parla, grigio e piccolo quando non è sicuro di essere collegato). "
        "Toccare il cerchio è come premere per parlare; Disattiva microfono lascia la conversazione "
        "aperta senza ascoltare; la ✕ la chiude. I sottotitoli si accendono e spengono.\n"
        "- La richiesta di conferma (Approva / Rifiuta): quando Pisa vuole fare qualcosa fuori da Pisa (un "
        "evento, una mail) aspetta il tuo sì lì o a voce; alcune chiedono Face ID; scade dopo 10 minuti. "
        "Nella schermata principale è dentro la conversazione, nelle altre compare in alto.\n"
        "- «Fatto» con Annulla compare dopo una modifica di Pisa, per annullarla."
    ),
    "en-US": (
        "Pisa's app, Essential look (Settings → Appearance: Essential or Classic).\n"
        "- Top left, the button with the lines: opens the side menu (With Pisa, Today, Tasks, People, Money,"
        " Trips, Rituals, Recent objects, the Companion switch, Settings). Close it by tapping outside, "
        "swiping left or with ✕.\n"
        "- The main screen is the conversation: their words on the right in bubbles, Pisa's as text; the "
        "morning briefing, places, sheets and approval requests show up inside it.\n"
        "- At the bottom, the «Message Pisa» field: type and send. The microphone next to it dictates one "
        "spoken message. The waveform button opens voice mode; once there is text it becomes Send.\n"
        "- Voice mode, full screen: a circle whose colour follows Pisa (blue listening, indigo thinking, "
        "orange speaking, grey and small when unsure it is connected). Tapping the circle is pressing to "
        "talk; Mute keeps the talk open without listening; ✕ closes it. Captions can be turned on or off.\n"
        "- The approval request (Approve / Reject): when Pisa wants to do something outside Pisa (an event, "
        "an email) it waits for your yes there or by voice; some need Face ID; it expires after 10 minutes. "
        "On the main screen it is inside the conversation, on the others it shows at the top.\n"
        "- «Done» with Undo shows after a change Pisa made, to undo it."
    ),
}

# Their words ask about Pisa's own screen: a button, an icon, the screen, the app, a corner of it
_ASKS = re.compile(
    r"\b(?:pulsant\w*|bottone\w*|tast[oi]|icon\w*|schermat\w*|schermo|interfaccia|cerchi(?:o|etto)|"
    r"l'app\b|nell'app|dell'app|quest'app|menu|in\s+alto\s+a\s+(?:sinistra|destra)|in\s+basso|"
    r"button|icon|screen|the\s+app|top\s+(?:left|right)|bottom|waveform|onda)\b",
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
    return HELP.get(lang, HELP["en-US"])
