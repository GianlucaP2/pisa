# ruff: noqa: E501 (the guided-setup texts are prose, one line each)
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

**Guided setup** (`setup`, `free_tier`): each entry may carry the short steps to set it up and its free quota,
in the user's language, in the words of the docs (docs-site/ai-providers.md, places-and-web.md,
connectors.md). Only what the docs already state: an entry with no steps has an empty list, one with no known
free tier has none (null), never a guess.
"""

from typing import Literal

from pisa.domain import locale
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


# the steps to set an entry up, by entry id, then language (the docs' own words, shortened)
SETUP: dict[str, dict[str, tuple[str, ...]]] = {
    "ai.anthropic": {
        "en": (
            "Open the Claude Console at platform.claude.com and sign in, or create an account.",
            "On the API keys page, create a key named pisa.",
            "Copy the key: it is shown only once.",
            "Paste it into Pisa, then choose a model for each feature.",
        ),
        "it": (
            "Apri la Claude Console su platform.claude.com e accedi, o crea un account.",
            "Nella pagina delle chiavi API, crea una chiave chiamata pisa.",
            "Copia la chiave: viene mostrata una sola volta.",
            "Incollala in Pisa, poi scegli un modello per ogni funzione.",
        ),
    },
    "ai.openai": {
        "en": (
            "Sign in at platform.openai.com: the API platform is separate from a ChatGPT subscription.",
            "On the API keys page, create a secret key named pisa.",
            "Copy the key right away: you can't see it again.",
            "Paste it into Pisa, then choose a model for each feature.",
        ),
        "it": (
            "Accedi a platform.openai.com: la piattaforma API è separata da un abbonamento a ChatGPT.",
            "Nella pagina delle chiavi API, crea una chiave segreta chiamata pisa.",
            "Copia subito la chiave: non potrai rivederla.",
            "Incollala in Pisa, poi scegli un modello per ogni funzione.",
        ),
    },
    "ai.google": {
        "en": (
            "Sign in with your Google account at aistudio.google.com.",
            "On the API key page, click Create API key.",
            "Copy the key and paste it into Pisa, then choose a model for each feature.",
        ),
        "it": (
            "Accedi con il tuo account Google su aistudio.google.com.",
            "Nella pagina della chiave API, clicca Create API key.",
            "Copia la chiave e incollala in Pisa, poi scegli un modello per ogni funzione.",
        ),
    },
    "maps.apple": {
        "en": (
            "Join the Apple Developer Program and make a Maps private key (a .p8 file) with its key id.",
            "Set your Apple team id in PISA_APNS_TEAM_ID.",
            "Store the key in Pisa: pisa secrets set-maps --key-id <the key id> < AuthKey_XXXXXXXXXX.p8",
        ),
        "it": (
            "Iscriviti all'Apple Developer Program e crea una chiave privata Maps (un file .p8) con il suo id.",
            "Imposta il tuo team id Apple in PISA_APNS_TEAM_ID.",
            "Salva la chiave in Pisa: pisa secrets set-maps --key-id <l'id della chiave> < AuthKey_XXXXXXXXXX.p8",
        ),
    },
    "maps.google": {
        "en": (
            "In Google Cloud, turn on billing and enable the Places API (New) and the Routes API.",
            "Make an API key restricted to those two APIs.",
            "Paste the key into Pisa.",
            "Give your OK to Google Maps and its monthly cap in Settings → Connections: Pisa doesn't call it before.",
        ),
        "it": (
            "In Google Cloud, attiva la fatturazione e abilita la Places API (New) e la Routes API.",
            "Crea una chiave API limitata a quelle due API.",
            "Incolla la chiave in Pisa.",
            "Dai il tuo OK a Google Maps e al suo tetto mensile in Impostazioni → Collegamenti: prima Pisa non lo chiama.",
        ),
    },
    "maps.openrouteservice": {
        "en": (
            "Make a free account at openrouteservice.org and copy its API key.",
            "Paste the key into Pisa: travel times on foot, by bike and by car then work from OpenStreetMap.",
        ),
        "it": (
            "Crea un account gratuito su openrouteservice.org e copia la sua chiave API.",
            "Incolla la chiave in Pisa: i tempi di viaggio a piedi, in bici e in auto funzionano poi da OpenStreetMap.",
        ),
    },
    "web.tavily": {
        "en": (
            "Make a Tavily account at app.tavily.com and copy its API key.",
            "Paste the key into Pisa.",
            "Give your OK to Tavily and its monthly cap in Settings → Connections: Pisa doesn't call it before.",
        ),
        "it": (
            "Crea un account Tavily su app.tavily.com e copia la sua chiave API.",
            "Incolla la chiave in Pisa.",
            "Dai il tuo OK a Tavily e al suo tetto mensile in Impostazioni → Collegamenti: prima Pisa non lo chiama.",
        ),
    },
}

# the free quota of an entry, by entry id, then language: only what the docs already state; no entry here, no
# known free tier (null)
FREE_TIER: dict[str, dict[str, str]] = {
    "ai.google": {
        "en": "A free tier with limited models and rate limits; on it Google may use your content to improve its products.",
        "it": "Un piano gratuito con modelli e limiti di richieste ridotti; con quello Google può usare i tuoi contenuti per migliorare i suoi prodotti.",
    },
    "maps.apple": {
        "en": "Free with the Apple Developer Program: 25,000 calls a day, never billed.",
        "it": "Gratis con l'Apple Developer Program: 25.000 chiamate al giorno, mai a pagamento.",
    },
    "maps.google": {
        "en": "A free monthly allowance for each kind of call, paid per call above it.",
        "it": "Una quota mensile gratuita per ogni tipo di chiamata, a pagamento oltre quella.",
    },
    "maps.openrouteservice": {
        "en": "Free plan: a few hundred travel-time requests a day.",
        "it": "Piano gratuito: qualche centinaio di richieste di tempi di viaggio al giorno.",
    },
    "maps.osm": {
        "en": "Free, no key.",
        "it": "Gratis, senza chiave.",
    },
    "web.tavily": {
        "en": "1,000 free credits a month, then paid.",
        "it": "1.000 crediti gratuiti al mese, poi a pagamento.",
    },
}


def setup_steps(entry_id: str, lang: str) -> list[str]:
    """The steps to set an entry up, in `lang`; empty when the docs give none."""
    steps = SETUP.get(entry_id)
    return list(locale.pick(steps, lang)) if steps else []


def free_tier(entry_id: str, lang: str) -> str | None:
    """An entry's free quota in `lang`; None when the docs state none."""
    text = FREE_TIER.get(entry_id)
    return locale.pick(text, lang) if text else None


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
