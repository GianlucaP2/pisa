# AI providers and models

Pisa has no built-in model and no default: you bring a provider's key (or your own model server) and choose
which model each feature uses.

## Providers

Built in, each with your own API key: Anthropic, OpenAI, Google Gemini, Mistral, Groq, Cohere, xAI, OpenRouter,
DeepSeek, Moonshot (Kimi), Z.ai (Zhipu GLM), MiniMax, Alibaba Qwen and ElevenLabs (voice).

```bash
pisa secrets set openai      # the key is read from a hidden prompt or stdin, never from the command line
pisa catalog refresh         # models, prices and the USD→EUR rate, now (the worker also refreshes them)
```

Or from a client: `PUT /v1/ai/providers/{id}/key`, and `POST /v1/ai/providers/{id}/test` to test the key (in
the PISA app, Settings → AI providers). Keys are stored encrypted with `PISA_SECRETS_KEY`.

**Your own model server**: any server with an OpenAI-compatible API (vLLM, SGLang, llama.cpp, Ollama,
LM Studio, LiteLLM). Pisa lists its models and probes what each can do (tool calling, JSON output, vision,
embeddings, audio transcription), one tiny real request per capability, when you first choose a model; you can
probe again or override a result by hand. Its models can serve every slot their capabilities allow, memory
search included (an embedding model on the server). Each server has its own read timeout (60 s by default:
raise it for long prompts on a CPU) and per-slot thinking switches (off for the fast slots). See
[Your own model server](models/index.md), with one page per server. `PISA_OLLAMA_BASE_URL` adds an Ollama
server from the server's settings; with Ollama, set `OLLAMA_CONTEXT_LENGTH`, or long prompts are cut
silently ([Ollama](models/ollama.md)).

## Getting a key

Step by step for the three providers most people start with. Every provider's key works the same way in
Pisa: you create it on the provider's own site, paste it once (in the PISA app, Settings → AI providers, or with
`pisa secrets set <provider id>` on the server), and Pisa keeps it encrypted on your server. `pisa setup`
prints the key page of the provider you choose.

Prices, free tiers and limits are the providers' own and change often. What follows was checked **as of
November 2026**, with a link to the provider's page: check it there before you rely on it.

### Anthropic

Provider id: `anthropic`.

1. Open the Claude Console at <https://platform.claude.com/> and sign in, or create an account.
2. Go to the API keys page: <https://platform.claude.com/settings/keys>. (The old address,
   `console.anthropic.com/settings/keys`, sends you there.)
3. Click **Create key**, give it a name such as `pisa`, and choose a **workspace** for it (the default one is
   fine): a key that isn't scoped to a workspace must name one on every request (the `anthropic-workspace-id`
   header), and Pisa doesn't send it.
4. Copy the key (it starts with `sk-ant-`). The Console shows it **only once**; if you lose it, make a new one.
5. Paste it into Pisa, then choose a model for each feature (below).

**Billing.** As of November 2026, Anthropic's API is paid per use, billed separately from any Claude
subscription, from your organization's Billing page (<https://platform.claude.com/settings/billing>). Its
documentation describes no free API tier; new organizations start with lower limits that rise with use.
Source: [Get your Claude API key](https://platform.claude.com/docs/en/get-api-key) and
[Rate limits](https://platform.claude.com/docs/en/api/rate-limits).

**Spending limit.** Set your own monthly limit, below your tier's cap, at
<https://platform.claude.com/settings/spend-limits> (**Set limit** next to **Monthly spend limit**). When it is
reached, requests fail until the next month or until you raise it.

### OpenAI

Provider id: `openai`.

1. Sign in at <https://platform.openai.com/>. The API platform is separate from a ChatGPT subscription, and is
   billed on its own.
2. Go to the API keys page: <https://platform.openai.com/api-keys>.
3. Create a new secret key, name it `pisa`, and pick the project it belongs to. Copy the key right away: you
   can't see it again later.
4. Paste it into Pisa, then choose a model for each feature (below).

**Billing.** As of November 2026, new API accounts use prepaid billing: you buy credits before you use the
API (the minimum purchase is USD 5), and use takes them down; purchased credits expire after one year. Buy them
on the organization's billing page (<https://platform.openai.com/settings/organization/billing/overview>).
Source: [Setting up and managing prepaid API billing](https://help.openai.com/en/articles/8264644-setting-up-and-managing-prepaid-api-billing)
and the [quickstart](https://developers.openai.com/api/docs/quickstart).

**Spending limit.** In the organization's settings, open the project's **Limits** and set a **Monthly spend
limit**. On its own a limit only alerts you; turn on **Enforce a hard limit** to stop requests at it. Source:
[Spend limits](https://developers.openai.com/api/docs/guides/spend-limits). Prepaid credits are a limit too: you
can never spend more than you bought.

### Gemini

Provider id: `google` (Google's Gemini API, from Google AI Studio). A Google key also enables natural voice
talks with Gemini Live.

1. Sign in with your Google account at <https://aistudio.google.com/>.
2. Go to the API key page: <https://aistudio.google.com/apikey> and click **Create API key**. AI Studio makes
   a Google Cloud project for it, or lets you choose one of yours.
3. Optional but wise: on the API keys page, **Add restrictions** → **Restrict to Gemini API only**.
4. Copy the key and paste it into Pisa, then choose a model for each feature (below).

**Billing and the free tier.** As of November 2026, the Gemini API has a free tier with limited access to some
models and rate limits, and a paid tier. On the free tier, **Google uses your content to improve its
products**; on the paid tier it does not. To move to the paid tier you set up billing for the project in AI
Studio (a prepayment of at least USD 5). Source: [Pricing](https://aistudio.google.com/docs/pricing),
[API keys](https://aistudio.google.com/docs/api-key) and [Billing](https://aistudio.google.com/docs/billing).
Keep in mind that whatever Pisa sends to Gemini (below) is the content Google may use on the free tier.

**Google AI tier: you declare it.** The Gemini API's terms
([ai.google.dev/gemini-api/terms](https://ai.google.dev/gemini-api/terms)) apply the paid services' data-use
terms to all use in the EEA, Switzerland and the UK, unpaid quota included, and allow only paid services
when a client is made available to users there. Pisa can't read your key's tier or where you are, so you
declare it in `google_ai_tier` (`pisa setup` asks; or `PATCH /v1/settings`):

- `paid`: billing is on for the key's project. Pisa uses Google's models and Gemini Live.
- `free_eea_own_use`: your own declaration, "I am in the EEA, Switzerland or the UK, and I am the only person
  who uses this Pisa with this key." Gemini Live only; a model slot on Google still needs `paid`.
- `unknown` (the default) or `free`: nothing goes to Google (`409 google_ai_tier_not_paid`).

The value is the operator's own declaration. Anyone serving other users must use a paid key.

**Spending limit.** AI Studio's **Spend** page (<https://aistudio.google.com/spend>) has a monthly spend cap per
project (Google marks it experimental, and it can lag by about ten minutes); with prepayment and auto-reload,
set a monthly auto-reload limit on the Billing page.

### What Pisa sends to the provider

Before anything goes to an AI provider, Pisa shows you exactly what will go where, and waits for your yes:

- **The AI disclosure** (`GET /v1/ai/disclosure`) lists, for each feature, the provider and the model, and the
  kinds of your data that feature may send: the words of your request and the conversation, and, where the
  feature needs it, your mail, calendar, places and location, people, documents, finance and health. The PISA
  app shows it on its consent screen; `pisa consent` shows it on the server.
- **The server's voice is a processor too.** When you choose an OpenAI or ElevenLabs voice (Settings → Voice,
  with that provider's key set), every sentence Pisa says is sent to that provider to be spoken: your tasks,
  your calendar, summaries of your mail, answers about your health. The disclosure then lists it as
  `conversation.voice`, with the same kinds of data as the conversation, and choosing a voice from a provider
  your consent doesn't cover asks you again. The phone's own voice (Device) sends nothing anywhere and is not
  listed.
- **Your consent** is recorded per device. Until you give it, and again whenever a provider, a model or a kind
  of data changes, no AI call is made (`409 ai_consent_required`).
- **Health goes only with the separate health consent.** A request that touches your health is not sent to any
  AI provider unless you also allowed health (in the app's consent screen, or `pisa consent --health`).
- Nothing goes to the PISA project. The key, and your data, stay on your server, and go only to the provider you
  chose. See [Privacy](privacy.md).

The same three tips for every provider: use a key made only for Pisa (so you can revoke it alone), set a
spending limit at the provider, and set Pisa's own caps too ([Spending](#spending)).

## Slots: which model does what

Each feature is a *slot*. A slot with no model fails with `model_not_configured`, naming the slot.

| Slot | Feature | The model needs |
|---|---|---|
| `chat.conversation` | the conversation | tool calling |
| `conversation.reflex` | Pisa's reflex while you talk | structured output |
| `intake.triage` | sorting incoming email and events | structured output |
| `intake.extract` | extracting tasks, deadlines and places | structured output |
| `briefing.compose` | the morning briefing | |
| `places.research` | researching a place on the web | structured output |
| `memory.embed` | embeddings for search | embeddings |
| `understand.confirm` | checking a name Pisa isn't sure it heard | |
| `attend.decide` | deciding when Pisa speaks up | structured output |
| `learn.answer` | answers from your own sources | |

```bash
pisa models list                                  # every slot and its model
pisa models available --slot chat.conversation    # the models of every provider with a key, that fit
pisa models set chat.conversation openai:<model>
pisa models probe <server id>:<model>             # probe a model on your own server (a minute or more)
pisa models server <server id> --read-timeout 300 --thinking chat.conversation=off   # your own server
```

A client does the same with `GET /v1/ai/slots` and `PUT /v1/ai/slots/{slot}` (in the PISA app, Settings → AI
models).

A provider's key can't be deleted while a slot's model or fallback is on that provider: the answer names the
slots (`409 provider_in_use`). Move them first, or delete with `?replace_with=<another provider>`, which moves
each slot to that provider's fitting model in the same step ([Connections](connections.md#deleting-a-key)).
Every provider, voice and maps service in one list, with what each costs and sends where:
[Connections](connections.md).

## Spending

Every paid call (AI, voice, maps, web) is priced and recorded. Maps and web search have their own monthly cap
too: [Places and web search](places-and-web.md#spending). Two limits apply:

- the **hard limit**, set on the server: `PISA_HARD_DAILY_EUR` (default 3) and `PISA_HARD_MONTHLY_EUR`
  (default 30), over every paid service together;
- your **caps**, set from a client (`PUT /v1/ai/caps`), which can only be lower.

At a limit, paid features stop and free ones keep working. `GET /v1/ai/spend` shows the spending. The AI
providers and the voices need no separate OK of their cost: your AI consent and these caps are their guard
(Google Maps and Tavily do: [the paid OK](connections.md#the-paid-ok)).

## What goes to which provider

`GET /v1/ai/disclosure` lists which provider and model gets which of your data, per slot, and the server's
voice (`conversation.voice`) while an OpenAI or ElevenLabs voice speaks. When it changes, Pisa
needs your consent again, which a client asks for and records (`POST /v1/ai/consent`). See [Privacy](privacy.md).
