# Your own model server

Pisa can use a model server you run yourself, through the OpenAI-compatible API that most servers offer. You
add the server once, Pisa lists its models, probes what each model can do, and you choose which uses go to
which model in Settings → AI models.

One page per server:

- [vLLM](vllm.md): recommended for a GPU server.
- [SGLang](sglang.md)
- [llama.cpp server](llama-cpp.md): best for a CPU or Apple Silicon.
- [Ollama](ollama.md): the easiest to set up.
- [LM Studio](lm-studio.md): a desktop app.
- [LiteLLM proxy](litellm.md): one address in front of several servers.
- [Text Generation Inference](tgi.md): may work; not tested.

Upgrading from a version that probed only on request? See [Upgrading](upgrading.md): models served before
show no capabilities until probed, and Pisa now probes them by itself.

## What Pisa needs from the server

- **The base URL**: the address that ends in `/v1`, e.g. `http://192.168.1.20:8000/v1`. Pisa calls
  `{base}/models`, `{base}/chat/completions`, and, when the model has them, `{base}/embeddings` and
  `{base}/audio/transcriptions`.
- **An API key**, only if the server asks for one. Pisa stores it encrypted, like every provider's key, and
  sends it as `Authorization: Bearer <key>`.
- **A CA bundle**, only if the server uses HTTPS with a certificate your own authority signed. Copy the PEM
  file of that authority to Pisa's server and give its absolute path. A certificate from a public authority
  needs nothing.

## Adding a server

From a client, through the API below (in the PISA app: Settings → AI providers → Add a server).

Through the API (every request carries a session token, e.g. from `pisa client token`; once Pisa has a Face
ID key, adding or changing a server is signed with the client's device key, op `model_server`,
[Pairing](../pairing.md#signed-operations)):

```sh
# add the server
curl -X POST https://pisa.example/v1/ai/providers -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type": "openai_compatible", "id": "home-gpu", "name": "Home GPU",
       "base_url": "http://192.168.1.20:8000/v1"}'

# its key, if it has one
curl -X PUT https://pisa.example/v1/ai/providers/home-gpu/key -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"key": "<the server key>"}'

# check that Pisa reaches it and sees its models
curl -X POST https://pisa.example/v1/ai/providers/home-gpu/test -H "Authorization: Bearer $TOKEN"

# probe one model
curl -X POST "https://pisa.example/v1/ai/providers/home-gpu/probe?model=Qwen/Qwen3-8B" \
  -H "Authorization: Bearer $TOKEN"
```

The server's id becomes the first part of its models' references: `home-gpu:Qwen/Qwen3-8B`. It needs 2 to 32
lowercase letters, digits or `-`, and can't be a built-in provider's name.

Ollama can also be set on Pisa's server: `PISA_OLLAMA_BASE_URL=http://127.0.0.1:11434` adds a server with
the id `ollama`. That one is changed only through the setting.

`PATCH /v1/ai/providers/{id}` changes the name, the address, the CA bundle (`"ca_bundle": null` removes
it), the read timeout or the thinking switches (below). A new address forgets the probe's results, since it may be another server. `DELETE` removes the
server, its key, its probes and its models.

## Timeouts and thinking

- **The read timeout** (`read_timeout_s`, default 60 s, 10 to 900): how long a request may wait for the
  server's next byte. It replaces the 20 s Pisa gives a cloud provider, for every use of the server's models
  (the conversation, email sorting, the probe). A CPU reads a long prompt for minutes before its first token:
  llama.cpp sends a keepalive every 30 s meanwhile, Ollama sends nothing. Raise it for long prompts on a CPU:
  `pisa models server home-gpu --read-timeout 300`, or `"read_timeout_s": 300` in the POST or PATCH body. A
  request that times out fails at once, worded "did not answer within N s" (not "could not be reached"); the
  OpenAI SDK's own retries are off, so a slow server never gets the same long prompt three times.
- **Thinking** (`thinking`, per slot): a thinking model (Qwen 3, DeepSeek R1...) spends seconds and tokens
  before it answers: on a CPU, the first spoken word came after 86 s. When the probe saw the model think, Pisa
  asks the server not to think for the fast slots, `conversation.reflex`, `intake.triage` and
  `understand.confirm`; the other slots, the conversation included, think unless you turn it off:
  `pisa models server home-gpu --thinking chat.conversation=off`, or `"thinking": {"chat.conversation":
  false}` in a PATCH body (only the slots named change). The switch is `chat_template_kwargs:
  {"enable_thinking": false}` (llama.cpp, vLLM, SGLang: the model's chat template reads it) or
  `reasoning_effort: "none"` (Ollama); a server or template that ignores it keeps thinking.

!!! note "What has been tested"
    Pisa's own-server support was measured against **llama.cpp** and **Ollama** on a CPU, with small models
    (the compatibility matrix in the repository's `scripts/local-models/`). **vLLM, SGLang and LM Studio have
    not been tested against a live server yet**, the thinking switch included: they speak the same
    OpenAI-compatible API, and the probe checks what each one actually does, but treat them as untested until
    this page says otherwise.

Thinking is never spoken: Pisa reads it from `reasoning_content`, `reasoning` or `<think>` tags and keeps it
apart from the answer.

## The probe

Pisa assumes nothing about a model. Until it is probed, every capability is false. Pisa probes a model once
when you first choose it for a use, and once when a use first misses a capability on a model never probed
(see [Upgrading](upgrading.md)). You can probe it again by hand at any time: `pisa models probe
<server:model>` on Pisa's server, or `POST /v1/ai/providers/{id}/probe?model=...`.

The probe sends one tiny real request per capability and checks the answer strictly:

| Capability | The request | It passes when |
|---|---|---|
| `tools` | One tool (`pisa_probe_add`) and "Use the pisa_probe_add tool to add 2 and 3." | A real `tool_calls` entry names the tool, with `a` and `b` set to 2 and 3. A call written as text in the answer does not count. |
| `tool_choice` | The same tool, `tool_choice: "required"`, and "Say hello." (only when `tools` passed) | The tool is called although nothing in the words asks for it. |
| `json_schema` | `response_format` with a JSON schema whose only key is `pisa_probe_sum`, and "What is 2 plus 3?" | The answer is a JSON object with an integer `pisa_probe_sum`. The key is named nowhere else, so only a server that enforces the schema produces it. |
| `json_object` | `response_format: {"type": "json_object"}` and "Describe the colour of the sky in JSON." | The answer is a JSON object, with no fence or text around it. |
| `vision` | A small red PNG and "What colour is this image?" | The answer says red. |
| `audio` | Half a second of silent WAV to `/audio/transcriptions` | The answer has a `text` field. |
| `embeddings` | `"pisa probe"` to `/embeddings` | The answer has a vector of numbers. |

Each request asks for at most 256 tokens and may take up to 120 seconds (or the server's read timeout, when
longer), so a probe on a CPU can take a few minutes. A thinking model can spend 256 tokens thinking: when an
answer stops at the limit while still thinking, that request is sent once more with room for 2048 tokens.
The probe also asks the server what it is and the context it serves the model with: llama.cpp's `/props`
(`n_ctx`), Ollama's `/api/ps` (the loaded model's `context_length`) or `/api/show` (a Modelfile's
`num_ctx`). `GET /v1/ai/providers/{id}/capabilities?model=...` shows the result:

```json
{
  "model": "Qwen/Qwen3-8B",
  "probed_at": "2026-11-18T09:12:44+00:00",
  "tools": true, "json_schema": true, "json_object": true, "tool_choice": true,
  "vision": false, "audio": false, "embeddings": false,
  "overrides": {},
  "features_needing_tools": ["conversation"],
  "thinking": true, "context_tokens": 32768, "server": "llama.cpp",
  "truncated": null
}
```

`thinking`: the probe saw the model think. `context_tokens` and `server`: when the server says. `truncated`:
the last time the server read far fewer prompt tokens than Pisa sent (see below), until a probe finds a
context that holds that prompt.

## A prompt longer than the context

llama.cpp refuses it (HTTP 400, `exceed_context_size_error`); Pisa says so, with the prompt's tokens and
the context, and how to raise it (`--ctx-size`). **Ollama cuts it without an error**: it answers 200 from
the part it kept, and only `usage.prompt_tokens` shows the cut (2050 tokens read of 140 000 sent, in our
test). Pisa compares those tokens with its own estimate of what it sent: when the server read less than
half, the prompt was cut. Pisa then logs it (token counts only, never the text), shows it in `/readyz`
(`model_truncations`), in the model's capabilities (`truncated`) and in `pisa models probe`, and tells the
user once, after the answer: "One thing: my model server cut off part of what I sent it, so this answer may
have missed something. Whoever runs the server can raise its context size." Pisa's conversation prompt, with
its tools, is well over Ollama's default of 4096 tokens: set `OLLAMA_CONTEXT_LENGTH` (see
[Ollama](ollama.md)).

## Rounds without tools

Pisa's last round of a turn may use no tool (`tool_choice: "none"`). Neither llama.cpp nor Ollama honours
that: Ollama calls the tool anyway, llama.cpp writes the call as text (`<tool_call>…</tool_call>`). So on
an own server such a round is sent with no tools at all. As a backstop, tool-call markup in an answer is
never said, and a call on such a round is refused.

The values are the effective ones: the probe's result with your overrides applied.

## Overrides

A probe can be wrong both ways: a small model may fail a test the server passes, or pass one by chance. Set a
capability by hand with:

```sh
curl -X PUT "https://pisa.example/v1/ai/providers/home-gpu/capabilities/overrides?model=Qwen/Qwen3-8B" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"vision": true}'
```

The body replaces all of the model's overrides; a capability left out follows the probe again, and `{}`
clears them.

## What the capabilities decide

- **Which uses can take the model.** The conversation needs `tools`. Sorting email, extracting tasks and the
  other structured uses need structured output: `json_schema`, `json_object` or `tools`. The `embeddings`
  and `audio` results are recorded. Memory search (`memory.embed`) can use a model on your server whose
  probe found `embeddings` (its `/v1/embeddings`); the vectors are kept per model, so a new model's search
  never reads the old one's (they are made again). Speech still goes through the speech providers.
- **How structured output is asked for**, per use: the server's JSON schema when it has `json_schema`; else a
  tool call when it has `tools`; else the schema in the prompt, with `json_object` mode when the server has
  it, checked and retried once.
- **Whether a tool call can be forced**: only with `tool_choice`. Otherwise Pisa leaves the choice to the model.

A use whose model lacks what it needs fails before any request, with the error code
`model_capability_missing` and the capability and model named in the error (`capability`, `model`). A model
never probed is probed first, once, and checked again. If the use has a fallback model, Pisa tries that one.

## `pisa setup --ai own`

Setup adds the server (`--server-url`, `--server-id`, `--read-timeout`), probes the conversation's model
(`--model <server:model>`), then gives every slot that model can serve to it, and memory search to the
model itself when it embeds, else to another of the server's models that answers `/v1/embeddings`. It ends
saying which slots keep a model of their own (a cloud one) and which stay unset.

## Cost

Calls to your server cost nothing in Pisa's ledger. Their tokens are still counted.
