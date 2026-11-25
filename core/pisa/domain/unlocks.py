"""Abilities unlocked. When earned autonomy promotes a type (Q's table: their Face ID
or their yes to Pisa's Sunday proposal), the change is announced **once**, as a small ceremony in the next
Sunday card, with the way back. Never by voice in the middle of a talk (no notice, no push of its own). Pure
rules.
"""

from typing import Any

from pisa.domain import autonomy as autonomy_rules

# What Pisa now does without asking, per type: (Italian, English)
_DOES = {
    "calendar_create": ("creo i tuoi eventi", "I create your events"),
    "calendar_move": ("sposto i tuoi eventi", "I move your events"),
    "calendar_cancel": ("annullo i tuoi eventi", "I cancel your events"),
    "task_done": ("chiudo le attività fatte", "I close tasks that are done"),
    "task_close": ("tolgo le attività ferme", "I clear stale tasks"),
    "email_draft": ("preparo le bozze delle email", "I draft your emails"),
}
_HOW = {
    "tell": ("senza chiedere, e te lo dico subito dopo", "without asking, and I tell you right after"),
    "silent": (
        "senza chiedere e senza dirtelo: lo trovi nel registro",
        "without asking or telling you: it's in the ledger",
    ),
    "ledger": (
        "senza chiedere: lo vedi solo nella carta della domenica",
        "without asking: you see it only in Sunday's card",
    ),
}
# The way back, in their words (the brain's `ask_again`) or the app's button (POST /v1/autonomy/{type}/ask)
_BACK = {
    "calendar_create": ("chiedimelo di nuovo prima di creare eventi", "ask me again before creating events"),
    "calendar_move": (
        "chiedimelo di nuovo prima di spostare gli eventi",
        "ask me again before moving events",
    ),
    "calendar_cancel": (
        "chiedimelo di nuovo prima di annullare eventi",
        "ask me again before cancelling events",
    ),
    "task_done": ("chiedimelo di nuovo prima di chiudere le attività", "ask me again before closing tasks"),
    "task_close": ("chiedimelo di nuovo prima di togliere attività", "ask me again before clearing tasks"),
    "email_draft": ("chiedimelo di nuovo prima di preparare bozze", "ask me again before drafting emails"),
}
_SHORT = {
    "calendar_create": ("nuovi eventi", "new events"),
    "calendar_move": ("spostamenti", "moves"),
    "calendar_cancel": ("annullamenti", "cancellations"),
    "task_done": ("attività fatte", "done tasks"),
    "task_close": ("attività ferme", "stale tasks"),
    "email_draft": ("bozze", "drafts"),
}
TITLE = {"it-IT": "Una cosa nuova che faccio per te", "en-US": "Something new I do for you"}
CHIP = {"it-IT": "Chiedimelo di nuovo", "en-US": "Ask me again"}
MAX_SHOWN = 3


def _i(lang: str) -> int:
    return 0 if lang == "it-IT" else 1


def text(type_: str, level: str, lang: str) -> str:
    """ "Da oggi sposto i tuoi eventi senza chiedere, e te lo dico subito dopo: per tornare indietro, dimmi
    «chiedimelo di nuovo prima di spostare gli eventi» o tocca «Chiedimelo di nuovo»." """
    i = _i(lang)
    does = _DOES.get(type_, (type_, type_))[i]
    how = _HOW.get(level, _HOW["tell"])[i]
    back = _BACK.get(type_, ("chiedimelo di nuovo", "ask me again"))[i]
    chip = CHIP["it-IT" if i == 0 else "en-US"]
    if i == 0:
        return f"Da oggi {does} {how}. Per tornare indietro, dimmi «{back}» o tocca «{chip}»."
    return f"From today {does} {how}. To go back, say “{back}” or tap “{chip}”."


def promoted(previous: str, level: str) -> bool:
    """A step up the ladder (never `email_send`: it is locked)."""
    ladder = autonomy_rules.LADDER
    return previous in ladder and level in ladder and ladder.index(level) > ladder.index(previous)


def blocks(unlocks: list[dict[str, Any]], lang: str) -> list[dict[str, Any]]:
    """The ceremony as blocks: a section, one line per unlock with its way back, the way-back chip."""
    if not unlocks:
        return []
    key = "it-IT" if lang == "it-IT" else "en-US"
    shown = unlocks[:MAX_SHOWN]
    out: list[dict[str, Any]] = [{"type": "section", "title": TITLE[key]}]
    out += [{"type": "text", "text": u["text"][:600]} for u in shown]
    i = _i(key)
    chips = [f"{CHIP[key]}: {_SHORT.get(u['type'], (u['type'], u['type']))[i]}"[:40] for u in shown]
    out.append({"type": "chips", "items": chips})
    return out
