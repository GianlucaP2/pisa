# Text Generation Inference (may work)

Hugging Face's Text Generation Inference (TGI) is archived and in maintenance mode: its repository is
read-only. Pisa is not tested with it. It has an OpenAI-compatible Messages API at `/v1/chat/completions`,
so it may work.

## Launch

```sh
model=Qwen/Qwen2.5-7B-Instruct
docker run --gpus all --shm-size 1g -p 8080:80 -v "$PWD/data:/data" \
  ghcr.io/huggingface/text-generation-inference:3.3.5 --model-id $model
```

## In Pisa

- Base URL: `http://<host>:8080/v1`
- No API key, unless a proxy in front of it asks for one.

## What the probe reports

Probe the model and trust the result: whatever TGI doesn't answer the way the probe checks stays false, and
the uses that need it won't take the model. If the probe finds no tool calling, give the conversation another
model and use TGI's model only for the uses that need nothing more than structured output or plain text.

## Sources

- https://github.com/huggingface/text-generation-inference
