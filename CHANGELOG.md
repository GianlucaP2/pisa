# Changelog

All notable changes to PISA are written here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). From the first release on, release-please writes the entries from the
Conventional Commit titles.

## [0.1.6] - 2026-11-26

- Mail becomes storylines. Each new email is read once, for what happened in it and what it asks; Pisa links
  it by its exact order, tracking or booking number to a storyline (a sale, a purchase, a refund case, an
  appointment, a request) and follows it to its end. A task comes only from a step that needs you, with the
  email's own words as its evidence, and you can take any change back. Nothing closes because time passed,
  and a storyline closes only on its exact number; otherwise Pisa asks.
- Checks are tasks too: after "refund granted" Pisa reminds you to check the money arrived, and after a
  delivery to check the item before the return window closes.
- Tell Pisa "ho già spedito", "l'ho pagato" or "risolto" and the storyline moves; the morning briefing lists
  what moved since yesterday.
- What you wrote earlier, quoted in a reply, is never taken as a request to you, and a forwarded message's old
  text is never read at all: only what its sender wrote on top counts. Mail text is kept 30 days at
  most, and everything read from Gmail goes when you disconnect it.
- A reply you send is read as your answer: the request it answers stops asking you, and what you promise in it
  stays yours to do. A mailed "done" counts as your reply; to close a storyline yourself, tell Pisa in chat.
- One language per answer, no progress line in casual talk, and asked for your day's count, Pisa names the
  tasks. A typed question that waits for a list waits for the whole answer, and an answer that came in
  another language than the one you wrote in is asked again in yours, never dropped.
- A reminder is said at the time it was set ("12:29 AM (00:29)" in English, never "12:29" for just after
  midnight).
- Place search with OpenStreetMap works in Italian again (Photon was asked in a language it does not take).
- Google AI only within the Gemini API's terms, on the operator's own declaration (`google_ai_tier`): `paid`
  (billing on: Google's models and Gemini Live), or `free_eea_own_use` ("I am in the EEA, Switzerland or the
  UK, and I am the only person who uses this Pisa with this key": Gemini Live only). `pisa setup` asks
  whether billing is on, and otherwise both questions of the declaration; two yeses are needed. Until then
  (`unknown`, or `free`), nothing goes to Google, and an existing Google slot stops. Anyone serving other users
  must use a paid key.
- `/health` reports the real version.

### Known issues

- One sentence of a reply can still mix two languages ("Per your list, domani…"): Pisa checks a reply's
  language sentence by sentence, so it is said as written.

### Protocol (contract 2.5, additive)

- The `pisa.notify` frames (`schedule`, `cancel`, `regions`) carry an optional `epoch`: when it changes, the phone
  resets its last applied revision. A restore of the database makes a new one (`pisa notify reset-epoch`).
- A new error code, `google_ai_tier_not_paid` (409): a model slot on Google while `google_ai_tier` isn't `paid`,
  or a Gemini Live talk while it is neither `paid` nor `free_eea_own_use`. The app shows its message and
  action as for any error; a talk takes economy.

### REST (additive)

- `GET/PATCH /v1/settings`: `google_ai_tier` (`unknown` by default, `free`, `free_eea_own_use` or `paid`). The
  app shows no control for it yet: set it with `pisa setup` or a PATCH.
- `GET /v1/connections`: each entry has an optional `setup` (short setup steps in the user's language) and
  `free_tier` (its free quota, or null), from what the guides already state.
- `GET /v1/owner/aliases`, `POST /v1/owner/aliases/{address}/confirm` and `POST
  /v1/owner/aliases/{address}/forget` (contract 2.5): the addresses Pisa saw the user write from, confirmed or
  forgotten once. A confirmed one makes the sent mail from it the user's own words; confirming is always signed
  (`device` challenge, op `alias_confirm`, target the address in lowercase).

## [0.1.5] - 2026-11-25

- Reminders you ask for are set again. Since 0.1.0, a reminder you asked for, at a time or at a place, could
  be refused («Non ci sono riuscito»), mostly when your task list held tasks made from email. Now a reminder,
  a place note or a task you ask for is set, as long as its words and its time are yours and not only in an
  email or page Pisa read. Anything that leaves your server still waits for your yes.
- A reminder you confirm is said with the time it was really set for ("Reminder set for 5:57 PM.").
- A spoken answer starts as soon as its first sentence is ready, and nothing said aloud is said again later in
  the same answer. A typed answer is one reply, in the language you typed.
- Places: OpenStreetMap results carry their opening hours, and "open now" with OpenStreetMap only keeps the
  places that are open. "No ratings or prices" is said only when you asked for them.
- Dependencies: pyjwt, cryptography and pytest have minimum versions without known vulnerabilities.

### Protocol (contract 2.4, additive)

- `pisa.presence`'s `event` is optional: a route change or a lock with no event of its own sends `state` alone.
- `POST /v1/talk/sessions` takes an optional `mcpApps` (the live hello's shape). A talk sends drawings as
  `mcp-apps` views only to a phone that declared them; otherwise as a note card with the drawing's title and
  summary.

### Known issues

- A reply can still mix Italian and English, or add a short line of its own in casual talk.
- Asked for today's count, Pisa can give the number without naming the tasks.
- Both are fixed in 0.1.6.

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
