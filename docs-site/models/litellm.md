# LiteLLM proxy

The LiteLLM proxy puts one OpenAI-compatible address in front of several servers. It listens on port 4000
by default.

## Launch

A `config.yaml` names each model and where it is served:

```yaml
model_list:
  - model_name: qwen3-8b
    litellm_params:
      model: hosted_vllm/Qwen/Qwen3-8B
      api_base: http://192.168.1.20:8000/v1
  - model_name: llama-small
    litellm_params:
      model: hosted_vllm/llama-3.2-3b-instruct
      api_base: http://192.168.1.21:8080/v1

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

```sh
litellm --config /path/to/config.yaml
```

`master_key` is optional; with it, every request needs that key. A value written `os.environ/<VAR>` is read
from the environment.

## In Pisa

- Base URL: `http://<host>:4000/v1`
- API key: the master key (or a virtual key made from it), if you set one.
- The model ids are the `model_name` values.

## What the probe reports

The proxy passes requests on to the model behind each name, so every capability is the backend's: see that
server's page. Probe each model through the proxy: the result reflects both the backend and what the proxy
passes on, which is exactly what Pisa will get.

## Sources

- https://docs.litellm.ai/docs/proxy/configs
- https://docs.litellm.ai/docs/providers/vllm
