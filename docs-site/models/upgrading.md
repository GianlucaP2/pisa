# Upgrading: models served before automatic probing

Earlier versions of Pisa probed a model on your own server only when you asked. A model that was never
probed reports every capability as false, so after an upgrade a model you already use may show no
capabilities, and a use that needs one (the conversation needs tool calling) fails with
`model_capability_missing` and "the model was never probed".

Pisa now probes such a model by itself, once:

- **When you choose it for a use**, in Settings → AI models or with `pisa models set <slot> <server:model>`.
  The probe runs before the change is saved, and the answer says what it found. If the server can't be
  reached, the change is saved all the same: the model stays unprobed, and the answer says so.
- **On its first missing capability.** When a use needs a capability that a never-probed model doesn't show,
  Pisa probes the model and checks again, once. If the probe finds the capability, the use goes ahead. If
  the probe confirms it is missing, the error stands, and it now says what to change on the server. If the
  server can't be reached, the error stands and says that the server could not be reached for probing.

A model that was probed is never probed again by itself: what its probe found is an answer, not a guess. Two
uses that miss at the same time run one probe; the second waits for its result. Models of the built-in
providers are never probed.

## Probing by hand

Probe a model again after you change the server (a tool-call parser turned on, another model loaded under the
same name), or when the server was down when Pisa tried:

- From a client, with the API call below (in the PISA app: Settings → AI providers).
- On Pisa's server: `pisa models probe home-gpu:Qwen/Qwen3-8B`.
- Through the API:

  ```sh
  curl -X POST "https://pisa.example/v1/ai/providers/home-gpu/probe?model=Qwen/Qwen3-8B" \
    -H "Authorization: Bearer $TOKEN"
  ```

A probe sends a few tiny requests and may take a few minutes on a CPU. Your overrides are kept: they still
win over what the probe finds.
