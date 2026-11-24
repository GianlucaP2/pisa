"""Pisa's actions in the user's own app (contract 2.2, `pisa.app`): open one of its screens, and the app
reports back. And a change to one of Pisa's server settings (the briefing's rules, the reminders' lead
times), proposed and made only on the user's yes, with Undo.

Offered only to a client whose hello picked `pisa.app` (the app answers each action); never anything outside
the app. A server setting is never changed by an app action: only `propose_setting_change`, on their yes.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from pydantic_ai import Agent

from pisa.brain.purpose import purposeful
from pisa.brain.registry import offer
from pisa.domain import app_actions as rules
from pisa.domain import app_map as app_map_rules

# opens one of the app's screens (target, id) and returns the app's report: "ok", its reason, "unconfirmed",
# "unsent" (`pisa.services.session.DeviceSession.app_action`)
AppAction = Callable[[str, str | None], Awaitable[str]]


class SettingChanges(Protocol):
    """Pisa's server settings it may change on the user's yes (`pisa.services.setting_changes`)."""

    async def propose(self, setting: str, value: Any) -> str: ...


def add_app_tools(agent: Agent[None, str], app_action: AppAction) -> None:
    @offer(agent)
    @purposeful(None)
    async def open_in_app(
        target: Literal["tab", "riepilogo", "task", "ai_privacy", "riepilogo_refresh"],
        tab: Literal["pisa", "oggi", "impostazioni"] | None = None,
        task_id: str | None = None,
    ) -> str:
        """Open one of the app's own screens on their phone, when they ask to see it ("aprimi Oggi", "fammi
        vedere quell'attività", "dove cambio i modelli?"): a tab, the riepilogo, one of their tasks, or
        Settings → AI e privacy; or have the app read the riepilogo again. The app says whether it opened
        it. It only opens: it never changes a setting or anything else.

        Args:
            target: tab, riepilogo, task, ai_privacy, or riepilogo_refresh (the riepilogo read again).
            tab: for target tab: pisa, oggi or impostazioni.
            task_id: for target task: the task's id, from get_tasks.
        """
        ident = tab if target == "tab" else (task_id or "").strip().lower() if target == "task" else None
        if (refused := rules.checked(target, ident, app_map_rules.TABS)) is not None:
            return refused
        return rules.said(await app_action(target, ident), target, ident)


def add_setting_tools(agent: Agent[None, str], settings: SettingChanges) -> None:
    @offer(agent)
    @purposeful("propose")
    async def propose_setting_change(
        setting: Literal[
            "briefing.time",
            "briefing.max_words",
            "briefing.sections",
            "briefing.triggers.glasses_on",
            "briefing.triggers.app_open",
            "briefing.triggers.time",
            "briefing.triggers.phrase",
            "reminders.event_lead_min",
            "reminders.task_lead_min",
        ],
        value: str | int | bool | list[str],
    ) -> str:
        """Propose a change to one of Pisa's own settings when they ask for it ("il riepilogo alle 7:30",
        "avvisami 30 minuti prima degli eventi"): the briefing's time, length, sections or triggers, or the
        reminders' lead times. Nothing changes now: Pisa asks once and their yes makes it, with Undo. Say the
        change as a question; never say it is done.

        Args:
            setting: which setting.
            value: the new value: a time "07:30", a number of words (20-200) or minutes (0-120), true or
                false, or the sections (agenda, tasks, email, weather).
        """
        return await settings.propose(setting, value)
