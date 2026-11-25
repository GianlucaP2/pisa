# SGLang

!!! warning "Untested on a live server"
    Pisa has not yet been tested against a live SGLang server; the probe checks what it actually does.
    See [What has been tested](index.md#timeouts-and-thinking).

SGLang serves the OpenAI API on port 30000 by default, on 127.0.0.1 unless you change `--host`.

## Launch

Tool calling needs `--tool-call-parser` for the model's family (or `auto`, which reads it from the chat
template):

```sh
python3 -m sglang.launch_server --model-path Qwen/Qwen2.5-7B-Instruct \
  --host 0.0.0.0 --port 30000 \
  --tool-call-parser qwen \
  --api-key "$SGLANG_API_KEY"
```

| Model family | `--tool-call-parser` |
|---|---|
| Qwen (except Qwen3-Coder) | `qwen` |
| Llama 3.1, 3.2, 3.3 | `llama3` |
| Mistral | `mistral` |
| gpt-oss | `gpt-oss` |

`--api-key` is optional. For HTTPS add `--ssl-certfile` and `--ssl-keyfile`. Structured output uses the
XGrammar backend by default; forcing a tool call (`tool_choice`) works fully with it.

## In Pisa

- Base URL: `http://<host>:30000/v1`
- API key: the `--api-key` value, if you set one.
- The model id is the one `/v1/models` lists (the model path unless you renamed it).

## What the probe reports

| Capability | With `--tool-call-parser` | Without it |
|---|---|---|
| `tools` | true | false |
| `tool_choice` | true with the default XGrammar backend; may fail with another backend | false (not tried) |
| `json_schema` | true | true |
| `json_object` | the probe tells (SGLang's docs show only JSON schemas) | same |
| `vision` | true only for a vision-language model | same |
| `audio` | false, unless the server offers `/v1/audio/transcriptions` | same |
| `embeddings` | true only for an embedding model | same |

## Sources

- https://docs.sglang.io/advanced_features/tool_parser.html
- https://docs.sglang.io/advanced_features/structured_outputs.html
- https://docs.sglang.io/advanced_features/server_arguments.html
