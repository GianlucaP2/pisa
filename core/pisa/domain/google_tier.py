"""Google AI's tier (Gemini API terms, ai.google.dev/gemini-api/terms): the paid services' data-use terms
apply to all use in the EEA, Switzerland and the UK, unpaid quota included, and only paid services may be
made available to users there (GDPR Art. 28 and 44). Pisa can't read a key's tier or where its owner is, so
the operator declares it (`google_ai_tier`, `pisa setup` asks):

- `paid`: billing is on for the key's project. Google's models and Gemini Live.
- `free_eea_own_use`: the operator's own declaration, "I am in the EEA, Switzerland or the UK, and I am the
  only person who uses this Pisa with this key." Gemini Live only (D-142: Google for speech); the model
  gateway still needs paid. Anyone serving other users must use paid.
- `unknown`, `free`: nothing of theirs goes to Google.

No I/O here.
"""

from urllib.parse import urlsplit

TIERS: tuple[str, ...] = ("unknown", "free", "free_eea_own_use", "paid")
TALKER_TIERS: frozenset[str] = frozenset({"paid", "free_eea_own_use"})
MODEL_TIERS: frozenset[str] = frozenset({"paid"})
DEFAULT = "unknown"
PROVIDER = "google"  # the provider id whose models and talker the tier governs
CODE = "google_ai_tier_not_paid"

# the refusals (E1's): a Google model needs paid; Gemini Live names both ways out
_MODEL_MESSAGE = {
    "en": (
        "Google AI needs a paid tier to receive your data (Gemini API terms): Pisa doesn't send anything "
        "to Gemini on a free or unconfirmed key."
    ),
    "it": (
        "Google AI richiede un piano a pagamento per ricevere i tuoi dati (termini dell'API Gemini): Pisa "
        "non invia nulla a Gemini con una chiave gratuita o non confermata."
    ),
}
_MODEL_ACTION = {
    "en": (
        "Turn on billing for the key's Google Cloud project, then set Google AI tier to paid in "
        "Settings → Connections (or run pisa setup)."
    ),
    "it": (
        "Attiva la fatturazione nel progetto Google Cloud della chiave, poi imposta il piano di Google AI "
        "su a pagamento in Impostazioni → Collegamenti (o esegui pisa setup)."
    ),
}
_TALKER_MESSAGE = {
    "en": (
        "Google AI needs a paid tier, or a free key used only by you in the EEA, Switzerland or the UK, to "
        "receive your data (Gemini API terms): Pisa doesn't send anything to Gemini until you confirm one."
    ),
    "it": (
        "Google AI richiede un piano a pagamento, o una chiave gratuita usata solo da te nel SEE, in "
        "Svizzera o nel Regno Unito, per ricevere i tuoi dati (termini dell'API Gemini): Pisa non invia "
        "nulla a Gemini finché non ne confermi uno."
    ),
}
_TALKER_ACTION = {
    "en": (
        "Turn on billing for the key's Google Cloud project and set Google AI tier to paid, or, if only you "
        "use this Pisa and you're in the EEA, Switzerland or the UK, set it to free for your own use, in "
        "Settings → Connections (or run pisa setup)."
    ),
    "it": (
        "Attiva la fatturazione nel progetto Google Cloud della chiave e imposta il piano di Google AI su a "
        "pagamento, oppure, se questo Pisa lo usi solo tu e sei nel SEE, in Svizzera o nel Regno Unito, "
        "impostalo su gratuito per uso personale, in Impostazioni → Collegamenti (o esegui pisa setup)."
    ),
}


def governs(provider_id: str, base_url: str) -> bool:
    """The provider is Google's: its own, or a server the user added on a Google API host (Gemini's
    OpenAI-compatible endpoint, `*.googleapis.com`; E1 GT-1)."""
    host = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    return provider_id == PROVIDER or host == "googleapis.com" or host.endswith(".googleapis.com")


def _tier(settings: object) -> str:
    return str(settings.get("google_ai_tier") or DEFAULT) if isinstance(settings, dict) else DEFAULT


def talker_allowed(settings: object) -> bool:
    """Gemini Live may hear them: a paid key, or the operator's own-use declaration in the EEA, CH or UK."""
    return _tier(settings) in TALKER_TIERS


def models_allowed(settings: object) -> bool:
    """A Google model may receive their data: a paid key only (D-142)."""
    return _tier(settings) in MODEL_TIERS


def refusal(lang: str, *, talker: bool) -> tuple[str, str]:
    """(message, action) in `lang` (Italian for "it…", else English), for the talker or for a model."""
    code = "it" if lang.lower().startswith("it") else "en"
    if talker:
        return _TALKER_MESSAGE[code], _TALKER_ACTION[code]
    return _MODEL_MESSAGE[code], _MODEL_ACTION[code]
