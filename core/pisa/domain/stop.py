""" "Pisa, ferma tutto": a global pause of Pisa's own initiative. Pure rules: the user's words that
stop or resume. Which of the worker's steps are Pisa's initiative (they wait while stopped) is
`services.worker_steps`'s.

The user's own requests are never paused: a talk, a REST call, their yes to an approval, the reminders they
asked for and the floors (leave now) keep working. Pisa resumes only on their word (by voice or in the app).
"""

import re

# What waits while stopped, as the app shows it (`paused`).
PAUSED = (
    "follow_ups",
    "scheduled_pushes",
    "planning",
    "email_tasks",
    "vault_reminders",
    "autonomy",
    "ready_answers",
)
_NEGATION = r"(?:non|not|don'?t|never|mai)\s+(?:\w+\s+){0,2}"
_STOP = re.compile(
    r"\b(?:(?:ferma(?:ti)?|fermate|fermare|blocca(?:re)?|sospendi|sospendere|stoppa(?:re)?"
    r"|metti\s+in\s+pausa)"
    r"\s+(?:\w+\s+)?tutto|stop\s+(?:everything|all|it\s+all)|pause\s+everything|halt\s+everything)\b",
    re.IGNORECASE,
)
_RESUME = re.compile(
    r"\b(?:riprendi|riprendere|riparti|ripartire|ricomincia|ricominciare|riattiva(?:ti)?|riattivare|"
    r"resume|start\s+again|carry\s+on|back\s+to\s+work)\b",
    re.IGNORECASE,
)
_RESUME_WHAT = re.compile(r"\b(?:tutto|everything|all|pure|pausa|pause|lavorare|work|pisa)\b", re.IGNORECASE)
_RESUME_SHORT = 5  # "Pisa, riprendi" / "ok riparti pure": the verb alone in a short sentence


def _negated(words: str, at: int) -> bool:
    return re.search(_NEGATION + r"$", words[:at], re.IGNORECASE) is not None


def asks_to_stop(words: str) -> bool:
    """Their own words stop Pisa ("Pisa, ferma tutto", "stop everything"), never negated ("non fermare
    tutto")."""
    m = _STOP.search(words)
    return m is not None and not _negated(words, m.start())


# What Pisa says when their words stopped it by code (the conversation's reflex, before any model round)
STOPPED = {
    "it-IT": "Fermo tutto: niente iniziative mie finché non mi dici di riprendere.",
    "en-US": "Everything's stopped: nothing on my own initiative until you tell me to resume.",
}


def stopped_line(lang: str) -> str:
    return STOPPED["it-IT" if lang.startswith("it") else "en-US"]


# What Pisa says when their words resumed it by code (the conversation's reflex, while it is stopped)
RESUMED = {
    "it-IT": "Riparto: le mie iniziative sono di nuovo attive.",
    "en-US": "I'm back: my own initiative is on again.",
}


def resumed_line(lang: str) -> str:
    return RESUMED["it-IT" if lang.startswith("it") else "en-US"]


# Words around the verb that leave a resume a plain request ("Ok, puoi ripartire", "you can start again now")
_FILLER = frozenset(
    "ok okay va bene dai allora pure ora adesso subito pisa puoi potete si sì certo grazie per favore "
    "yes you can now please again then sure right all thanks a to".split()
)
_ASKING = re.compile(
    r"^\W*(?:come|cosa|che|quando|perch[eé]|posso|devo|how|what|when|why|can\s+i|do\s+i|should\s+i)\b",
    re.IGNORECASE,
)


def resumes_by_code(words: str) -> bool:
    """Their words are the plain request to resume, the conversation's reflex while Pisa is stopped (as
    "ferma tutto" stops it): `asks_to_resume`, and nothing but the verb, what it resumes and filler ("Ok, puoi
    ripartire", "Pisa, riprendi tutto", "you can resume now"). A question ("come faccio a ripartire?") and
    words about something else ("riprendi la palestra domani") go to the model."""
    if not asks_to_resume(words) or words.rstrip().endswith("?") or _ASKING.search(words):
        return False
    rest = _RESUME_WHAT.sub(" ", _RESUME.sub(" ", words))
    return all(w.casefold() in _FILLER for w in re.findall(r"\w+", rest))


def asks_to_resume(words: str) -> bool:
    """Their own words resume Pisa ("Pisa, riprendi", "riparti pure", "resume everything"): the verb with what
    it resumes, or alone in a short sentence ("riprendi la palestra domani" is about something else)."""
    m = _RESUME.search(words)
    if m is None or _negated(words, m.start()):
        return False
    short = len(re.findall(r"\w+", words)) <= _RESUME_SHORT
    return short or _RESUME_WHAT.search(words[m.end() :]) is not None
