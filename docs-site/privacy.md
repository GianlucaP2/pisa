# Privacy policy

This policy covers the Pisa server and the PISA app for iPhone and Apple Watch. It is short because the
design is simple: **you run the server, and your data lives there.** Last updated: November 2026.

## What we collect: nothing

The PISA project (its maintainers, and whoever publishes the app) **collects no data about you**. There is no
PISA account, no PISA cloud service and no PISA server that your data passes through. We never receive your
messages, your keys, your health data, your mail or anything else.

- **No tracking and no analytics.** The app has no analytics, advertising or tracking code of its own, and
  does not track you across apps or websites. It includes Meta's Wearables SDK, for Meta smart glasses, with
  that SDK's analytics and crash reporting switched off.
- **No accounts with us.** You pair the app with your own server; there is nothing to sign up for, and nothing
  to delete with us.

## Where your data goes

- **To your own server.** The app talks only to the Pisa server you installed and paired it with. Everything
  Pisa knows about you is kept there, in its own database, on a machine you control.
- **To the providers you choose, and only those.** Your server sends data out only to the AI providers and the
  data providers you set up yourself, with your own keys and accounts: for example an AI provider (Anthropic,
  OpenAI, Google Gemini…), a maps or web-search provider, Google for your mail and calendar, a bank through
  Enable Banking. Each of them handles that data under its own terms and privacy policy. If you set up none,
  nothing leaves your server.

## AI: you see it, then you decide

Before any of your data goes to an AI provider, Pisa shows you **the AI disclosure**: which provider and which
model receives which kinds of your data, for each feature. Nothing is sent to an AI until you agree, on the
app's consent screen (or with `pisa consent` on the server). When the providers, the models or the kinds of
data change, Pisa asks you again. If you choose one of the server's voices (OpenAI or ElevenLabs) instead of
the phone's own, everything Pisa says to you goes to that provider to be spoken, so it is listed in the
disclosure too. You can withdraw by removing the provider's key or choosing other models.

## Health data

- The app reads Apple Health (HealthKit) only if you allow it, and only the kinds of data you allow. It sends a
  daily summary **to your own server only**.
- Health data reaches an AI provider **only if you give the separate health consent**, which is off until you
  turn it on. Without it, Pisa does not send a request that touches your health to any AI.
- Health data is never used for advertising or sold, and is not stored in iCloud by the app.

## Google data

If you connect Gmail and Google Calendar, you do it with **your own Google Cloud project and OAuth client**,
and the sign-in comes back to **your own server**. Your Google tokens are kept there, encrypted. Your use of
Google data is under Google's
[API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), with you as
the project's owner. The PISA project never receives your Google data, tokens or keys
([Connectors](connectors.md#google)).

## Keeping and deleting

You decide how long conversation messages are kept, and you can delete your data from the app or the server
at any time (details below). Deleting the app removes what it keeps on the phone. Since nothing is held by the
PISA project, there is nothing for us to delete.

## Contact

Questions about this policy: open an issue in the **Issues** tab of the Pisa repository on GitHub, the
repository this site is published from. Please don't post personal data in a public issue.

## The details

The rest of this page is the technical detail behind the policy, for people running a server.

### What goes to the AI providers

- `GET /v1/ai/disclosure` lists, per feature slot, which provider and model receives which categories of your
  data. Its `revision` changes whenever a provider, a model or a data category does.
- The server's voice is listed as `conversation.voice` when an OpenAI or ElevenLabs voice is chosen (and its
  key is set): every sentence Pisa says, which may include your mail, your calendar or an answer about your
  health, is sent to that provider to be spoken, so it receives the same categories as the conversation.
  Choosing a voice from a provider your consent doesn't cover needs consent again before Pisa speaks with it;
  the phone's own voice is no processor. Without that consent, a notice is spoken by the phone, not sent to
  the provider.
- A client shows it and asks for your consent, including whether health data may go to an AI provider, and
  records it with `POST /v1/ai/consent` ([Text chat](build-a-client/text-chat.md#consent-first)); the PISA app
  does the same. A run carries the consent it was given; work started without it (a watch, Shortcuts) uses the
  consent recorded with `POST /v1/ai/consent`. When the revision is no longer current, Pisa answers
  `409 ai_consent_required` until you consent again.
- A connector that sends data to a provider other than Pisa shows where, before it is switched on
  ([Connectors](connectors.md)).

### Voice

Recording and keeping your voice (for the voice gate, `PISA_VOICE_GATE`) needs your explicit consent
(`POST /v1/voice/consent`, with the version of the text you agreed to). Without an enrolled voice, the gate
does nothing.

### How long things are kept

- Conversation messages: forever (the default), one year or 90 days, your choice in Settings
  (`messages_retention`; `GET /v1/settings/messages_retention/preview` shows what a shorter value would remove).
  A change waits a day before the nightly prune (03:30) acts on it. What Pisa learned from the messages stays.
- Everything else is kept until you delete it; only a closed list of history goes by age.

### Deleting

`POST /v1/data/erase` deletes one family of your data older than a date: `messages`, `places`, `mail`,
`money`, `records`, `memory`, `health`, `voice` or `learn`. `GET /v1/data/erase/preview` counts what would go
first. A delete needs a signature, by any paired client's own device key
([Deleting data](build-a-client/erase.md); `pisa client erase all` does it from the command line) or by the PISA
app's Face ID key, and runs in the background.

A single memory is forgotten from where it is shown (`POST /v1/memory/episodes/{id}/forget`).

### Stopping Pisa's own initiative

"Pisa, stop everything" (`POST /v1/stop`) pauses what Pisa does on its own initiative; your own requests still
work. `POST /v1/stop/resume` resumes.

### On the server

- Provider keys, tokens and connector credentials are encrypted with `PISA_SECRETS_KEY`; credentials are
  write-only.
- Device sessions are short (1 hour), signed by each device's own key ([Pairing](pairing.md)).
- Push notices in relay mode are sealed to the device's key: the relay sees no text
  ([Notifications](notifications.md)).
