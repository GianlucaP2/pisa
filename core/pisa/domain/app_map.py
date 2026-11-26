"""The app's screen map (contract 2.2, `forwardedProps.pisa.app`, `$defs/AppMap`): its version, its tabs,
the tab on screen, the cards visible on it and its Settings sections, sent with each run. Pure: no I/O.

The map is the user's own app talking, but its titles reach the model: they are untrusted data, read in a data
block like outside text (`app_help`), and never change Pisa's tools or rules. A map out of bounds is dropped
whole (`parse` returns None): the turn goes on with Pisa's own description of the app (`app_help.HELP`).
"""

import re
from dataclasses import dataclass
from typing import Any

TABS_MAX = 6
CARDS_MAX = 12
SETTINGS_MAX = 16
TEXT_MAX = 40  # a tab's title, a Settings section's
_ID = re.compile(r"[a-z][a-z0-9_]{0,31}", re.ASCII)
_VERSION = re.compile(r"[0-9]{1,4}(\.[0-9]{1,4}){1,2}", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_KEYS = frozenset({"version", "build", "tabs", "current", "cards", "settings"})
# the words that name a tab of the build 64+ app (its ids): what Pisa may open (`open_in_app`)
TABS = ("pisa", "oggi", "impostazioni")


@dataclass(frozen=True)
class AppMap:
    version: str
    tabs: tuple[tuple[str, str], ...]  # (id, title), in order
    current: str
    cards: tuple[str, ...] = ()
    settings: tuple[str, ...] = ()
    build: int | None = None


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= TEXT_MAX or _CONTROL.search(value):
        return None
    return value


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def parse(value: Any) -> AppMap | None:
    """The map as `$defs/AppMap` bounds it; None for anything else (absent, a wrong field, a size or a length
    out of bounds, a current tab that is not one of its tabs)."""
    if not isinstance(value, dict) or not set(value) <= _KEYS:
        return None
    version, tabs, current = value.get("version"), value.get("tabs"), _id(value.get("current"))
    if not isinstance(version, str) or not _VERSION.fullmatch(version) or current is None:
        return None
    build = value.get("build")
    if build is not None and (
        isinstance(build, bool) or not isinstance(build, int) or not 1 <= build <= 1_000_000
    ):
        return None
    if not isinstance(tabs, list) or not 1 <= len(tabs) <= TABS_MAX:
        return None
    parsed: list[tuple[str, str]] = []
    for tab in tabs:
        if not isinstance(tab, dict) or set(tab) != {"id", "title"}:
            return None
        tab_id, title = _id(tab["id"]), _text(tab["title"])
        if tab_id is None or title is None:
            return None
        parsed.append((tab_id, title))
    if current not in {t for t, _ in parsed}:
        return None
    cards, settings = value.get("cards", []), value.get("settings", [])
    if not isinstance(cards, list) or len(cards) > CARDS_MAX or len(set(map(str, cards))) != len(cards):
        return None
    if not isinstance(settings, list) or len(settings) > SETTINGS_MAX:
        return None
    card_ids = [_id(c) for c in cards]
    sections = [_text(s) for s in settings]
    if None in card_ids or None in sections:
        return None
    return AppMap(
        version=version,
        tabs=tuple(parsed),
        current=current,
        cards=tuple(c for c in card_ids if c),
        settings=tuple(s for s in sections if s),
        build=build,
    )


def lines(app: AppMap) -> str:
    """The map as the data block's lines (`app_help`): only its own values, one per line."""
    current = next(title for tab_id, title in app.tabs if tab_id == app.current)
    out = [
        f"app version: {app.version}" + (f" (build {app.build})" if app.build else ""),
        "tabs, in order: " + "; ".join(f"{title} [{tab_id}]" for tab_id, title in app.tabs),
        f"on screen now: {current} [{app.current}]",
    ]
    if app.cards:
        out.append("cards visible on it: " + ", ".join(app.cards))
    if app.settings:
        out.append("Settings sections: " + "; ".join(app.settings))
    return "\n".join(out)
