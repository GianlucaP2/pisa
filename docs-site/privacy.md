# Privacy and consent

Everything Pisa knows about you is in its own Postgres database, on your server. It calls out only to the
providers you configure (AI, maps, voice, web, the connectors you switch on), and tells you which.

## What goes to the AI providers

- `GET /v1/ai/disclosure` lists, per feature slot, which provider and model receives which categories of your
  data. Its `revision` changes whenever a provider, a model or a data category does.
- A client shows it and asks for your consent, including whether health data may go to an AI provider, and
  records it with `POST /v1/ai/consent` ([Text chat](build-a-client/text-chat.md#consent-first)); the PISA app
  does the same. A run carries the consent it was given; work started without it (a watch, Shortcuts) uses the
  consent recorded with `POST /v1/ai/consent`. When the revision is no longer current, Pisa answers
  `409 ai_consent_required` until you consent again.
- A connector that sends data to a provider other than Pisa shows where, before it is switched on
  ([Connectors](connectors.md)).

## Voice

Recording and keeping your voice (for the voice gate, `PISA_VOICE_GATE`) needs your explicit consent
(`POST /v1/voice/consent`, with the version of the text you agreed to). Without an enrolled voice, the gate
does nothing.

## How long things are kept

- Conversation messages: forever (the default), one year or 90 days, your choice in Settings
  (`messages_retention`; `GET /v1/settings/messages_retention/preview` shows what a shorter value would remove).
  A change waits a day before the nightly prune (03:30) acts on it. What Pisa learned from the messages stays.
- Everything else is kept until you delete it; only a closed list of history goes by age.

## Deleting

`POST /v1/data/erase` deletes one family of your data older than a date: `messages`, `places`, `mail`,
`money`, `records`, `memory`, `health`, `voice` or `learn`. `GET /v1/data/erase/preview` counts what would go
first. A delete needs a signature, by any paired client's own device key
([Deleting data](build-a-client/erase.md); `pisa client erase all` does it from the command line) or by the PISA
app's Face ID key, and runs in the background.

A single memory is forgotten from where it is shown (`POST /v1/memory/episodes/{id}/forget`).

## Stopping Pisa's own initiative

"Pisa, stop everything" (`POST /v1/stop`) pauses what Pisa does on its own initiative; your own requests still
work. `POST /v1/stop/resume` resumes.

## On the server

- Provider keys, tokens and connector credentials are encrypted with `PISA_SECRETS_KEY`; credentials are
  write-only.
- Device sessions are short (1 hour), signed by each device's own key ([Pairing](pairing.md)).
- Push notices in relay mode are sealed to the device's key: the relay sees no text
  ([Notifications](notifications.md)).
