# LM Studio

A desktop app that also runs an OpenAI-compatible server. Its examples use port 1234.

## Launch

Load a model and start the server, from the app's Developer page or with the `lms` command:

```sh
lms load qwen2.5-7b-instruct --identifier qwen2.5-7b
lms server start --port 1234 --bind 0.0.0.0
```

- `--bind 0.0.0.0` is only needed when Pisa runs on another machine; LM Studio warns that any bind other than
  `127.0.0.1` exposes the server beyond your computer.
- Authentication is off by default. Turn it on in the Developer page's Server Settings and create a token
  (Manage Tokens → Create Token); clients send it as `Authorization: Bearer <token>`.
- `--identifier` keeps the model's id stable across loads.

Tool calling works on `/v1/chat/completions` for every model: natively for the models marked with a hammer
(Qwen 2.5 Instruct, Llama 3.1 / 3.2, Ministral 8B among them), through a default format for the others, with
results that vary. Structured output takes a JSON schema in `response_format`.

## In Pisa

- Base URL: `http://<host>:1234/v1`
- API key: the token, if authentication is on.
- The model id is the identifier `/v1/models` lists.

## What the probe reports

| Capability | Expected |
|---|---|
| `tools` | true for a model with native tool use; the probe tells for the others |
| `tool_choice` | the probe tells (LM Studio's docs don't describe it) |
| `json_schema` | true |
| `json_object` | the probe tells (the docs show only JSON schemas) |
| `vision` | true only for a vision model |
| `audio` | false |
| `embeddings` | true only for an embedding model |

## Sources

- https://lmstudio.ai/docs/developer/openai-compat
- https://lmstudio.ai/docs/developer/openai-compat/tools
- https://lmstudio.ai/docs/developer/openai-compat/structured-output
- https://lmstudio.ai/docs/developer/core/authentication
- https://lmstudio.ai/docs/cli/server-start
