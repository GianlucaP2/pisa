"""Provider registry: one row per AI provider.

Base URLs were checked against each provider's official docs. Native Pydantic AI providers are used where
one exists; otherwise the OpenAI-compatible client with a base URL.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
from pydantic_ai.models import Model

from pisa.domain import model_catalog
from pisa.domain.ai import Capability
from pisa.domain.errors import PisaError

if TYPE_CHECKING:
    from pisa.domain.model_caps import ModelCaps


@dataclass(frozen=True)
class ListedModel:
    """A model as a provider's own model-list endpoint describes it."""

    model_id: str
    name: str | None = None
    context_tokens: int | None = None
    capabilities: tuple[Capability, ...] | None = None  # None = the provider does not say


# --- model lists --------------------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _anthropic_caps(item: dict[str, Any]) -> tuple[Capability, ...] | None:
    caps = item.get("capabilities")
    if not isinstance(caps, dict):
        return None
    found: list[Capability] = ["tools"]  # every Claude model on the Messages API supports tool use

    def supported(name: str) -> bool:
        value = caps.get(name)
        return bool(value.get("supported")) if isinstance(value, dict) else bool(value)

    if supported("structured_outputs"):
        found.append("structured_output")
    if supported("image_input"):
        found.append("vision")
    return tuple(found)


def _mistral_caps(item: dict[str, Any]) -> tuple[Capability, ...] | None:
    caps = item.get("capabilities")
    if not isinstance(caps, dict):
        return None
    found: list[Capability] = []
    if caps.get("function_calling"):
        found.extend(("tools", "structured_output"))
    if caps.get("vision"):
        found.append("vision")
    if caps.get("audio") or caps.get("audio_transcription"):
        found.append("audio_in")
    if caps.get("audio_speech"):
        found.append("audio_out")
    return tuple(found)


def _google_caps(item: dict[str, Any]) -> tuple[Capability, ...] | None:
    methods = item.get("supportedGenerationMethods")
    if not isinstance(methods, list):
        return None
    if "embedContent" in methods and "generateContent" not in methods:
        return ("embeddings",)
    return None  # generation capabilities are not listed: use the other sources


def _cohere_caps(item: dict[str, Any]) -> tuple[Capability, ...] | None:
    endpoints = item.get("endpoints")
    if not isinstance(endpoints, list):
        return None
    if "embed" in endpoints and "chat" not in endpoints:
        return ("embeddings",)
    features = item.get("features") or []
    if "chat" in endpoints and isinstance(features, list):
        found: list[Capability] = []
        if "tools" in features or "strict_tools" in features:
            found.append("tools")
        if "json_schema" in features:
            found.append("structured_output")
        if "vision" in features:
            found.append("vision")
        return tuple(found)
    return None


def _dicts(value: Any) -> list[dict[str, Any]]:
    """The dict items of a JSON list; anything else (a malformed entry, a non-list) is ignored."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def bearer_auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _anthropic_auth(key: str) -> dict[str, str]:
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


def _google_auth(key: str) -> dict[str, str]:
    return {"x-goog-api-key": key}


def _elevenlabs_auth(key: str) -> dict[str, str]:
    return {"xi-api-key": key}


def _no_paging(body: dict[str, Any]) -> dict[str, str] | None:
    return None


def _anthropic_next(body: dict[str, Any]) -> dict[str, str] | None:
    last_id = _str(body.get("last_id"))
    return {"after_id": last_id} if body.get("has_more") is True and last_id else None


def _google_next(body: dict[str, Any]) -> dict[str, str] | None:
    token = _str(body.get("nextPageToken"))
    return {"pageToken": token} if token else None


def _cohere_next(body: dict[str, Any]) -> dict[str, str] | None:
    token = _str(body.get("next_page_token"))
    return {"page_token": token} if token else None


def _openai_list(body: dict[str, Any]) -> list[ListedModel]:
    """The OpenAI-shaped list (`data` of `{id, …}`): the default, and every OpenAI-compatible server."""
    out: list[ListedModel] = []
    for item in _dicts(body.get("data")):
        model_id = _str(item.get("id"))
        if model_id is None:
            continue
        # vLLM says `max_model_len`; llama.cpp's server, `meta.n_ctx_train`.
        meta = item["meta"] if isinstance(item.get("meta"), dict) else {}
        ctx = (
            _int(item.get("context_window"))
            or _int(item.get("context_length"))
            or _int(item.get("max_model_len"))
            or _int(meta.get("n_ctx_train"))
        )
        out.append(ListedModel(model_id, _str(item.get("name")), ctx))
    return out


def _anthropic_list(body: dict[str, Any]) -> list[ListedModel]:
    out: list[ListedModel] = []
    for item in _dicts(body.get("data")):
        model_id = _str(item.get("id"))
        if model_id is None:
            continue
        out.append(
            ListedModel(
                model_id,
                _str(item.get("display_name")),
                _int(item.get("max_input_tokens")),
                _anthropic_caps(item),
            )
        )
    return out


def _mistral_list(body: dict[str, Any]) -> list[ListedModel]:
    out: list[ListedModel] = []
    for item in _dicts(body.get("data")):
        model_id = _str(item.get("id"))
        if model_id is None:
            continue
        out.append(
            ListedModel(
                model_id,
                _str(item.get("name")),
                _int(item.get("max_context_length")),
                _mistral_caps(item),
            )
        )
    return out


def _google_list(body: dict[str, Any]) -> list[ListedModel]:
    out: list[ListedModel] = []
    for item in _dicts(body.get("models")):
        model_id = (_str(item.get("name")) or "").removeprefix("models/")
        if model_id:
            out.append(
                ListedModel(
                    model_id,
                    _str(item.get("displayName")),
                    _int(item.get("inputTokenLimit")),
                    _google_caps(item),
                )
            )
    return out


def _cohere_list(body: dict[str, Any]) -> list[ListedModel]:
    out: list[ListedModel] = []
    for item in _dicts(body.get("models")):
        name = _str(item.get("name"))
        if name and not item.get("is_deprecated"):
            out.append(ListedModel(name, None, _int(item.get("context_length")), _cohere_caps(item)))
    return out


def openai_chat_client(spec: "ProviderSpec", model_id: str, key: str) -> Model:
    """The OpenAI chat API at the provider's base URL: the default (MiniMax has no native provider)."""
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    return OpenAIChatModel(model_id, provider=OpenAIProvider(base_url=spec.base_url, api_key=key))


@dataclass(frozen=True)
class ProviderSpec:
    """One provider and how to talk to it: its key header, its model list's shape, paging and page size are
    its own strategies here, so the code that uses a provider asks the spec instead of comparing ids."""

    id: str
    name: str
    base_url: str
    keys_url: str
    # `genai-prices` provider ids holding this provider's prices (first match wins).
    genai_prices_ids: tuple[str, ...] = ()
    # OpenRouter's vendor prefix for this provider's own models ("<vendor>/<model_id>"), for capabilities.
    openrouter_vendor: str | None = None
    needs_key: bool = True
    # Path (relative to base_url) of the model-list endpoint; None = the provider has none.
    models_path: str | None = "/models"
    # Only these genai-prices model ids belong to this provider's own API (Google's entry also lists Vertex
    # partner models). Empty = all.
    builtin_prefixes: tuple[str, ...] = field(default=())
    # "openai_compatible": a server the user runs (or reaches), added in Settings; see compat.py.
    kind: Literal["builtin", "openai_compatible"] = "builtin"
    # A PEM file of the certificate authorities that sign the server's TLS certificate (a private CA).
    ca_bundle: str | None = None
    # The headers that carry a key (never called without one).
    auth: Callable[[str], dict[str, str]] = bearer_auth
    # One page of the model list → its models (only the fields the provider documents).
    parse_models: Callable[[dict[str, Any]], list[ListedModel]] = _openai_list
    # One page of the model list → the query parameters of the next page, or None on the last one.
    next_page: Callable[[dict[str, Any]], dict[str, str] | None] = _no_paging
    # The query parameters asking for the largest page the provider allows.
    page_size: tuple[tuple[str, str], ...] = ()
    # A free endpoint (relative to base_url) that answers for the key, when the model list can't (public).
    key_check_path: str | None = None
    # Its public model list covers every vendor's models with their capabilities and prices: the catalog's
    # cross-provider source (`catalog.fetch_openrouter_models`), not a list of models of its own.
    aggregator: bool = False
    # Speech only (its key is managed here like the others; it has no chat models): its key is checked by
    # its speech adapter (`ports.speech`).
    speech_only: bool = False
    # Its models take a system entry inside the messages and bind their thinking to the prefix, so the
    # turn's prompt is frozen and mid-turn lines go inline (`brain.prefix`). Its id is the system name
    # Pydantic AI gives its models.
    inline_system: bool = False
    # (spec, model id, key) → the Pydantic AI model of one call (a built-in provider's; a compatible server
    # is built by compat.py).
    client: "Callable[[ProviderSpec, str, str], Model]" = openai_chat_client

    @property
    def compatible(self) -> bool:
        return self.kind == "openai_compatible"

    def auth_headers(self, key: str | None) -> dict[str, str]:
        return self.auth(key) if key else {}


# --- clients: each provider's Pydantic AI model ----------------------------------------------------------


def _anthropic_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
    from pydantic_ai.providers.anthropic import AnthropicProvider

    effort = model_catalog.effort(spec.id, model_id)
    settings = AnthropicModelSettings(anthropic_effort=cast(Any, effort)) if effort else None
    return AnthropicModel(model_id, provider=AnthropicProvider(api_key=key), settings=settings)


def _openai_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
    from pydantic_ai.providers.openai import OpenAIProvider

    effort = model_catalog.effort(spec.id, model_id)
    settings = OpenAIResponsesModelSettings(openai_reasoning_effort=cast(Any, effort)) if effort else None
    return OpenAIResponsesModel(model_id, provider=OpenAIProvider(api_key=key), settings=settings)


def _google_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider

    return GoogleModel(model_id, provider=GoogleProvider(api_key=key))


def _mistral_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    from pydantic_ai.models.mistral import MistralModel
    from pydantic_ai.providers.mistral import MistralProvider

    return MistralModel(model_id, provider=MistralProvider(api_key=key))


def _groq_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    from pydantic_ai.models.groq import GroqModel
    from pydantic_ai.providers.groq import GroqProvider

    return GroqModel(model_id, provider=GroqProvider(api_key=key))


def _cohere_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    from pydantic_ai.models.cohere import CohereModel
    from pydantic_ai.providers.cohere import CohereProvider

    return CohereModel(model_id, provider=CohereProvider(api_key=key))


def _openrouter_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    from pydantic_ai.models.openrouter import OpenRouterModel
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    return OpenRouterModel(model_id, provider=OpenRouterProvider(api_key=key))


def _deepseek_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.deepseek import DeepSeekProvider

    return OpenAIChatModel(model_id, provider=DeepSeekProvider(api_key=key))


def _moonshot_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.moonshotai import MoonshotAIProvider

    return OpenAIChatModel(model_id, provider=MoonshotAIProvider(api_key=key))


def _zhipu_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.zai import ZaiProvider

    return OpenAIChatModel(model_id, provider=ZaiProvider(api_key=key, base_url=spec.base_url))


def _qwen_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.alibaba import AlibabaProvider

    return OpenAIChatModel(model_id, provider=AlibabaProvider(api_key=key, base_url=spec.base_url))


def _xai_client(spec: ProviderSpec, model_id: str, key: str) -> Model:
    return xai_model(model_id, key, spec.base_url)


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "anthropic",
        "Anthropic",
        "https://api.anthropic.com/v1",
        "https://platform.claude.com/settings/keys",
        ("anthropic",),
        "anthropic",
        auth=_anthropic_auth,
        parse_models=_anthropic_list,
        next_page=_anthropic_next,
        page_size=(("limit", "1000"),),
        inline_system=True,
        client=_anthropic_client,
    ),
    ProviderSpec(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "https://platform.openai.com/api-keys",
        ("openai",),
        "openai",
        client=_openai_client,
    ),
    ProviderSpec(
        "google",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta",
        "https://aistudio.google.com/apikey",
        ("google",),
        "google",
        builtin_prefixes=("gemini", "gemma"),
        auth=_google_auth,
        parse_models=_google_list,
        next_page=_google_next,
        page_size=(("pageSize", "1000"),),
        client=_google_client,
    ),
    ProviderSpec(
        "mistral",
        "Mistral",
        "https://api.mistral.ai/v1",
        "https://console.mistral.ai/api-keys",
        ("mistral",),
        "mistralai",
        parse_models=_mistral_list,
        client=_mistral_client,
    ),
    ProviderSpec(
        "groq",
        "Groq",
        "https://api.groq.com/openai/v1",
        "https://console.groq.com/keys",
        ("groq",),
        client=_groq_client,
    ),
    ProviderSpec(
        "cohere",
        "Cohere",
        "https://api.cohere.com/v1",
        "https://dashboard.cohere.com/api-keys",
        ("cohere",),
        "cohere",
        parse_models=_cohere_list,
        next_page=_cohere_next,
        page_size=(("page_size", "1000"),),
        client=_cohere_client,
    ),
    ProviderSpec(
        "xai", "xAI", "https://api.x.ai/v1", "https://console.x.ai", ("x-ai",), "x-ai", client=_xai_client
    ),
    ProviderSpec(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/settings/keys",
        ("openrouter",),
        key_check_path="/key",
        aggregator=True,
        client=_openrouter_client,
    ),
    ProviderSpec(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com",
        "https://platform.deepseek.com/api_keys",
        ("deepseek",),
        "deepseek",
        client=_deepseek_client,
    ),
    ProviderSpec(
        "moonshot",
        "Moonshot (Kimi)",
        "https://api.moonshot.ai/v1",
        "https://platform.moonshot.ai/console/api-keys",
        ("moonshotai",),
        "moonshotai",
        client=_moonshot_client,
    ),
    ProviderSpec(
        "zhipu",
        "Z.ai (Zhipu GLM)",
        "https://api.z.ai/api/paas/v4",
        "https://z.ai/manage-apikey/apikey-list",
        ("zai", "zhipuai"),
        "z-ai",
        models_path=None,
        client=_zhipu_client,
    ),
    ProviderSpec(
        "minimax",
        "MiniMax",
        "https://api.minimax.io/v1",
        "https://platform.minimax.io/user-center/basic-information/interface-key",
        ("minimax",),
        "minimax",
    ),
    ProviderSpec(
        "qwen",
        "Alibaba Qwen",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "https://www.alibabacloud.com/help/en/model-studio/get-api-key",
        (),
        "qwen",
        # The OpenAI-compatible endpoint has no documented model list (Alibaba documents a different native
        # API), so Qwen models come from the built-in list and OpenRouter only.
        models_path=None,
        client=_qwen_client,
    ),
    # Speech only: its key is managed here like the others; it has no chat models.
    ProviderSpec(
        "elevenlabs",
        "ElevenLabs",
        "https://api.elevenlabs.io/v1",
        "https://elevenlabs.io/app/settings/api-keys",
        models_path=None,
        auth=_elevenlabs_auth,
        speech_only=True,
    ),
)
PROVIDERS_BY_ID: dict[str, ProviderSpec] = {p.id: p for p in PROVIDERS}


def get_provider(provider_id: str) -> ProviderSpec:
    spec = PROVIDERS_BY_ID.get(provider_id)
    if spec is None:
        raise PisaError(
            "not_found",
            f"There is no AI provider {provider_id!r}.",
            "Use one of: " + ", ".join(PROVIDERS_BY_ID),
        )
    return spec


ModelFactory = Callable[[ProviderSpec, str, str | None, "ModelCaps | None"], Model]
"""(provider, model_id, api_key, caps) → a Pydantic AI model. `caps` are the model's capabilities on an
OpenAI-compatible server (None for the built-in providers). Tests inject fakes."""


def build_model(spec: ProviderSpec, model_id: str, api_key: str | None, caps: "ModelCaps | None") -> Model:
    """Build the Pydantic AI model for one call. The key lives only in this client object."""
    if spec.compatible:
        from pisa.brain.models.compat import build_compat_model
        from pisa.domain.model_caps import ModelCaps

        return build_compat_model(spec, model_id, api_key, caps or ModelCaps())
    return spec.client(spec, model_id, api_key or "")


def xai_model(model_id: str, key: str, base_url: str, openai_client: Any = None) -> Model:
    """Grok through xAI's OpenAI-compatible endpoint: Pydantic AI's native xAI provider needs the
    gRPC xai-sdk, which Pisa doesn't carry. Tool calls and streaming are the OpenAI chat API's; the usage
    chunk (`stream_options.include_usage`) carries `prompt_tokens_details.cached_tokens`, read as the cache's
    tokens (xAI caches the prompt prefix by itself). Grok's own profile adds what its models support."""
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
    from pydantic_ai.profiles import merge_profile
    from pydantic_ai.profiles.grok import grok_model_profile
    from pydantic_ai.profiles.openai import OpenAIJsonSchemaTransformer, OpenAIModelProfile
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = (
        OpenAIProvider(openai_client=openai_client)
        if openai_client is not None  # tests: a client on a fake transport
        else OpenAIProvider(base_url=base_url, api_key=key)
    )
    profile = merge_profile(
        OpenAIModelProfile(json_schema_transformer=OpenAIJsonSchemaTransformer), grok_model_profile(model_id)
    )
    effort = model_catalog.effort("xai", model_id)
    settings = OpenAIChatModelSettings(openai_reasoning_effort=cast(Any, effort)) if effort else None
    return OpenAIChatModel(model_id, provider=provider, profile=profile, settings=settings)


def parse_model_list(spec: ProviderSpec, body: Any) -> list[ListedModel]:
    """Turn one page of a provider's model list into ListedModels (only fields the provider documents)."""
    return spec.parse_models(body) if isinstance(body, dict) else []


MAX_LIST_PAGES = 20


def next_page_params(spec: ProviderSpec, body: Any) -> dict[str, str] | None:
    """The query parameters for the next page of a paginated model list, or None on the last page."""
    return spec.next_page(body) if isinstance(body, dict) else None


class ProviderCallError(Exception):
    """A model-list call failed. `message` names the provider and HTTP status, never the key."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


async def list_models(spec: ProviderSpec, key: str | None, http: httpx.AsyncClient) -> list[ListedModel]:
    """Call the provider's model-list endpoint (a free call). Raises ProviderCallError naming the cause."""
    if spec.models_path is None:
        raise ProviderCallError(
            f"{spec.name} has no model-list endpoint, so Pisa can't verify the key for free."
        )
    root = spec.base_url.rstrip("/")
    params = dict(spec.page_size)
    models: list[ListedModel] = []
    page: dict[str, str] = {}
    for _ in range(MAX_LIST_PAGES):
        body = await _get_model_page(spec, http, root + spec.models_path, key, {**params, **page})
        models += parse_model_list(spec, body)
        next_page = next_page_params(spec, body)
        if next_page is None:
            break
        page = next_page
    return models


async def _get_model_page(
    spec: ProviderSpec, http: httpx.AsyncClient, url: str, key: str | None, params: dict[str, str]
) -> Any:
    try:
        response = await http.get(url, headers=spec.auth_headers(key), params=params, timeout=20)
    except httpx.HTTPError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            raise ProviderCallError(
                f"{spec.name}'s TLS certificate is not trusted: give Pisa the certificate authority that "
                "signed it (the server's CA bundle)."
            ) from None
        raise ProviderCallError(f"{spec.name} is unreachable ({exc.__class__.__name__}).") from None
    if response.status_code in (401, 403):
        raise ProviderCallError(
            f"{spec.name} rejected the key (HTTP {response.status_code}).", response.status_code
        )
    if response.status_code >= 400:
        raise ProviderCallError(
            f"The {spec.name} model list failed (HTTP {response.status_code}).", response.status_code
        )
    try:
        return response.json()
    except ValueError:
        raise ProviderCallError(f"{spec.name} returned a model list that is not JSON.") from None


async def check_key_endpoint(spec: ProviderSpec, key: str, http: httpx.AsyncClient) -> None:
    """A provider whose model list is public can't validate a key with it: its key endpoint does, for free
    (OpenRouter: GET /key)."""
    assert spec.key_check_path is not None
    try:
        response = await http.get(
            spec.base_url.rstrip("/") + spec.key_check_path, headers=spec.auth_headers(key), timeout=20
        )
    except httpx.HTTPError as exc:
        raise ProviderCallError(f"{spec.name} is unreachable ({exc.__class__.__name__}).") from None
    if response.status_code in (401, 403):
        raise ProviderCallError(
            f"{spec.name} rejected the key (HTTP {response.status_code}).", response.status_code
        )
    if response.status_code >= 400:
        raise ProviderCallError(
            f"The {spec.name} key check failed (HTTP {response.status_code}).", response.status_code
        )
