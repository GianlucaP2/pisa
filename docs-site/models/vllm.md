# vLLM

Recommended for a server with a GPU. vLLM serves the OpenAI API on port 8000 by default.

## Launch

Tool calling is off unless you turn it on. Pisa's conversation needs it, so start vLLM with
`--enable-auto-tool-choice` and the `--tool-call-parser` for the model's family:

```sh
vllm serve Qwen/Qwen3-8B \
  --host 0.0.0.0 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --api-key "$VLLM_API_KEY"
```

| Model family | `--tool-call-parser` |
|---|---|
| Qwen 2.5 / Qwen 3, Hermes | `hermes` |
| Llama 3.1, 3.2, 4 (JSON format) | `llama3_json` (Llama 4 is recommended with `llama4_pythonic`) |
| Mistral | `mistral` |
| gpt-oss | `openai` |

vLLM's tool-calling page lists the other parsers. `--api-key` is optional; without it the server accepts any
request, so keep it on a private network. For HTTPS, add `--ssl-certfile` and `--ssl-keyfile`; if a private
authority signed the certificate, give Pisa that authority's PEM file as the CA bundle.

Structured output (`response_format` with a JSON schema) is on by default; nothing to add.

## In Pisa

- Base URL: `http://<host>:8000/v1`
- API key: the `--api-key` value, if you set one.
- The model id is the name vLLM serves (the `vllm serve` argument, or `--served-model-name`).

## What the probe reports

| Capability | With the flags above | Without `--enable-auto-tool-choice` |
|---|---|---|
| `tools` | true | false: vLLM refuses tools without a parser |
| `tool_choice` | true (vLLM forces the call with structured outputs) | false (not tried) |
| `json_schema` | true | true |
| `json_object` | true, if the server accepts JSON mode; the probe tells | same |
| `vision` | true only for a vision-language model | same |
| `audio` | true only for a speech-to-text model (e.g. Whisper) served on `/v1/audio/transcriptions` | same |
| `embeddings` | true only for an embedding (pooling) model | same |

If `tools` is false although you passed the flags, check that the parser matches the model: with the wrong
parser the model writes its call as text, and the probe doesn't count that.

## Sources

- https://docs.vllm.ai/en/latest/features/tool_calling.html
- https://docs.vllm.ai/en/latest/features/structured_outputs.html
- https://docs.vllm.ai/en/latest/cli/serve.html
