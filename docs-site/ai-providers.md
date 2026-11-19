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
probe again or override a result by hand. See
[Your own model server](models/index.md), with one page per server. `PISA_OLLAMA_BASE_URL` adds an Ollama
server from the server's settings.

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
```

A client does the same with `GET /v1/ai/slots` and `PUT /v1/ai/slots/{slot}` (in the PISA app, Settings → AI
models).

## Spending

Every paid call (AI, voice, maps, web) is priced and recorded. Maps and web search have their own monthly cap
too: [Places and web search](places-and-web.md#spending). Two limits apply:

- the **hard limit**, set on the server: `PISA_HARD_DAILY_EUR` (default 3) and `PISA_HARD_MONTHLY_EUR`
  (default 30), over every paid service together;
- your **caps**, set from a client (`PUT /v1/ai/caps`), which can only be lower.

At a limit, paid features stop and free ones keep working. `GET /v1/ai/spend` shows the spending.

## What goes to which provider

`GET /v1/ai/disclosure` lists which provider and model gets which of your data, per slot. When it changes, Pisa
needs your consent again, which a client asks for and records (`POST /v1/ai/consent`). See [Privacy](privacy.md).
