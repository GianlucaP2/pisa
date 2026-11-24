# ruff: noqa: E501
"""US retail through UCP, as a cart the user pays themselves. "Comprami delle lampadine su
Target": Pisa searches the merchant's catalog (its UCP endpoint), builds the cart, and puts the merchant's own
checkout link on the phone as a `open_url` button with the host shown. Pisa never buys, pays or orders: the
user reviews the cart and pays on the merchant's page (their tap there is the yes).

RAILS (code, never the model's):
- only merchants on the allowlist (pisa/domain/world.py, `app_settings.world.merchants`); any other is never
  contacted;
- a cart holds only products this conversation's search found (their ids), 1 to 20 each;
- a price is shown only as the merchant stated it (its own amounts and total; Pisa never computes one), and the
  merchant's words reach the model as outside text (`merchant` data blocks);
- the checkout link is the merchant's own (https on its domain, plain, no redirect), its host on the button;
- no UCP endpoint (or it failed): the ladder's next rung, the merchant's own search page for their words.
"""

import time
import uuid
from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent

from pisa.brain.handoff_tools import Shower
from pisa.brain.profile_tools import ProfileTurn
from pisa.brain.purpose import purposeful
from pisa.brain.registry import offer
from pisa.brain.untrusted import data_block
from pisa.domain import handoffs as hrules
from pisa.domain import world as rules
from pisa.ports.world import WorldData

FOUND_FOR_S = 1800.0  # a search's products can go in a cart this long after


def _plain(text: object, limit: int) -> str:
    return " ".join(hrules.clean(str(text)).split())[:limit]


def _merchant_block(text: str) -> str:
    block = data_block("merchant", text, what="what the merchant's catalog or cart says (names and prices)")
    return block.render()


def add_world_tools(
    agent: Agent[None, str],
    world: WorldData,
    show: Shower,
    turn: ProfileTurn,
    *,
    now: Callable[[], float] = time.monotonic,
) -> None:
    found: dict[tuple[str, str], float] = {}  # (merchant key, product id) → when Pisa's search found it

    def lang() -> str:
        return turn.lang

    def italian() -> bool:
        return not lang().startswith("en")

    async def allowed(merchant: str) -> dict[str, Any] | str:
        m = await world.merchant(merchant)
        if m is None:
            return (
                f"Not prepared: Pisa talks only to the merchants on the user's list, and {_plain(merchant, 40)!r} "
                "isn't one. Say so; they can open the store's site themselves."
            )
        return m

    async def search_sheet(m: dict[str, Any], query: str, url: str) -> str:
        """The deep-link rung: the merchant's own search page for their words."""
        host = hrules.site_host(url)
        buttons = [{
            "id": str(uuid.uuid4()),
            "kind": "open_url",
            "title": f"Cerca su {m['name']}" if italian() else f"Search {m['name']}",
            "subtitle": f"{host} · la ricerca di {m['name']}" if italian() else f"{host} · {m['name']}'s search",
            "url": url,
        }]  # fmt: skip
        return await show(
            "prepare_cart",
            "shopping",
            m["name"],
            f"{_plain(query, 60)} su {m['name']}" if italian() else f"{_plain(query, 60)} at {m['name']}",
            {"merchant": m["key"], "route": "deep_link"},
            [],
            buttons,
            f"{m['name']} has no cart Pisa can build (no UCP endpoint): the button opens its own search for "
            f"«{_plain(query, 60)}»; they pick and pays there.",
            site=url,
        )

    @offer(agent)
    @purposeful("look")
    async def find_products(merchant: str, query: str) -> str:
        """Search a US store's catalog for what the user wants to buy ("comprami delle lampadine su Target"):
        the products with their prices as the store states them. Then prepare_cart with the ones that fit. Pisa
        never buys.

        Args:
            merchant: the store, as they said it ("Target", "Walmart").
            query: what they want, in the store's language (English for a US store: "light bulbs").
        """
        m = await allowed(merchant)
        if isinstance(m, str):
            return m
        query = _plain(query, 120)
        if not query:
            return "Not searched: say what to look for."
        got = await world.find_products(m["key"], query)
        if got.get("route") != "protocol" or not got.get("products"):
            return await search_sheet(m, query, got["search_url"])
        lines = []
        for p in got["products"]:
            found[(m["key"], p["id"])] = now()
            lines.append(f"[{p['id']}] {p['title']} — {rules.money(p['price'], lang())}")
        return (
            f"{m['name']}'s catalog (UCP), prices as {m['name']} states them (never say another price):\n"
            + _merchant_block("\n".join(lines))
            + "\nPick what fits their words and call prepare_cart with those ids; nothing is bought."
        )

    @offer(agent)
    @purposeful("propose")
    async def prepare_cart(merchant: str, product_ids: list[str], quantities: list[int] | None = None) -> str:
        """Build the cart at a US store (UCP) with products find_products found, and put the store's own
        checkout link on their phone: they review and pays there themselves. Pisa never buys, pays or orders.

        Args:
            merchant: the store, as they said it.
            product_ids: the ids from find_products, one per product.
            quantities: how many of each (default 1), in the same order.
        """
        m = await allowed(merchant)
        if isinstance(m, str):
            return m
        qty = list(quantities or [])
        items: list[tuple[str, int]] = []
        for i, pid in enumerate(product_ids[: rules.ITEMS_MAX]):
            n = qty[i] if i < len(qty) else 1
            if not 1 <= n <= rules.QUANTITY_MAX:
                return f"Not prepared: a quantity must be between 1 and {rules.QUANTITY_MAX}."
            seen = found.get((m["key"], str(pid)))
            if seen is None or now() - seen > FOUND_FOR_S:
                return (
                    f"Not prepared: {_plain(pid, 40)!r} isn't a product Pisa's search found at {m['name']} in this "
                    "conversation. Call find_products first."
                )
            items.append((str(pid), n))
        if not items:
            return "Not prepared: no products; call find_products first."
        got = await world.build_cart(m["key"], items, lang())
        if got.get("route") != "protocol":
            why = "its cart failed" if got.get("failed") else "it has no UCP cart"
            return f"Not prepared: {m['name']} gave no checkout link ({why}). Offer its own search instead."
        url = got["continue_url"]
        host = hrules.site_host(url)
        facts: list[tuple[str, str]] = [
            (f"{ln['title'][:30]} x{ln['quantity']}", ln["total_text"] or ln["price_text"])
            for ln in got["lines"]
        ]
        if got.get("total_text"):
            facts.append(("Totale" if italian() else "Total", f"{got['total_text']} ({m['name']})"))
        buttons = [{
            "id": str(uuid.uuid4()),
            "kind": "open_url",
            "title": f"Rivedi e paga su {m['name']}" if italian() else f"Review and pay on {m['name']}",
            "subtitle": (f"{host} · il carrello di {m['name']}, paghi tu lì" if italian()
                         else f"{host} · {m['name']}'s cart, you pay there"),
            "url": url,
            "notes": ["Prezzi come li indica il negozio." if italian() else "Prices as the store states them."],
        }]  # fmt: skip
        result = await show(
            "prepare_cart",
            "shopping",
            m["name"],
            f"Carrello su {m['name']}" if italian() else f"Cart at {m['name']}",
            {"merchant": m["key"], "route": "protocol", "cart_id": got["cart_id"], "items": len(got["lines"])},
            facts,
            buttons,
            "The cart is the store's; they review and pays on its page.",
            site=url,
        )  # fmt: skip
        if not result.startswith("Not prepared"):
            stated = "; ".join(f"{ln['title']} x{ln['quantity']}: {ln['total_text'] or ln['price_text']}"
                               for ln in got["lines"])  # fmt: skip
            total = f"; total {got['total_text']}" if got.get("total_text") else ""
            result += "\n" + _merchant_block(f"{m['name']}'s cart, as it states it: {stated}{total}")
        return result
