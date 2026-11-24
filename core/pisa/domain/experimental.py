"""Experimental features: everything built is supported and on; only stubs and unbuilt or unused integrations
are experimental, off by default. Pure rules, no I/O.

`REGISTRY` lists every feature that can be switched, by a short stable id (`google`, `garmin`, `reminders`,
`step_rituals`, …): its kind, what it gates, and whether it is experimental. The operator turns experimental
features on with `PISA_EXPERIMENTAL` (a comma-separated list of ids, or `all`); `enabled` parses it and `on`
answers for one feature. A supported feature (`experimental=False`) is always on, whatever the list says.

An off feature doesn't exist for the user: its connectors are absent from `/v1/connectors` (their routes
answer 404), its tools are not offered to the model, "what can you do" neither counts it nor names it as "not
connected yet", its worker steps don't run and `pisa setup` doesn't offer it. Discovery (`GET /v1/server`)
lists each enabled experimental feature as `experimental.<id>`.

The rule: a feature built end to end is supported (`experimental=False`), on by default, whether or not it has
been tested (supported doesn't mean tested). Experimental are only stubs (a row with no provider) and
integrations never built or never used against the real service. What is supported, and the evidence for
each, is `docs/pisa/SUPPORT-MATRIX.md`. Promoting a feature is one line: its `Feature(` becomes `_built(`.
What can't be switched (internal
plumbing, the CLI's admin commands) is listed in that document only.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

Kind = Literal["connector", "tools", "step", "voice"]
ALL_WORD = "all"
# discovery's `experimental.<id>` stays within contract 2's Feature id (^[a-z][a-z0-9_.]{1,60}$)
ID_PATTERN = r"^[a-z][a-z0-9_]{1,46}$"


@dataclass(frozen=True)
class Feature:
    id: str
    kind: Kind
    gates: str  # what it gates, in a few words
    experimental: bool = True
    # what the user calls it, in English and Italian, as a list names it ("bank linking", "Garmin"): the one
    # wording of "<label> isn't available on PISA" (`unavailable`); none: its id, spaced
    label: str = ""
    label_it: str = ""


def _built(feature_id: str, kind: Kind, gates: str, label: str = "", label_it: str = "") -> Feature:
    """A feature built end to end: supported, always on."""
    return Feature(feature_id, kind, gates, experimental=False, label=label, label_it=label_it)


def _step(name: str, gates: str) -> Feature:
    """A worker step of the minute (`services.worker_steps.STEPS`), gated by `step_<its name>`: every one is
    built, so supported."""
    return Feature(f"step_{name}", "step", f"the worker step `{name}`: {gates}", experimental=False)


REGISTRY: tuple[Feature, ...] = (
    # The rule: what is built end to end is supported, on by default (`_built`); only the stubs and the
    # integrations never built or never used stay experimental, off until PISA_EXPERIMENTAL names them.
    # Supported doesn't mean tested: the evidence per feature is docs/pisa/SUPPORT-MATRIX.md.
    # --- connectors: experimental are garmin (built, unused), bank, radar and ade (never run against the
    # real service), polar and withings (rows, no provider) -------------------------------------------------
    _built("google", "connector", "Google (Gmail + Calendar): the connector and its routes, the mail and "
            "calendar tools, the gmail-sync and calendar-sync schedules",
            label="Google (Gmail and Google Calendar)", label_it="Google (Gmail e Google Calendar)"),
    _built("apple_health", "connector", "Apple Health: the connector and POST /v1/health/summary",
            label="Apple Health", label_it="Apple Salute"),
    Feature("garmin", "connector", "Garmin: the connector and its sync",
            label="Garmin", label_it="Garmin"),
    Feature("bank", "connector", "the banks (Enable Banking, read-only): bank, bank_2, bank_3, their consent "
            "and return routes",
            label="bank linking", label_it="il collegamento al conto in banca"),
    Feature("radar", "connector", "the admin radar: admin_mail, admin_pec, admin_documents, admin_calendar",
            label="PEC and admin mail linking", label_it="il collegamento di PEC e posta amministrativa"),
    Feature("ade", "connector", "Agenzia delle Entrate's tax drawer (ade_cassetto)",
            label="the cassetto fiscale (Agenzia delle Entrate)",
            label_it="il cassetto fiscale dell'Agenzia delle Entrate"),
    Feature("polar", "connector", "Polar (a row, no provider yet)",
            label="Polar", label_it="Polar"),
    Feature("withings", "connector", "Withings (a row, no provider yet)",
            label="Withings", label_it="Withings"),
    _built("push_relay", "connector", "push through the relay (PISA_PUSH_RELAY_URL): relay mode in "
            "discovery, registration and sending",
            label="push through the relay", label_it="il push tramite relay"),
    # --- tools offered to the model: all built, all supported --------------------------------------------
    _built("reminders", "tools", "reminders: `remember_to`, and the worker's reminders and attend_floors "
            "steps (until the owner sees one fire)",
            label="setting reminders", label_it="l'impostazione dei promemoria"),
    _built("places", "tools", "places, maps handoffs and research: the places family but `get_weather`, "
            "`start_navigation`, the place cards",
            label="searching places, maps and the web", label_it="la ricerca di luoghi, mappe e web"),
    _built("handoffs", "tools", "Pisa prepares, the user taps: the handoffs family and the store carts",
            label="handing off to apps and stores", label_it="il passaggio ad app e negozi"),
    _built("phone_actions", "tools", "messages and calls from the phone: `compose_message`, `call_place`",
            label="sending messages and calls", label_it="l'invio di messaggi e chiamate"),
    _built("undo", "tools", "`undo_last_change` and `answer_proposal`"),  # audited (F8)
    _built("decisions", "tools", "`my_recent_decisions`"),
    _built("team", "tools", "`tell_the_team`"),
    _built("views", "tools", "Pisa's views"),
    _built("drawing", "tools", "drawings (also off until PISA_DRAWING_BOX=on)",
            label="drawing", label_it="il disegno"),
    _built("rituals", "tools", "rituals, place notes, hunches"),
    _built("autopilot", "tools", "the vault, bills, skills and autonomy (stop and resume stay on)"),
    _built("learn", "tools", "Pisa Learn",
            label="Pisa Learn", label_it="Pisa Learn"),
    _built("profile", "tools", "the profile (\"about me\")"),
    _built("note", "tools", "Memoria: their standing instructions and their notes"),
    _built("trips", "tools", "trips and residency"),
    _built("explore", "tools", "the fog of war, the map of life"),
    _built("night", "tools", "`stanotte`"),
    _built("memory", "tools", "`recall`"),
    _built("people", "tools", "the people they love"),
    _built("money", "tools", "money back, warranties"),
    _built("public", "tools", "transit, weather alerts, ZTL, pharmacies, airport",
            label="public data (transit, weather alerts, ZTL, pharmacies)",
            label_it="i dati pubblici (trasporti, allerte meteo, ZTL, farmacie)"),
    # --- voice: the xAI talker failed as the default (natural voice uses Gemini), so it stays experimental --
    _built("tts_server", "voice", "the server's voices (OpenAI speech, ElevenLabs): without it the phone "
            "speaks",
            label="the server's voice", label_it="la voce del server"),
    Feature("talker_xai", "voice", "natural voice with the xAI talker (else Gemini)",
            label="the xAI voice", label_it="la voce xAI"),
    # --- the worker's steps: all built, all supported (`_step`) ---------------------------------------------
    _step("briefing_time", "the briefing's scheduled push"),
    _step("done", "done by being there"),
    _step("plan", "the day's re-plans"),
    _step("check_ins", "check-ins"),
    _step("aging", "aging"),
    _step("attend", "Pisa speaks first"),
    _step("live_activity", "the Live Activity"),
    _step("guardian", "the guardian"),
    _step("handoffs", "the handoffs' follow-up"),
    _step("money_back", "money back"),
    _step("weekly_check", "Sunday's weekly check"),
    _step("owner_loop", "the week's talk report"),
    _step("sunday_card", "the Sunday card"),
    _step("vault_reminders", "the vault's renewals"),
    _step("vault_starters", "the research's first reminders"),
    _step("skills", "scheduled skills"),
    _step("personal_evals", "a quick undo as a private test"),
    _step("rituals", "the rituals"),
    _step("people", "birthdays, orbits, thank-yous"),
    _step("errands", "the errand combo"),
    _step("public_data", "public datasets' downloads"),
    _step("trip_leaving", "3 days before a trip"),
    _step("learn", "the night (03:30 learning, then the night)"),
)  # fmt: skip

# App Store review mode keeps these supported features off too, as it always has: they reach outside the
# server (Google, maps and the web, messages and calls, the push relay, the server's voices, public data's
# downloads, Pisa Learn's fetches of papers and URLs and the night's learning, the handoffs' store carts with
# the merchants over UCP: the `world` family goes with `handoffs`) or are connectors (the reviewer's
# Connectors screen is empty). Google's, the places providers', Learn's fetcher and the UCP client are
# switched off by review mode too (`disabled`), whatever the database holds.
REVIEW_OFF: frozenset[str] = frozenset({
    "google", "apple_health", "places", "phone_actions", "push_relay", "tts_server", "public",
    "step_public_data", "learn", "handoffs", "step_learn",
})  # fmt: skip
# review mode's mark in an enabled set (`enabled(..., review_mode=True)`): never an id (out of `ID_PATTERN`)
REVIEW = "@review"

BY_ID: dict[str, Feature] = {f.id: f for f in REGISTRY}
if len(BY_ID) != len(REGISTRY):  # a check that holds under `python -O` too
    raise RuntimeError("an experimental feature declared twice")
if _bad := [f.id for f in REGISTRY if not re.match(ID_PATTERN, f.id)] + sorted(REVIEW_OFF - BY_ID.keys()):
    raise RuntimeError(f"an experimental feature id out of its pattern, or unknown: {', '.join(_bad)}")
EXPERIMENTAL: frozenset[str] = frozenset(f.id for f in REGISTRY if f.experimental)
ALL: frozenset[str] = frozenset(BY_ID)  # every feature on (`PISA_EXPERIMENTAL=all`)


def parse(spec: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """PISA_EXPERIMENTAL ("google,garmin", "all", "") as (the enabled ids, the unknown ids, for the caller to
    warn about). Case and spaces don't matter."""
    words = [w.strip().lower() for w in spec.split(",") if w.strip()]
    if REVIEW in words:  # review mode's mark (`review_spec`): every experimental feature off
        return frozenset({REVIEW}), ()
    if ALL_WORD in words:
        return ALL, tuple(w for w in words if w != ALL_WORD and w not in BY_ID)
    return frozenset(w for w in words if w in BY_ID), tuple(w for w in words if w not in BY_ID)


def enabled(spec: str, *, review_mode: bool = False) -> frozenset[str]:
    """The experimental features PISA_EXPERIMENTAL turns on; an unknown id is ignored (`unknown` names it).
    Under App Store review mode (PISA_REVIEW_MODE) none, whatever the list says, and the supported ones of
    `REVIEW_OFF` are off too: every connector is absent, nothing reaches outside the server but the AI
    provider and the weather (docs-site/app-review.md)."""
    return parse(review_spec(spec, review_mode=review_mode))[0]


def review_spec(spec: str, *, review_mode: bool) -> str:
    """The spec a worker passes on (a string): review mode's mark under review mode, else the operator's."""
    return REVIEW if review_mode else spec


def unknown(spec: str) -> tuple[str, ...]:
    """The ids PISA_EXPERIMENTAL names that no feature has (the caller logs a warning)."""
    return parse(spec)[1]


def on(feature_id: str, enabled: Iterable[str]) -> bool:
    """The feature is on: supported (always on), or experimental and enabled. An id the registry doesn't have
    is a programming error."""
    feature = BY_ID[feature_id]
    chosen = frozenset(enabled)
    if REVIEW in chosen:  # review mode: no experimental feature, nor a supported one of `REVIEW_OFF`
        return not feature.experimental and feature_id not in REVIEW_OFF
    return not feature.experimental or feature_id in chosen


def any_on(feature_ids: Iterable[str], enabled: Iterable[str]) -> bool:
    chosen = frozenset(enabled)
    return any(on(f, chosen) for f in feature_ids)


def listed(enabled: Iterable[str]) -> list[str]:
    """The enabled experimental features, in the registry's order (discovery's `experimental.<id>`)."""
    chosen = frozenset(enabled)
    return [f.id for f in REGISTRY if f.experimental and f.id in chosen]


# --- what each feature hides: connectors, tool families, single tools ---------------------------------------

# each connector of `domain.connectors.REGISTRY` by its feature
CONNECTORS: dict[str, str] = {
    "google": "google",
    "apple_health": "apple_health",
    "garmin": "garmin",
    "bank": "bank",
    "bank_2": "bank",
    "bank_3": "bank",
    "admin_mail": "radar",
    "admin_pec": "radar",
    "admin_documents": "radar",
    "admin_calendar": "radar",
    "ade_cassetto": "ade",
    "polar": "polar",
    "withings": "withings",
}

# tool families (`brain.conversation.ToolFamilies` fields) a feature hides whole: the family's service is None
FAMILIES: dict[str, str] = {
    "views": "views",
    "rituals": "rituals",
    "learn": "learn",
    "profile": "profile",
    "memoria": "note",
    "trips": "trips",
    "explore": "explore",
    "night": "night",
    "memory": "memory",
    "people": "people",
    "money": "money",
    "handoffs": "handoffs",
    "world": "handoffs",
    "public": "public",
}

# single tools a feature hides, where a family has a supported part (tasks, places, autopilot, actions) or the
# family's service is shared (drawing, decisions, team)
TOOLS: dict[str, frozenset[str]] = {
    # the Google connector's own tools go with it (`domain.connectors.GOOGLE_TOOLS`); these read its mail too
    "google": frozenset({"show_email", "thing"}),
    "reminders": frozenset({"remember_to"}),
    "places": frozenset({
        "detour", "events_on", "map_search", "my_findings", "open_at", "place_details", "read_page",
        "shop_prices", "show_events", "show_findings", "show_places", "show_products", "table_slots",
        "travel_time", "walk_times", "web_search", "where_am_i", "start_navigation", "compare_places",
        "show_place",
    }),
    "phone_actions": frozenset({"compose_message", "call_place"}),
    "undo": frozenset({"undo_last_change", "answer_proposal"}),
    "autopilot": frozenset({
        "accept_autonomy", "accountant_package", "add_document", "ask_again", "list_bills", "list_documents",
        "note_aire", "rename_skill", "run_skill", "schedule_skill", "wish_skill",
    }),
    "decisions": frozenset({"my_recent_decisions"}),
    "team": frozenset({"tell_the_team"}),
    "drawing": frozenset({"my_drawing", "save_drawing", "show_drawing"}),
}  # fmt: skip


def connector_on(connector_id: str, enabled: Iterable[str]) -> bool:
    """A connector of `domain.connectors.REGISTRY` exists on this install (one not listed here never)."""
    feature = CONNECTORS.get(connector_id)
    return feature is not None and on(feature, frozenset(enabled))


def families_off(enabled: Iterable[str]) -> frozenset[str]:
    """The tool families whose service is withheld (their tools are not offered)."""
    chosen = frozenset(enabled)
    return frozenset(family for family, feature in FAMILIES.items() if not on(feature, chosen))


def tools_off(enabled: Iterable[str]) -> frozenset[str]:
    """The single tools never offered (their feature is off)."""
    chosen = frozenset(enabled)
    return frozenset(t for feature, tools in TOOLS.items() if not on(feature, chosen) for t in tools)


# --- what the user calls a feature that is off: one wording everywhere --------------------------------------
# (search_tools' off answer, show_setting's refusal, the reply rail's line, the prompt's list)

_UNAVAILABLE = {"en": "{label} isn't available on PISA.", "it": "{label} non è disponibile su PISA."}
# the words that name a feature in their words or Pisa's, Italian and English (only the features someone
# would ask to connect or set up: the connectors and the xAI voice)
_NAMED: dict[str, re.Pattern[str]] = {
    feature: re.compile(rf"\b(?:{words})\b", re.IGNORECASE)
    for feature, words in {
        "google": r"gmail|google(?!\s+maps)",
        "apple_health": r"apple\s+(?:health|salute)|health\s+app|app\s+salute",
        "garmin": r"garmin",
        "bank": r"banc(?:a|he|ari[oa]?|arie)|bank(?:s|ing)?(?!\s+holiday)|conti?\s+corrent[ei]|"
        r"enable\s+banking|home\s+banking",
        "radar": r"pec|posta\s+(?:elettronica\s+)?certificata|posta\s+amministrativa|admin\s+mail",
        "ade": r"cassetto\s+fiscale|ade\s+cassetto|agenzia\s+(?:delle\s+)?entrate|tax\s+drawer",
        "polar": r"polar(?!\s+bears?)",
        "withings": r"withings",
        "talker_xai": r"xai|grok",
    }.items()
}


def label(feature_id: str, lang: str = "en") -> str:
    """What the user calls the feature, as a list names it ("bank linking", "Garmin"), in `lang`."""
    feature = BY_ID[feature_id]
    italian = lang.lower().startswith("it")
    return (feature.label_it if italian else feature.label) or feature.label or feature_id.replace("_", " ")


def not_on_pisa(name: str, lang: str = "en") -> str:
    """ "<name> isn't available on PISA." in `lang` (`name`: a feature's `label`)."""
    table = _UNAVAILABLE["it" if lang.lower().startswith("it") else "en"]
    return table.format(label=name[:1].upper() + name[1:])


def unavailable(feature_id: str, lang: str = "en") -> str:
    """The one line for a feature that is off: "Bank linking isn't available on PISA." in `lang`."""
    return not_on_pisa(label(feature_id, lang), lang)


def named(text: str) -> list[str]:
    """The features whose words `text` names ("collega il mio conto in banca": bank), in the registry's
    order; only those someone would ask to connect (`_NAMED`). An id's underscores count as spaces."""
    plain = text.replace("_", " ")
    return [f.id for f in REGISTRY if (words := _NAMED.get(f.id)) is not None and words.search(plain)]


def named_off(text: str, enabled: Iterable[str]) -> list[str]:
    """The features `text` names that are off on this install."""
    chosen = frozenset(enabled)
    return [f for f in named(text) if not on(f, chosen)]


def off_labels(enabled: Iterable[str], lang: str = "en") -> list[str]:
    """What is off on this install, by its label, in the registry's order (the worker's steps aside)."""
    chosen = frozenset(enabled)
    return [label(f.id, lang) for f in REGISTRY if f.kind != "step" and not on(f.id, chosen)]


def first_off(texts: Sequence[str], enabled: Iterable[str]) -> str | None:
    """The first feature off on this install that one of `texts` names, or None."""
    chosen = frozenset(enabled)
    return next((f for t in texts for f in named_off(t, chosen)), None)
