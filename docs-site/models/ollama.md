# Ollama

The easiest server to set up. Ollama serves its OpenAI-compatible API at `http://localhost:11434/v1`.

## Launch

```sh
ollama pull qwen3:8b
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

`OLLAMA_HOST` is only needed when Pisa runs on another machine. Ollama itself has no API key; keep it on a
private network, or put it behind a proxy that checks one.

What its OpenAI API offers:

- tools: yes, for models that support them (e.g. Qwen 3, Llama 3.1+);
- **`tool_choice`: no.** Ollama ignores it, so a tool call can't be forced;
- `response_format` (JSON mode and JSON schemas): yes;
- images: yes, base64 only, for vision models;
- `/v1/embeddings`: yes, for embedding models (e.g. `nomic-embed-text`);
- `/v1/audio/transcriptions`: yes, for speech-to-text models, in recent versions.

## In Pisa

Either set it on Pisa's server:

```sh
PISA_OLLAMA_BASE_URL=http://127.0.0.1:11434
```

which adds a server with the id `ollama` (its models are `ollama:qwen3:8b`), changed only through the
setting; or add it in Settings → AI providers like any other server, with the base URL
`http://<host>:11434/v1` and no key.

## What the probe reports

| Capability | A chat model with tools (e.g. `qwen3:8b`) |
|---|---|
| `tools` | true |
| `tool_choice` | **false**: Ollama ignores it, so the forced call never comes |
| `json_schema` | true |
| `json_object` | true |
| `vision` | true only for a vision model (e.g. `qwen2.5vl`) |
| `audio` | true only for a speech-to-text model |
| `embeddings` | true for an embedding model; the probe tells for a chat model |

Without `tool_choice` Pisa never forces a tool call: structured output uses the JSON schema instead, and the
conversation leaves the choice of tools to the model.

## Sources

- https://docs.ollama.com/api/openai-compatibility
