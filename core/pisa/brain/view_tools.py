"""Pisa's own views. `save_view` keeps what Pisa just showed as a named recipe (data, never code), only
on the user's own words this turn; `show_view` runs one again with fresh data and presents it."""

import uuid
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from pisa.brain.drawing_tools import AFTER_OUTSIDE, outside_before_drawing, payload
from pisa.brain.present_tools import Focus, Presenter
from pisa.brain.profile_tools import ProfileTurn
from pisa.brain.registry import offer
from pisa.brain.untrusted import current, data_block
from pisa.domain import views as rules
from pisa.ports.views import ViewsData


class ReadIn(BaseModel):
    tool: str = Field(description=f"one of: {', '.join(sorted(rules.READS))}")
    args: dict[str, str | int] = Field(default_factory=dict)


class RecipeIn(BaseModel):
    reads: list[ReadIn] = Field(default_factory=list, max_length=rules.READS_MAX)
    blocks: list[dict[str, Any]] = Field(min_length=1, max_length=24)


def add_view_tools(
    agent: Agent[None, str], views: ViewsData, screen: Callable[[], Presenter | None], turn: ProfileTurn
) -> None:
    @agent.instructions(name="views")  # its block's tag is new on every call: kept as first said
    async def own_views() -> str:
        names = await views.names()
        if not names:
            return ""
        block = data_block("skill-names", ", ".join(names[:20]), what="the names of Pisa's views")
        return f"Pisa's own views (show_view by name):\n{block.render()}"

    @offer(agent)
    async def save_view(name: str, description: str, recipe: RecipeIn) -> str:
        """Keep what you just showed (on the screen or in words) as one of Pisa's own views, ONLY when the
        user's words this turn ask to keep it ("salvala come meteo"). A view is a recipe, never code: `reads`
        are Pisa's own read-only tools to run again (get_current_time, get_agenda{day}, get_tasks, get_plan,
        get_weather{where, when: today, tomorrow, +2…}, map_search{query, near}, web_search{query},
        read_page{url}, search_my_email{query, days}); `blocks` are sheet blocks whose strings may hold
        placeholders {{n.path}} (read n's result, dotted: get_weather's {{0.day.max_c}}) and at most one
        {"type": "each", "from": "{{0.items}}", "block": {...}}
        repeating one block per item ({{item.path}}).

        Args:
            name: the user's word for it ("meteo").
            description: one line: what it shows.
            recipe: {reads: [{tool, args}], blocks: [...]}.
        """
        if not rules.asks_to_save(turn.words) or current().outside_now():
            return (
                "Not saved: a view is kept only when the user's own words this turn ask for it (never on "
                "your initiative, never in a turn that read outside text). Offer it in one line if useful."
            )
        p = screen()
        hosts = {rules.host(s["url"]) for s in (p.pages() if p is not None else []) if s.get("url")}
        return await views.save(
            name, description, recipe.model_dump(), hosts=hosts, said=turn.words,
            turn_id=turn.turn_id or None, device_id=str(turn.device) if turn.device else None,
        )  # fmt: skip

    @offer(agent)
    async def show_view(name: str, purpose: str = "", focus: Focus = "take") -> str:
        """Show one of Pisa's own views ("mostrami meteo", "meteo"): its reads run now and the sheet is drawn
        with today's data. Say one line; the screen does the rest.

        Args:
            name: the user's word for the view.
            purpose: one line in their language.
            focus: take, offer or quiet.
        """
        p = screen()
        if p is None:
            return "Not shown: no screen in this turn."
        found, data, dropped = await views.run(name)
        if data is None:
            why = "; ".join(dropped[:3])
            return found if not dropped else f"Not shown: {found} has nothing to show today ({why})."
        if (drawn := data.get("drawing")) is not None:  # the code unchanged, fresh data
            if outside_before_drawing():  # never the box after outside text
                return AFTER_OUTSIDE.replace("show_sheet", "the sheet")
            drawing_id = str(uuid.uuid4())
            object_id = f"drawing:{drawing_id}"
            if (refused := p.held("show_view", "drawing", object_id)) is not None:
                return refused
            body = payload(drawing_id, str(drawn.get("title") or found), str(drawn.get("summary") or ""),
                           str(drawn.get("kind") or "other"), str(drawn["html"]), drawn["data"],
                           drawn.get("basis"))  # fmt: skip
            # a saved view is a new drawing: revision 0 (the phone marks "Aggiornato" above 0)
            ack = await p.show(
                "show_view", "drawing", object_id, purpose or found, focus, data=body, revision=0
            )
        else:
            object_id = f"sheet:{uuid.uuid4()}"
            if (refused := p.held("show_view", "sheet", object_id)) is not None:
                return refused
            ack = await p.show("show_view", "sheet", object_id, purpose or found, focus, data=data)
        left = f" Left out: {'; '.join(dropped[:3])}." if dropped else ""
        return (ack if isinstance(ack, str) else ack.text()) + left
