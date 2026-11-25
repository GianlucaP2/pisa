"""Collegamenti (Connections): every outside service Pisa can reach, in six categories, and the user's OK of a
paid one. Pure rules, no I/O.

**The view** (`GET /v1/connections`) is read-only: it aggregates what the providers' own endpoints already
say (AI providers and model servers, the voices, the places and web providers, the Google account, the
notification paths). Each entry says what it is, its state, whether it costs money, what leaves the server and
to whom (`DATA_OUT`: a fixed phrase of code, never the user's data), its docs page and the actions a client
may offer (each one an existing endpoint, `ACTIONS`).

**Paid** (`paid`): a metered service with a price in Pisa's tables: the AI providers' and voices' (the model
catalog's prices, `domain.voice`'s), Google Maps' and Tavily's (`domain.place_prices`). The phone's maps,
Apple Maps (a daily quota, never billed), OpenStreetMap, OpenRouteService's free plan, the user's own model
server, the Google account and the notification paths are not.

**The paid OK** (`paid_ack`): a maps or web provider that bills beyond a free allowance (`ACKED`: every
provider of `place_prices.PRICES` with a paid tier, Google Maps and Tavily today) is never called until the
user acknowledged it and its own monthly cap, in cents (at most the server's hard monthly limit), with a
signed `device` op `paid_ack`, target `<provider>:<cap in cents>`. The AI providers and the voices keep their
own guard (the AI consent, the daily and monthly AI caps): no paid OK for them in v0.1.4.
"""

from typing import Literal

from pisa.domain.errors import PisaError
from pisa.domain.place_prices import PRICES

Category = Literal["ai", "voice", "maps", "web", "accounts", "notifications"]
State = Literal["on", "off", "not_set_up", "error", "needs_reconnect"]
Action = Literal["set_key", "delete_key", "connect", "disconnect", "test", "ack_paid"]
CATEGORIES: tuple[Category, ...] = ("ai", "voice", "maps", "web", "accounts", "notifications")
CATEGORY_NAMES: dict[Category, str] = {
    "ai": "AI models",
    "voice": "Voice",
    "maps": "Maps & places",
    "web": "Web research",
    "accounts": "Accounts",
    "notifications": "Notifications",
}
ACTIONS: tuple[Action, ...] = ("set_key", "delete_key", "connect", "disconnect", "test", "ack_paid")

# the maps and web providers that bill beyond a free allowance: called only after the user's paid OK
ACKED: frozenset[str] = frozenset(p.provider for p in PRICES.values() if p.free_per_month is not None)
ACK_OP = "paid_ack"  # the `device` op: target `<provider>:<cap in cents>`
CAP_CENTS_MIN = 1
# what a turn and a REST error say of a paid provider set up but not acknowledged (the `detail`)
ACK_DETAIL = "paid_ack_required"
ACK_REASON = "paid_ack_required"  # the error body's `reason`

DOCS: dict[Category, str] = {
    "ai": "ai-providers.md",
    "voice": "ai-providers.md",
    "maps": "places-and-web.md",
    "web": "places-and-web.md",
    "accounts": "connectors.md",
    "notifications": "notifications.md",
}

# what leaves the server, and to whom: code's own words, never the user's data (a server's own name is the
# user's, so a model server is "your own model server")
DATA_OUT: dict[str, str] = {
    "ai.builtin": "Your words and the context Pisa sends with them, to {name}",
    "ai.server": "Your words and the context Pisa sends with them, to your own model server",
    "voice.device": "Nothing: the phone speaks with its own voice",
    "voice.openai": "The text of Pisa's replies, to OpenAI (speech)",
    "voice.elevenlabs": "The text of Pisa's replies, to ElevenLabs",
    "voice.gemini": "Your voice and the conversation's context, to Google (Gemini Live)",
    "voice.xai": "Your voice and the conversation's context, to xAI",
    "maps.phone": "Nothing from the server: the phone asks its own maps",
    "maps.apple": "Place searches and trip ends, to Apple Maps",
    "maps.google": "Place searches, place ids and trip ends, to Google Maps",
    # on by default (PISA_OSM_ENABLED): the public services, unless the server points the URLs at its own
    "maps.osm": (
        "On by default: the words of each place search and a point rounded to about 100 m, to the public "
        "OpenStreetMap services Photon, Nominatim and Overpass, or to your server's own instances of them"
    ),
    # a route needs its real ends: 5 decimals, which can be the user's home
    "maps.openrouteservice": (
        "The real start and end points of each trip (to about 1 m, which can be your home), to "
        "OpenRouteService"
    ),
    "web.tavily": "Web searches and page addresses, to Tavily",
    "accounts.google": "Calendar changes and drafts you approve, to Google; your mail and events come in",
    "notifications.phone": "Alerts' times and titles, to your phone over its own connection",
    "notifications.relay": "Sealed notices (end-to-end encrypted), to the push relay, then Apple",
    "notifications.direct": "Notices, to Apple's push service (APNs)",
}

# the voices' and talkers' key provider (`brain.models.providers`): their key is the AI provider's
VOICE_KEYS: dict[str, str] = {
    "openai": "openai",
    "elevenlabs": "elevenlabs",
    "gemini": "google",
    "xai": "xai",
}


def data_out(key: str, name: str = "") -> str:
    return DATA_OUT[key].format(name=name)


def ack_target(provider: str, cap_cents: int) -> str:
    """The signed op's target: `<provider>:<cap in cents>` (the canonical text says `paid_ack` and it)."""
    return f"{provider}:{cap_cents}"


def check_ack(provider: str, cap_cents: int, hard_monthly_cents: int) -> None:
    """400 `validation_failed` (404 `not_found` for a provider that needs no OK), before any signature is
    spent: the cap is whole cents, at least 1, at most the server's hard monthly limit."""
    if provider not in ACKED:
        raise PisaError(
            "not_found",
            f"There is no paid maps or web provider {provider[:40]!r} to acknowledge.",
            f"Use one of {', '.join(sorted(ACKED))}",
        )
    if isinstance(cap_cents, bool) or not isinstance(cap_cents, int) or cap_cents < CAP_CENTS_MIN:
        raise PisaError(
            "validation_failed",
            "The cap (cap_cents) must be a whole number of cents, at least 1.",
            "Fix the request",
        )
    if cap_cents > hard_monthly_cents:
        raise PisaError(
            "validation_failed",
            f"The cap ({cap_cents} cents) is over this server's hard monthly limit ({hard_monthly_cents} "
            "cents), which no cap can pass.",
            f"Choose a cap of {hard_monthly_cents} cents or less",
        )


def ack_needed_message(names: list[str], job_phrase: str) -> str:
    """A job none of whose providers may run because a paid one waits for the user's OK (the model reads it,
    the REST error carries it): one line, Pisa's own words."""
    listed = " and ".join(names)
    if len(names) > 1:
        what = f"{listed} cost money and wait for your OK of their monthly caps"
    else:
        what = f"{listed} costs money and waits for your OK of its monthly cap"
    return f"No provider for {job_phrase} is ready: {what} in Settings → Connections."


ACK_ACTION = "Acknowledge it and its monthly cap in Settings → Connections (Collegamenti)"
