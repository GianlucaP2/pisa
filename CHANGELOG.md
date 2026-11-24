# Changelog

All notable changes to PISA are written here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). From the first release on, release-please writes the entries from the
Conventional Commit titles.

## [0.1.2] - 2026-11-24

- Memoria: standing instructions ("d'ora in poi rispondimi sempre in modo breve") that every turn follows, and
  free notes Pisa keeps and finds again ("annota il codice del cancello"). Settings → Memoria lists, edits and
  deletes them (`/v1/memoria`). A brevity instruction is enforced, not only suggested; an instruction can shape
  style and content, never tools, consent or approvals. The v0.1 note is moved into Memoria.
- Timed reminders are real: "ricordami tra 2 minuti", "fra un'ora", "domani alle 9" are stored and fire at that
  minute, as a push when the app is closed. Pisa says "te lo ricordo alle …" only for the time it set.
- The chat holds what Pisa said: a correction is one short line of its own, never spliced into the answer, and
  in a talk the stored reply is what you heard. Claims about a task follow what happened to it.
- One answer per turn: an earlier offer never opens or splits a reply.
- Talks: typing into an open talk (`talk.text`, contract 2.1); a talk's turns are shown once; ending a talk
  never brings a late answer; "e poi…" at the end of a sentence waits for the rest; revoking a device ends its
  talk at once.
- The app as context (contract 2.2): the app sends its screen map, so app help describes the app you have; the
  riepilogo follows the current plan; Pisa can open a tab, the riepilogo, a task or AI e privacy; a server
  setting (the briefing's time and rules, reminder lead times) changes only after your yes, with Undo.
- Messages, calls and directions are offered only for the phone extensions the app granted.
- A weather card (catalog core:2, `WeatherSummary`); Google tells each device whether it started a pending
  sign-in; `pisa setup` explains the places providers.
- Logs hold no user data at any level, debug included; provider failures are logged by status only.
- AI consent covers Pisa Learn (checked again when a source is read), briefings, place research and Memoria
  notes; the server's voice provider is named in the disclosure.
- A model slot that rests is reported once an hour and counted in `/readyz`; quick model calls leave room for
  reasoning.
- `install.sh` works piped (`curl … | PISA_REPO=… PISA_VERSION=… bash`).
- App Store review mode: Pisa Learn, handoffs and server settings are off; a review database used for real
  refuses review mode again.

## [0.1.1] - 2026-11-22

- Features that are only stubs or were never built (Garmin, Polar, Withings, bank linking, PEC and the admin
  radar, the cassetto fiscale, the xAI voice) are off by default. Everything built is on. An operator turns an
  off one on with `PISA_EXPERIMENTAL` (`docs-site/experimental.md`). An off feature has no routes (404), no
  tools and no settings card, and Pisa says it isn't available on PISA instead of sending you to Settings.
- A reminder is promised only when one was set. "I'll remind you tomorrow" with only a task on the list becomes
  "it's on your list; you won't get an alert at a set time", and offers of a feature this server doesn't have
  are held back.
- One turn, one answer: a sentence is never said twice in a reply, and a continuation never repeats the reply
  it continues.
- A search for a feature that is off gets one plain answer, so Pisa stops looking.
- Messages, calls and directions are offered only to an app that can show them. Over plain HTTP Pisa gives
  you the text instead of saying "tap Send".
- Google uses your own OAuth client (Personal mode): the sign-in returns to your server, and a new account is
  confirmed before it's kept.
- The AI disclosure names the server's voice (OpenAI speech or ElevenLabs) when one is chosen, and picking a
  voice provider you haven't agreed to asks first. Pisa Learn, briefings and place research ask for AI consent
  too.
- Consent given for one set of AI providers covers a smaller set later: switching from the natural voice to
  the economy one doesn't ask again.
- The night stops once your daily or monthly AI caps are reached.
- App Store review mode (`PISA_REVIEW_MODE=1`, `docs-site/app-review.md`): a fresh demo database, review pairing
  codes for up to 3 devices for 30 days, no connectors and nothing that reaches outside the server. Review
  devices can't pair others or register Face ID keys, and `pisa pair --review --revoke` ends them all. It
  refuses to start on a database that already holds data.
- A documentation site (`docs-site/`, built with MkDocs).

## [0.1.0] - 2026-11-20

- The first public version of PISA: the server (`core/`), the push relay (`relay/`), and the device protocol
  (`protocol/`).
- Replies never contain tool-call syntax: a call the model writes as text is said as its words, or not at all.
- "What can you do" and every reply name only what this install and this client can do. A client that didn't
  pick the phone's extensions (any run over HTTP) is never offered messages, calls or directions, and an offer
  Pisa holds back takes its follow-up question with it.
- When a reply already says something isn't connected, Pisa doesn't repeat it; the notice names only what was
  claimed.
- A task title written in another language than the user's words is restated in theirs. A title they said word
  for word stays as it is.
- One message that names several tasks marks each one done ("I bought milk, bread and eggs").
- "Undo" right after Pisa added a task removes that task; it can be restored for 24 hours.
