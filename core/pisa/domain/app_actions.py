"""Pisa's actions in the user's own app (contract 2.2, the extension `pisa.app`, the feature
`app.actions`): the server asks the app to open one of its screens (`pisa.app.open`), the app does it and
reports back (`pisa.app.result`), and the tool's result says what the app reported. Pure: no I/O.

Only the app's own screens, never anything outside it; a server setting is never changed this way (that is
`propose_setting_change`, on the user's yes).
"""

import re
from typing import Literal

Target = Literal["tab", "riepilogo", "task", "ai_privacy", "riepilogo_refresh"]
TARGETS: tuple[Target, ...] = ("tab", "riepilogo", "task", "ai_privacy", "riepilogo_refresh")
REASONS = ("unsupported", "not_found", "not_now", "error")  # `pisa.app.result`'s reason, a closed set
# how long the tool waits for the app's report; past it the result says "not confirmed"
WAIT_S = 3.0
# the session's outcomes besides the app's own reasons
OK, UNCONFIRMED, UNSENT = "ok", "unconfirmed", "unsent"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII)

WHAT = {
    "tab": "the {id} tab",
    "riepilogo": "the riepilogo (Oggi)",
    "task": "the task",
    "ai_privacy": "Settings → AI e privacy",
    "riepilogo_refresh": "the riepilogo, read again",
}
SAID = {
    OK: "Open on their phone: {what}. Say it in a few words; don't describe the screen.",
    UNCONFIRMED: (
        "Not confirmed: Pisa asked the app to open {what} and the app didn't answer in time. Don't say it is "
        "open; tell them where it is instead."
    ),
    UNSENT: "Not opened: the app isn't connected now. Tell them where it is instead.",
    "unsupported": "Not opened: this app has no such screen. Tell them where it is instead.",
    "not_found": "Not opened: the app has no {what} (maybe it was closed or deleted). Say so.",
    "not_now": "Not opened: the app can't move now (a sheet open, the phone locked). Tell them where it is.",
    "error": "Not opened: the app couldn't open {what}. Tell them where it is instead.",
}


def checked(target: str, ident: str | None, tabs: tuple[str, ...]) -> str | None:
    """Why the call can't be sent (None: it can): a tab is one of `tabs`, a task is named by its id."""
    if target not in TARGETS:
        return f"Not opened: {target[:40]!r} isn't a screen of the app."
    if target == "tab" and ident not in tabs:
        return f"Not opened: tab is one of {', '.join(tabs)}."
    if target == "task" and not (isinstance(ident, str) and _UUID.fullmatch(ident.lower())):
        return "Not opened: a task is opened by its id (get_tasks gives it)."
    return None


def said(outcome: str, target: str, ident: str | None) -> str:
    """The tool's result for the app's report (or its absence)."""
    what = WHAT.get(target, target).format(id=ident or "")
    return SAID.get(outcome, SAID["error"]).format(what=what)
