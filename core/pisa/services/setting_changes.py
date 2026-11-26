"""Pisa's server settings it may change for the user (contract 2.2): the briefing's rules (its time, length,
sections and triggers) and the reminders' lead times. Only on the user's explicit yes: `propose` asks one
question (an approval of kind `setting_change`, answered like any proposal: a plain "sì" by code,
`answer_proposal`, or the card), the yes makes it, and it shows with Undo for a day (`undo_last_change`, the
card's Annulla) that puts the value back.

Locked on a demo server (App Store review mode), as its AI settings are: nothing is proposed or changed.
"""

from typing import Any

from pisa.domain import results
from pisa.domain import settings as setting_rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services.approvals import ApprovalsService, Outcome
from pisa.services.conversation import current_turn, turn_device
from pisa.services.settings import SettingsService

KIND = "setting_change"
_log = get_logger("pisa.setting_changes")

# the settings Pisa may propose, with how each is said
LABELS: dict[str, dict[str, str]] = {
    "briefing.time": {"it-IT": "l'ora del riepilogo", "en-US": "the briefing's time"},
    "briefing.max_words": {
        "it-IT": "la lunghezza del riepilogo (parole)",
        "en-US": "the briefing's length (words)",
    },
    "briefing.sections": {"it-IT": "le parti del riepilogo", "en-US": "the briefing's sections"},
    "briefing.triggers.glasses_on": {
        "it-IT": "il riepilogo quando metti gli occhiali",
        "en-US": "the briefing when the glasses go on",
    },
    "briefing.triggers.app_open": {
        "it-IT": "il riepilogo quando apri l'app",
        "en-US": "the briefing when the app opens",
    },
    "briefing.triggers.time": {"it-IT": "il riepilogo all'ora fissata", "en-US": "the briefing at its time"},
    "briefing.triggers.phrase": {
        "it-IT": "il riepilogo quando lo chiedi a voce",
        "en-US": "the briefing when asked aloud",
    },
    "reminders.event_lead_min": {
        "it-IT": "il preavviso per gli eventi (minuti)",
        "en-US": "the reminder before events (minutes)",
    },
    "reminders.task_lead_min": {
        "it-IT": "il preavviso per le attività (minuti)",
        "en-US": "the reminder before tasks (minutes)",
    },
}
LOCKED = (
    "propose_setting_change: locked",
    "This is a demo server (App Store review mode): its settings are set by its operator and can't be "
    "changed. Say so; nothing changed.",
)


def _path_get(settings: dict[str, Any], path: str) -> Any:
    value: Any = settings
    for key in path.split("."):
        value = value[key]
    return value


def _patch(path: str, value: Any) -> dict[str, Any]:
    out: Any = value
    for key in reversed(path.split(".")):
        out = {key: out}
    return out


def _said(value: Any, lang: str) -> str:
    if isinstance(value, bool):
        return ("sì" if value else "no") if lang == "it-IT" else ("on" if value else "off")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


class SettingChangesService:
    def __init__(
        self,
        settings: SettingsService,
        approvals: ApprovalsService,
        *,
        owner: Owner,
        locked: bool = False,
    ) -> None:
        self._settings = settings
        self._approvals = approvals
        self._owner = owner
        self._locked = locked
        approvals.register(KIND, self._do, self._undo, self._verify)

    def _unlocked(self) -> None:
        if self._locked:
            raise PisaError("connector_off", LOCKED[1], "Use the settings this server already has")

    async def propose(self, setting: str, value: Any) -> str:
        """One question for their yes: the change, from what it is to what they asked. Nothing changes now."""
        if self._locked:
            return results.error(*LOCKED)
        if setting not in LABELS:
            return results.error(
                f"propose_setting_change: {setting[:40]!r} isn't one Pisa changes",
                "Only the briefing's rules and the reminders' lead times: the rest is in Settings.",
            )
        current = await self._settings.get()
        lang = current["language"] if current["language"] in ("it-IT", "en-US") else "en-US"
        try:
            setting_rules.apply_patch(current, _patch(setting, value))
        except PisaError as err:
            return results.error(f"propose_setting_change: {err.message}", f"Not proposed: {err.message}")
        before = _path_get(current, setting)
        if before == value:
            return results.error(
                f"propose_setting_change: {setting} is already {_said(value, lang)}",
                "Nothing to change: say it is already so.",
            )
        label = LABELS[setting][lang]
        summary = (
            f"Cambio {label}: da {_said(before, lang)} a {_said(value, lang)}"
            if lang == "it-IT"
            else f"Change {label}: from {_said(before, lang)} to {_said(value, lang)}"
        )
        payload = {"setting": setting, "value": value, "before": before, "title": label}
        view = await self._approvals.create(
            KIND, summary, [], payload, device_id=turn_device(), lang=lang, origin="proposal", asked=True
        )
        if (turn := current_turn.get()) is not None:
            turn.approvals.append(view["id"])  # their yes is heard in their next turn, by code
            turn.proposed.append(view["id"])
        ident = results.short(view["id"])
        ask = f"{summary}?"
        return results.needs_yes(
            ident, f"{ident} proposed, NOT done: {ask}", reason="a setting changes only on their yes", ask=ask
        )

    async def _do(self, payload: dict[str, Any]) -> Outcome:
        self._unlocked()
        setting, value = str(payload["setting"]), payload["value"]
        lang = (await self._settings.patch(_patch(setting, value)))["language"]
        _log.info("setting_changed", setting=setting, by="approval")
        title = str(payload.get("title") or setting)
        said = f"Fatto: {title} ora è {_said(value, lang)}." if lang == "it-IT" else (
            f"Done: {title} is now {_said(value, lang)}."
        )  # fmt: skip
        return Outcome(said, undo={"setting": setting, "value": value, "before": payload["before"]})

    async def _undo(self, undo: dict[str, Any]) -> str:
        """The value it had before, unless it changed again since (then nothing is overwritten)."""
        self._unlocked()
        setting = str(undo["setting"])
        current = await self._settings.get()
        if _path_get(current, setting) != undo["value"]:
            raise PisaError(
                "approval_decided", "That setting changed again since: nothing was put back.", "Ask Pisa"
            )
        await self._settings.patch(_patch(setting, undo["before"]))
        _log.info("setting_changed", setting=setting, by="undo")
        lang = current["language"]
        before = _said(undo["before"], lang)
        return f"Rimesso com'era: {before}." if lang == "it-IT" else f"Put back as it was: {before}."

    async def _verify(self, payload: dict[str, Any], _: Outcome) -> bool:
        return _path_get(await self._settings.get(), str(payload["setting"])) == payload["value"]
