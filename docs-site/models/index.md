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

`PATCH /v1/ai/providers/{id}` changes the name, the address or the CA bundle (`"ca_bundle": null` removes
it). A new address forgets the probe's results, since it may be another server. `DELETE` removes the
server, its key, its probes and its models.

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

Each request asks for at most 256 tokens and may take up to 120 seconds, so a probe on a CPU can take a few
minutes. `GET /v1/ai/providers/{id}/capabilities?model=...` shows the result:

```json
{
  "model": "Qwen/Qwen3-8B",
  "probed_at": "2026-11-18T09:12:44+00:00",
  "tools": true, "json_schema": true, "json_object": true, "tool_choice": true,
  "vision": false, "audio": false, "embeddings": false,
  "overrides": {},
  "features_needing_tools": ["conversation"]
}
```

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
  and `audio` results are recorded, but memory search still makes its vectors with OpenAI's embeddings only
  (with any other model it searches by words), and speech still goes through the speech providers.
- **How structured output is asked for**, per use: the server's JSON schema when it has `json_schema`; else a
  tool call when it has `tools`; else the schema in the prompt, with `json_object` mode when the server has
  it, checked and retried once.
- **Whether a tool call can be forced**: only with `tool_choice`. Otherwise Pisa leaves the choice to the model.

A use whose model lacks what it needs fails before any request, with the error code
`model_capability_missing` and the capability and model named in the error (`capability`, `model`). A model
never probed is probed first, once, and checked again. If the use has a fallback model, Pisa tries that one.

## Cost

Calls to your server cost nothing in Pisa's ledger. Their tokens are still counted.
