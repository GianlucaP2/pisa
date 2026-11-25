# llama.cpp server

`llama-server` runs GGUF models on a CPU, Apple Silicon or a GPU. It listens on 127.0.0.1 by default; give
`--host` and `--port` explicitly (the default port has changed between versions).

## Launch

Tool calling needs the Jinja template engine, `--jinja`. Recent versions turn it on by default; passing it
does no harm on any version:

```sh
llama-server --jinja -fa -hf bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M \
  --host 0.0.0.0 --port 8080 \
  --api-key "$LLAMA_API_KEY"
```

- Native tool-call formats: Llama 3.1 / 3.2 / 3.3, Qwen 2.5 (Coder included), Hermes 2 / 3, Mistral Nemo,
  Functionary, Firefunction v2, Command R7B, DeepSeek R1, gpt-oss. Other models use a generic format, which
  works less well.
- A model whose template has no tool use may need `--chat-template-file` with a tool-aware template from
  `models/templates/`.
- Parallel tool calls are off by default; Pisa doesn't need them.
- Strong KV-cache quantization (e.g. `-ctk q4_0`) can hurt tool calling.
- Images need the model's projector (`--mmproj`, downloaded automatically with `-hf`).
- `/v1/embeddings` needs a model with pooling; `--embeddings` restricts the server to a dedicated embedding
  model.
- HTTPS: `--ssl-cert-file` and `--ssl-key-file` (a build with OpenSSL).
- The context (`--ctx-size`): a longer prompt is refused (HTTP 400, `exceed_context_size_error`), and Pisa
  says so with the prompt's tokens and the context.
- Without `--jinja`, a request with tools fails with HTTP 500 ("tools param requires --jinja flag"); Pisa says
  to restart with `--jinja`.
- **Any model name is answered** (HTTP 200): the server has one model and ignores the name. A typo in a
  model reference is not caught by the server: check it against `/v1/models`.
- `tool_choice: "required"` and a named tool are ignored, and `"none"` makes the model write the call as
  text (`<tool_call>…`): Pisa sends a no-tool round without tools and never says such text.
- Thinking models (e.g. Qwen 3): the thinking comes in `reasoning_content` (or in `<think>` tags with
  `--reasoning-format none`), never said. Pisa turns it off for the fast slots with `chat_template_kwargs:
  {"enable_thinking": false}`.
- On a CPU a long prompt takes minutes before the first token (75 s at 8k tokens, 576 s at 30k, on 4 cores);
  the server sends a keepalive every 30 s meanwhile. Raise the server's read timeout in Pisa for long
  prompts (`pisa models server <id> --read-timeout 600`; the default is 60 s).

## In Pisa

- Base URL: `http://<host>:8080/v1`
- API key: the `--api-key` value, if you set one.
- The model id is the single entry `/v1/models` lists. As the server has only one model, Pisa's capability
  and probe requests may leave the model out.

## What the probe reports

| Capability | With `--jinja` | Without Jinja (older versions, or `--no-jinja`) |
|---|---|---|
| `tools` | true for a model with a tool-use template | false: the server refuses tools |
| `tool_choice` | false on b12679 ("required" is ignored); the probe tells | false (not tried) |
| `json_schema` | true (the schema becomes a grammar) | true |
| `json_object` | true | true |
| `vision` | true with a projector loaded | same |
| `audio` | false, unless the server offers `/v1/audio/transcriptions` for the model | same |
| `embeddings` | true for a model with pooling | same |

## Sources

- https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md
- https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
