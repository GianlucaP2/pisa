# Changelog

All notable changes to PISA are written here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). From the first release on, release-please writes the entries from the
Conventional Commit titles.

## [0.1.4] - 2026-11-25

- Places from your phone first: with the PISA app (contract 2.3, `pisa.maps`), place search and travel times
  use the phone's own maps, with no key. Then Google or Apple with your key, then OpenStreetMap as a free
  fallback (Photon search, OpenRouteService travel times with your free key). OpenStreetMap results carry their
  credit.
- Notifications scheduled on the phone (`pisa.notify`): reminders, dated tasks, event leads and leave alerts
  for the next 7 days are scheduled by the phone itself, with no push key. Lock-screen text is minimal by
  default. Pisa says "te lo ricordo" only when the reminder will really reach you, otherwise "lo vedrai quando
  apri l'app". `pisa setup` defaults to it.
- Collegamenti (Connections): one view of every connection (`GET /v1/connections`), each one's state, cost
  and what leaves your server. A delete for every key. Google Maps and Tavily are called only after you OK
  them and their own monthly cap (`pisa client paid-ack` from the command line). `pisa google setup --gcloud`
  enables the Google APIs for you.
- Conversations: "Nuova conversazione" starts fresh without deleting anything (`POST /v1/conversations/new`);
  deleting a device's conversations is a signed action (`DELETE /v1/conversations/{threadId}`). Tasks,
  reminders and Memoria stay.
- Your own model server: tested with llama.cpp and Ollama. Tool calls never leak into what Pisa says; a long
  prompt a server cuts off is noticed and reported; a per-server read timeout; thinking switchable per slot;
  memory embeddings from your own server; `pisa setup --ai own` fills every slot it can. vLLM, SGLang and LM
  Studio are untested on a live server so far.
- `pisa setup` follows the app's Collegamenti, and the docs state who runs what: you operate your server and
  choose your providers; PISA is provided as is (AGPL-3.0). No push relay is offered by the project today.
- Since 0.1.3's audit: a reminder is cancelled by voice ("annulla il promemoria per Anna"); with no maps or
  web provider set up, Pisa says so in one line instead of asking where you are; asked about your day, the
  answer carries the total of your tasks.

## [0.1.3] - 2026-11-24 (not released on its own; shipped in 0.1.4)

- Security: protobuf, pyasn1, dnspython and opentelemetry-api are updated to their patched versions (ortools
  moves to 9.15 with protobuf). The test suite builds every credential-shaped value at run time, so the secret
  scanners run with no exclusions.
- Asked what the day holds, Pisa names the tasks on your list; it never says there is nothing to do while there
  is.
- An answer written beside a tool call is no longer lost when the next step says something else.
- A promised reminder must match the reminder it names; "te lo ricorderò" and "ti avviso alle 18" are checked
  too.
- Revoking a device while its talk is still opening ends that talk as soon as it opens.
- On the Apple Watch, answers are short (at most 25 words, two sentences), with the rest on the phone; a watch
  answer is never only a card.
- A missing places provider is said plainly as not set up, not reported as a failure; place failures are logged
  with a reason code.
- Google setup: the sign-in window (10 minutes) and `redirect_uri_mismatch` are explained in the app, the
  `pisa google setup` guide and the docs.
- `pisa setup`: direct notifications name what is missing and how to set it; each model slot offers its current
  model.
- A new README.

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
