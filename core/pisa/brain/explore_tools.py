"""Their fog of war and the Map of the Life, as sheets on their word. Read-only: both are
built by code from their own stays, places and trips (never a model, never the web)."""

import uuid
from collections.abc import Callable
from typing import Literal

from pydantic_ai import Agent

from pisa.brain.present_tools import Focus, Presenter
from pisa.brain.registry import offer
from pisa.ports.explore import ExploreData


def add_explore_tools(
    agent: Agent[None, str],
    explore: ExploreData,
    screen: Callable[[], Presenter | None],
    lang: Callable[[], str],
) -> None:
    async def present(
        tool: str, data: dict[str, object] | None, purpose: str, focus: Focus, empty: str
    ) -> str:
        p = screen()
        if p is None:
            return "Not shown: no screen in this turn."
        if data is None:
            return empty
        object_id = f"sheet:{uuid.uuid4()}"
        if (refused := p.held(tool, "sheet", object_id)) is not None:
            return refused
        ack = await p.show(tool, "sheet", object_id, purpose or str(data["title"]), focus, data=data)
        return ack if isinstance(ack, str) else ack.text()

    @offer(agent)
    async def show_fog_of_war(purpose: str = "", focus: Focus = "take") -> str:
        """Show their fog of war ("la nebbia", "dove non sono mai stato?"): how much of the ground around
        home they know, by direction, and the places nearby where they have never been, as a sheet on the
        phone.

        Args:
            purpose: one line in their language.
            focus: take, offer or quiet.
        """
        return await present(
            "show_fog_of_war", await explore.fog_sheet(lang()), purpose, focus,
            "Pisa doesn't know where they go yet (no stays). Say so in one line.",
        )  # fmt: skip

    @offer(agent)
    async def show_map_of_life(
        season: Literal["current", "last"] = "current", purpose: str = "", focus: Focus = "take"
    ) -> str:
        """Show the Map of the Life ("la mappa della mia vita"): a season's places, firsts, the place they go
        back to most, their trips and countries, counted from their own records, as a sheet on the phone.

        Args:
            season: current (so far) or last (the one before).
            purpose: one line in their language.
            focus: take, offer or quiet.
        """
        return await present("show_map_of_life", await explore.life_sheet(lang(), season), purpose, focus,
                             "Nothing recorded for that season yet. Say so in one line.")  # fmt: skip
