# Pisa Device Protocol

This is the shared contract between Pisa Core and every device app (iPhone today; Android, web, wearables and car
later).

- **Schema**: `schema/pisa-device-protocol.v1.json` (JSON Schema 2020-12). Changes to it are
  proposed and reviewed, never made on the side.
- **Fixtures**: `fixtures/valid/*.json` must validate and `fixtures/invalid/*.json` must not. **Both sides test
  against these same files**:
  - CORE: Pydantic models parse every valid fixture, re-serialize it, and reject every invalid one.
  - IOS: Swift `Codable` types do the same.

## Transport

- **WebSocket** `GET /v1/device`, with the header `Authorization: Bearer <device_token>`. It is served over HTTPS by
  `tailscale serve`, so it is only reachable inside the user's tailnet.
- **Text frames**: one JSON message per frame. Every message has `v` (=1), `type`, `id` (UUID) and `ts`
  (RFC 3339 with offset).
- **Handshake**: the device sends `hello` first. Pisa answers `welcome`, or `error` and then closes.
- **Keep-alive**: the device sends `ping` every 25 s and Pisa answers `pong`. After 3 missed pongs the device
  reconnects, with backoff of 1, 2, 4, 8 and then 15 s maximum.
- **Replies**: `say` chunks stream in `seq` order. A device may start speaking at the first sentence boundary.
  The last chunk has `final=true`, and its `delta` may be empty.
- **Cancel**: `cancel.target_id` may be a reply's `stream_id`, or the `id` of the `utterance` that a reply is
  answering (for example when the reply hasn't started yet). After a cancel, Pisa sends no more chunks for that
  reply, not even a final one.
- **Presence**: the device sends `presence` with `glasses_on` / `glasses_off` (the glasses' wear sensor),
  `app_foreground` / `app_background` (the app lifecycle), `earbuds_connected` / `earbuds_disconnected` (the audio
  route) and `companion_on` / `companion_off` (companion mode). It sends one on every change, and the
  current state right after `welcome`. Pisa uses these to trigger the briefing and to route proactive
  messages (spoken over the WebSocket in companion mode with earbuds, otherwise pushed); Pisa never replies to them.
- **Check-ins**: a `notify` with `kind: check_in` carries `check_in_id` and `choices`; with
  `expects_reply` the device listens for a spoken answer right after speaking it (`utterance.wake: reply`). A tapped
  choice goes to `POST /v1/check-ins/{id}/answer`.
- **Done**: a `notify` with `kind: done` says Pisa closed a task (`task_id`) or marked today's event attended
  (`event_id`); it carries `title`, `place` (absent at a sensitive place, and when the user said it rather than
  Pisa seeing them there) and `undo_until`. Its push has the same
  fields and the APNs category `DONE` (one action, Undo). See "Done by being there" below.
- **Directions**: Pisa sends `directions` ("portami a…"); the device opens its maps app in the foreground, or shows a
  notification that opens them when tapped.
- **Close codes**:

  | Code | Meaning | Device should |
  |---|---|---|
  | 1000 | normal close | — |
  | 1002 | protocol error (the message before it says why) | reconnect with backoff; if the preceding error was `protocol_version_unsupported`, **stop** and ask the user to update the app |
  | 1011 | server error | reconnect with backoff |
  | 4001 | replaced by a newer connection of this device | don't auto-reconnect; offer "Reconnect" |
  | 4003 | auth failed | delete the token and go to pairing |
  | 4008 | no `hello` within 10 s | reconnect with backoff |

## Pairing (getting a device token)

1. A 6-character code valid for 10 minutes comes from a device that is already paired (Settings → Keys and
   devices → Pair another device: `POST /v1/devices/pairing-code`), or from the server
   (`pisa devices pair --name "iPhone"`, the first time).
   **Signed**: from the app, the code needs the phone's signature (below, `device` op
   `pairing_code`, target the name) once Pisa has a Face ID key. A device paired with an app code never registers
   Pisa's first Face ID key and is never the primary phone: only an iPhone paired with a host code is.
2. The app calls `POST /v1/devices/pair` with `{"code", "name", "platform"}` and gets back
   `{"device_id", "device_token"}`. The token is shown only once, and the app stores it in the iOS Keychain.
3. The server stores only a hash of the token. Revoking a device (`DELETE /v1/devices/{id}` from the app, or
   `pisa devices revoke <device_id>` on the server) cuts it off; the app never revokes the last active device.
   Revoking **another** device from the app needs the phone's signature (`device` op `revoke_device`, target
   its id); a device revokes itself without one.
4. **The user's actions are signed**: once Pisa holds a Face ID key (the phone key store), a
   copied device token alone can't take the account over. `POST /v1/phone/challenge {action: "device", op,
   target}` → `{nonce, canonical, expires_at, action}`, canonical = `pisa-device\nop=<op>\ntarget=<target>\n
   nonce=<nonce>\nexpires_at=<iso>`; the phone shows it, asks Face ID and signs it; the request then carries
   the headers `X-Pisa-Nonce`, `X-Pisa-Signature` (DER, base64) and `X-Pisa-Key-Id`. Without them, or signed for
   another op or target, or used twice: 403 `biometric_required`. The ops and their targets:

   | Route | `op` | `target` |
   |---|---|---|
   | `POST /v1/devices/pairing-code` | `pairing_code` | the name in the body |
   | `DELETE /v1/devices/{id}` (another device) | `revoke_device` | the device id |
   | `PUT /v1/ai/providers/{id}/key` | `provider_key` | `<provider id>:<SHA-256 hex of the key>` |
   | `POST /v1/connectors/google/code` | `google_connect` | the SHA-256 hex of `server_auth_code` |
   | `DELETE /v1/connectors/google` | `google_disconnect` | `google` |
   | `PUT /v1/places/providers/{id}/key` | `place_key` | `<provider id>:<SHA-256 hex of the key>` |
   | `PUT /v1/connectors/google/credentials` | `google_credentials` | the SHA-256 hex of `<web_client_id>\n<web_client_secret>\n<ios_client_id>` |
   | `PATCH /v1/settings` with a **shorter** `messages_retention` (longer or `forever`: unsigned) | `messages_retention` | the new value, `1y` or `90d` |
   | `PATCH /v1/connectors/{id}` | `connector` | `<id>:<SHA-256 hex of the body as sent>` |
   | `PUT /v1/connectors/{id}/credential` | `connector_credential` | `<id>:<SHA-256 hex of the body as sent>` |
   | `DELETE /v1/connectors/{id}/credential` | `connector_credential` | `<id>:delete` |
   | `POST /v1/connectors/{id}/consent` | `connector_consent` | `<id>:start` |
   | `POST /v1/connectors/{id}/consent/code` | `connector_consent` | `<id>:<SHA-256 hex of code>` |

   The server puts the target on one line (control characters become spaces, as the values) and compares a
   `revoke_device` target in lowercase. `PUT /v1/push/key` (the APNs key) stays unsigned: swapping it only breaks
   pushes to the user's own phone.

   Before the first key exists (a fresh install) the token alone does them. `pisa phone-keys reset --keep
   <device_id>` (on the server) removes every key and revokes every device but the one named, an iPhone paired
   with a host code. Fixture: `fixtures/rest/device_challenge.json`.

## Places and the user's profile

- **`card` kind `places`** (fixtures `places_card`, `places_card_v2`): the best 1 to 3 places Pisa found, each
  with `why`, its `checks` and `sources`. v2 adds, per place:
  - `lat`, `lon`: the pin;
  - `photo_count`;
  - `rating_count`: how many ratings `rating` is from;
  - `price_level`: 0–4;
  - `closes_at` (when open now) or `opens_at` (when closed), from the hours Pisa read;
  - `menu_url`: only a page Pisa read that turn;
  - `fits`: up to 3 phrases naming what in the user's profile the place matches;
  - `different`: "something different", a place unlike the user's usual, shown with a label
    and a `why` saying how it differs. At most one per card, about 1 card in 5, never against a hard limit;
    the user switches it off with `PATCH /v1/settings` `{"profile": {"something_different": false}}`.

  `card_id` identifies the card in `POST /v1/places/outcomes`.
- **REST.** The detail sheet `GET /v1/places/{id}/details` (fixture `fixtures/rest/place_details.json`).
  Google's AI summaries come with their `disclosure` (show it with the text) and `flag_uri` (a report link):
  `review_summary` is what the reviews say, `description` describes the place (never shown as the reviews).
  Each review's `author_uri` links its author's name.
  `GET /v1/places/{id}/photos/{index}?w=800` gives a short-lived photo URL with its attribution: show it,
  never store it.
- **The user's profile** (all REST, in `openapi.json`):
  - `GET /v1/profile` (fixture `fixtures/rest/profile.json`): the claims Pisa believes (`candidate`, `active`,
    `asking`); `?include=retired` adds the `retired` ones. Each claim has its `scope` with all four
    keys (`day`, `company`, `where`, `time`; null means any), `support`, `evidence_count` ("seen N times" in
    the last 4 weeks of the weekly check), `state` (`candidate` not used yet, `active`, `asking` in the weekly
    check, `retired` not true), `buffer_min` (a lateness claim the user said yes to: Pisa has them leave that
    many minutes earlier; else null) and its `evidence`; `travelling` is `{"since", "city"}` while the user
    sleeps more than 50 km from home (it ends after two nights back home), else null. Place names in
    statements come in «»;
  - `GET /v1/profile/weekly` (fixture `fixtures/rest/profile_weekly.json`): the weekly check, `{"week",
    "items"}`, at most 3 items of kind `confirm`, `changed`, `new`, `expiring` or `buffer` (empty most weeks).
    Served only from Sunday 18:00 to Monday 18:00 (Rome): any other time `items` is empty. Offered
    then as a `notify` kind `info` with `weekly: true`, at most once a week; its APNs push carries
    `"weekly": true` in its data too;
  - `POST /v1/profile/weekly/{item_id}` `{"answer"}`: `right`, `wrong` or `forget`; a `buffer` takes `yes`
    (Pisa tells the user to leave that many minutes earlier for that kind of event) or `no` (never asked again).
    Only the current week's items, while the check is open: else 400 `validation_failed`;
  - `PATCH` / `DELETE /v1/profile/claims/{id}`: an edit works on any claim not retired, makes it `active` and
    `confirmed`, and the user's sentence and scope stay (Pisa's nightly run no longer rewrites them).
    `{"buffer_off": true}` alone stops leaving earlier for that kind of event: the lateness claim stays and
    the buffer isn't offered again;
  - `POST` / `DELETE /v1/profile/pause`;
  - `POST /v1/profile/off_limits`;
  - `POST /v1/profile/first_talk`: Pisa then sends a `notify` kind `info` with `expects_reply: true`, the
    talk's first line.
  - `POST /v1/profile/off_limits` also covers the user's word for a kind of place in Italian or Dutch:
    "palestra" or "sportschool" covers a place Google files as `gym`.

## Planning with intent
- **Event kinds.** Every event has a kind:
  - `fixed`: a reservation, a meeting, a flight. Pisa never moves it; arriving after its start is late.
  - `window`: a festival, a market, an exhibition day. The user may arrive any time between its start and its
    end minus `min_stay_min`.
  - `soft`: a plan made in Pisa (an event Pisa created for the user).
- **Where the kind shows.**
  - `GET /v1/events?date=YYYY-MM-DD&days=N` (fixture `fixtures/rest/events.json`): each event with `kind`,
    `kind_source` (`inferred` | `owner`) and, for a window, `min_stay_min`.
  - Plan items (contract M): an event item carries the same fields as `event_kind`, `kind_source` and
    `min_stay_min`, because `kind` already says the item's type.
- **The user's kind.** `PATCH /v1/events/{id}` `{"kind": "fixed" | "window" | "soft" | null,
  "min_stay_min"}`.
  - null makes Pisa infer the kind again.
  - It is Pisa's own data: Google Calendar is never edited.
  - The plan is made again with it.
- **A window event moved.** When a stop comes first, the window event's plan item starts at `arrive_at`, the
  end of the trip into it. It is late only after its end minus the minimum stay. Fixture:
  `fixtures/rest/plan_window.json`, the festival with lunch at BRAM'S first.
- **"Plan it" with an intent.** `POST /v1/plan/insert` `{"task_id" | "place_id", "when": "before" | "after" |
  "instead" | "fits", "relative_to": event_id | null, "duration_min"}`.
  - A place becomes a task.
  - `relative_to` null means the next event.
  - The stop goes on the way: here → the stop → the event, never a round trip.
  - When it fits, it is pinned there as an adjustment (`source: app`) and the answer is the new plan
    (contract M).
  - When something fixed is in the way, the answer is options in contract N's shape (fixture
    `fixtures/rest/plan_insert_options.json`): now (late for the event), after it, or instead of it. Apply one
    with `POST /v1/plan/options/{options_id}/apply`.
  - By voice, the `plan_stop` tool does the same: "pranzo prima del festival".
- **Travel modes.** Settings `places.modes`: the modes the user allows, in order of preference, from `walk`,
  `transit` (ferries included), `bike`, `car` and `taxi`.
  - `places.walk_max_min` is the longest walk Pisa plans (default 45). Above it, the next allowed mode.
  - `places.default_mode` is the first of the modes, as routed (car and taxi route as `drive`); setting either
    keeps the other in step.
  - Plan travel items say `mode`: `drive` (car), `transit`, `walk`, `bike` or `taxi`.
  - In the rain a walk goes by the next allowed mode that isn't the bike; with only
    walking and the bike allowed it stays on foot.
- **Overlapping events.** An event item is **one calendar event**: `event_ids` holds its one id,
  `title` its own title, `start`/`end` its own times, `place_id` its own place (none stays none: a wait at home
  in a stop at the park has no `place_id`, `arrive_at` or lateness of its own). Events that overlap each other
  are separate items, each with `overlaps` naming the others it overlaps:
  - `overlaps` is always a list of objects `{"event_id", "title"}`: never a bare id, never a list of strings.
  - It is absent when the event overlaps nothing: never `null`, never `[]`.
  - It is symmetric: the wait lists the run, and the run lists the wait.
  - It names only events of the same route stop (one block of overlapping events).
  - Two events that only touch (14:00-15:00, then 15:00-16:00) don't overlap.

  The route still treats overlapping events as one stop (one trip in, one `leave`); the items say nothing new
  about travel. A wait at home ("Consegna spesa (resta a casa)", a courier, a technician; with no place, or at
  home) is a `window` (`event_kind`): what the user does meanwhile overlaps it on purpose, and it ends when the
  delivery is confirmed, by its email or by the user (its `end` moves to that time; being at home never ends
  it). Fixture: `fixtures/rest/plan_overlap.json`, the grocery wait 12:00-16:00 with the run 14:00-15:00 and
  the gym 15:00-16:00 inside it. Before, overlapping events were one merged item (titles joined by " / ").
- **A soft event moved.** When a stop needs the time, Pisa may move a soft event within its own day
  (from 07:00, ending by midnight), keeping its length, and as little as it can. The event item then carries
  `moved_from` (its start in the calendar) and `why` says so. Only Pisa's plan moves: Google Calendar keeps it
 . Fixed events and a window event's end never move.

## Sources
- **`card` kind `sources`** (fixture `fixtures/valid/card_sources.json`) is sent with every web answer:
  `sources: [{title, url, host}]`, at most 6, with one line per link in `lines`.
- Each `url` is an `http` or `https` link of at most 2,000 characters and each `host` at most 255: a longer
  one is left out, never cut (fixture `fixtures/invalid/card_sources_not_a_web_link.json`).
- The links are only pages this turn's searches returned, those it read first.
- A `places` card already carries its own sources, so no `sources` card follows it.
- The app opens each link in Safari and shows its host.

## Email precision
- **Dismiss.** `POST /v1/tasks/{id}/dismiss` `{"never": "sender" | "kind" | null}` dismisses the task. It can be
  undone with `PATCH status open`. Fixture: `fixtures/rest/task_dismissed.json`.
- **Never from this sender / this kind.** For a task from an email, `never` also makes the user's rule:
  - The rule's sentence is returned in `rule`.
  - It is kept as a `source` claim in About you; forgetting it there removes the rule.
  - It dismisses the open email tasks it covers (`also_dismissed`).
  - The kind is the email's account notice (`security`, `storage`, `auto_renewal`, `developer`,
    `contact_check`), else its category.
  - A task not from an email gets 400 with `never`.
- **The quiet list.** `GET /v1/inbox` (fixture `fixtures/rest/inbox.json`) is the "From your email" list.
  - It has recent emails that became no task, newest first, at most 50.
  - Each has its subject, sender, `kind`, `why` it's not a task (in the user's language) and the triage's
    sentence (`detail`).
  - Spam and promotions aren't listed. Never on Today.

## The user's home
- `GET /v1/profile` has `home`: `{"place": {name, lat, lon} | null, "source": "owner" | "learned" | null,
  "confirmed": bool}` (fixture `fixtures/rest/profile.json`).
  - `place: null`: Pisa doesn't know where the user lives. Then nothing is scoped home or travel and no trip
    starts (`travelling` stays null).
  - `source: owner`: the user set it (always `confirmed`). `learned`: Pisa's guess from where the phone sleeps,
    only after 21 nights with most of them in one place; while `confirmed` is false, show it as a guess ("I
    think you live in …") with a confirm button.
- `PUT /v1/profile/home` `{"place_id"}` or `{"query"}` (exactly one; the user's words are looked up once)
  answers the new `home`. It replaces a learned one; the next night more than 50 km from it starts a trip.
  No single place for the words: 404 `not_found`.
- `DELETE /v1/profile/home` (204) forgets it; Pisa doesn't learn the same home again from the nights before.
- `POST /v1/profile/home/confirm` makes a learned home the user's to keep (Pisa never moves it); 404 when Pisa
  knows no home.
- By voice: "abito a …" / "I live in …" (`set_home`), and the first talk asks it first.

## Understanding
- **`understood`** (Pisa → device, fixtures `fixtures/valid/understood.json`, `understood_ask.json`): before Pisa's
  answer to an utterance, what Pisa understood the user meant. Sent only when something changed (a filler, a
  repair, a name) or Pisa asks a question; `reply_to` is the utterance.
  - `meant`: the request as Pisa understood it (the words that didn't change keep their accents, case and
    punctuation). `raw`: what the recogniser wrote.
  - `corrections: [{from, to, via}]`: each name Pisa fixed. `via`: `sound` or `spelling` (fixed by the rules;
    show `to` underlined in `meant`), or `model` (the small model decided; Pisa also says the name in its answer).
  - `ask`: one short question instead of a guess (two names alike, an hour, a day or a city that contradict).
    The `say` that follows is that question, and nothing else.
  - The user's "no, ho detto X" right after a fix keeps the pair (heard, meant) on the server for a real test
    set; their words then stand as said.
- **`GET /v1/speech/hints`** → `{"hints": ["…"] (at most 100), "updated_at"}` (fixture
  `fixtures/rest/speech_hints.json`): the recogniser's contextual strings, best first: today's plan and
  home, this week's events, venues and people, places visited in 30 days, contacts, names Pisa fixed lately.
  Never one common word, never a task's title. Pisa rebuilds them when its tables change and daily; fetch them
  daily and when the app starts.

## Priorities
- **Every task has a priority**: `priority` 1 high, 2 mid (the default), 3 low; `priority_reason`, Pisa's one line
  for it (from the email's triage, as data: never instructions); `priority_source`: `pisa`, or `owner` once the
  owner changed it.
- **Low is quiet**: never on Today, in the plan, in reminders or in the briefing.
- **The Low list.** `GET /v1/tasks?priority=low` (fixture `fixtures/rest/tasks_low.json`) answers the open low
  tasks and `emails`: the emails that became no task, as `GET /v1/inbox` (which is unchanged). By voice: "cosa
  c'è in bassa priorità?".
- **The user's change.** `PATCH /v1/tasks/{id}` `{"priority": 2}` raises one (it is planned again);
  `{"priority": 3}` lowers one. Either sets `priority_source: owner`, clears the reason, and is kept as a label for
  Pisa's learning. Dismiss stays `POST /v1/tasks/{id}/dismiss`.
- **From email.** An account notice (a sign-in alert, storage, a renewal that renews by itself, a developer notice,
  "check your details") becomes a low task, not nothing. The user's "never from this sender / kind" still means
  no task at all. A person writing is never low.

## Done by being there
- **By being there.** An open task with a place, or today's event with a place, planned or not, is closed when
  the user stays there 10 minutes: within the task's day (today for an undated or overdue task) or within the
  event's time ± 1 h. Pisa says it in one line, a `notify` kind `done` ("Fatto: BRAM'S Broodjes ✓"; fixtures
  `fixtures/valid/notify_done.json`, `notify_done_event.json`), with Undo for 24 h. No check-in asks about it.
- **What closes.** A task becomes `status: done`; a repeating one makes its next occurrence. An event, which Pisa
  never edits in Google, is marked attended in Pisa's data: `GET /v1/events` shows `attended_at`, and the plan shows
  it `done`.
- **Undo.** `POST /v1/tasks/{id}/undo-done` answers the task, open again. `POST /v1/events/{id}/undo-attended`
  answers the event, without `attended_at`. Both work within 24 h of Pisa closing it; after that they answer 409
  `undo_expired`. When Pisa didn't close it they answer 404 `not_found`. The same visit doesn't close it again that
  day.
- **By voice.** "Ci sono stato", "fatto", "l'ho comprato" close the item named, by its title or place, or the
  item just discussed. A task and today's event at the same place are one visit: both close, in one line (the
  `done` carries the task). Otherwise, when two match, Pisa asks one question, and the answer picks by its number
  or its title. Only the user's own words count: after Pisa read an email or a page in the same turn, Pisa asks
  first.
- **One event, one close.** An event in two calendars (a shared invite) is closed and undone as one, by its id.
- **Where it counts.** A stay linked to a known place counts only at that place (home or the café next door
  isn't the shop); a stay linked to no place counts within its reach. One stay closes one occurrence of a
  repeating task.
- **Sensitive places** (health, worship, politics) close the item and learn nothing. The notification
  names no place, and the record keeps no place.
- **Held in quiet mode**, a `done` goes out in the one summary when quiet ends, without its Undo button: the line
  says "annulla dall'app" ("undo it in the app"), and the endpoints work for 24 h.

## What triage keeps of an email
- The triage call Pisa already makes also writes a neutral gist (what it is, who, when, where, what is asked, the
  deadline, the other side's role) and the keys: order, booking, ticket and tracking numbers, amounts and dates.
  - Keys are copied verbatim: a key that isn't word for word in the email is dropped.
  - The rules find the ones they recognise too, including schema.org JSON-LD, so none is lost.
- Text hidden in the email (invisible HTML, zero-width characters) is removed before the model reads it. The
  body goes to the model datamarked, so the email's words can't pass for instructions or add fields to the gist.
- Both are kept with the email, in Pisa only (not in the REST API yet). The gist is deleted with the body after 30
  days; the keys stay.

## Rules by id, places nearby, continuation, the turn hint
- **`rule_id`.** `POST /v1/tasks/{id}/dismiss` with `never` answers `rule_id` beside `rule` (fixture
  `fixtures/rest/task_dismissed.json`); without `never` it is null. `DELETE /v1/rules/{id}` (204) undoes the rule,
  as forgetting it in About you does; the tasks it dismissed stay dismissed (each has its own undo); 404 for no
  such rule. The app no longer needs to match the rule's sentence.
- **Nearby without a query.** `GET /v1/places/search?near=<lat>,<lon>` (or `near=here`) with `q` omitted answers the
  places Pisa already knows nearest to that point, by distance (`distance_m`), at most 20 (`limit` 1–20, default
  20), in the same item shape. No provider is called (no cost); a place Pisa has never seen isn't there. With `q`
  the search is as before (2–200 characters, `limit` at most 10). Neither `q` nor `near`: 400.
- **Continuation** (fixture `fixtures/valid/utterance_continues.json`): an `utterance` may carry
  `continues: <the id of an earlier final utterance of this connection>`, when the user went on talking after
  the app closed the turn. Pisa cancels that utterance's reply if it is still running, drops that turn (the words
  and any partial reply) from the history, and answers the whole turn as one (`say.reply_to` is the new
  utterance). The app sends **the whole turn** in `text` (the earlier words first); if `text` doesn't start with
  the earlier words, Pisa puts them in front (an app that sends only the new words), never twice. Pisa remembers the last 5 final utterances of the connection; an unknown
  id is answered as the new words alone.
- **How they spoke: `utterance.voice`** (fixtures `fixtures/valid/utterance_voice.json`,
  `fixtures/invalid/utterance_voice_emotion.json`). An optional object of four optional closed enums, each
  measured **on the phone against their own rolling 30-day baseline**: `pace` (`slow` | `usual` | `fast`),
  `loudness` (`soft` | `usual` | `loud`), `gaps` (`few` | `usual` | `many`), `place` (`whisper` | `normal` |
  `noisy_place`). The baseline and the audio **never leave the phone** (never synced, never sent); the server
  receives only these values for the one turn and stores none of them. Observables only: no emotion field exists
  and any other key is invalid. Absent on old builds, on typed turns and for a field the phone could not measure;
  a server that gets none behaves as before. Pisa uses them only as one of two channels that must agree before it
  asks them a question they can wave away ("Ti sento di corsa: giornata pesante? O lascio stare."); a `whisper` or a
  `noisy_place` means people may be near them, and Pisa asks nothing about them then. The iOS half comes later.
- **Partials and the turn hint** (fixtures `fixtures/valid/hello_turn_hints.json`,
  `utterance_partial.json`, `turn_hint.json`). A hello with `"turn_hints": true` lets the app send the transcript
  so far as `utterance` with `final: false`: at most 2 a second, only while listening (Pisa answers at most one
  every 0.5 s and ignores the rest; without `turn_hints` it ignores every partial). Pisa answers within 150 ms with
  `turn_hint {reply_to: <the partial's id>, p_end, wait_ms}`: `wait_ms` is how long to wait at this pause before
  closing the turn, `p_end` how likely the user has finished. Today both come from **the turn rules** below,
  the same the app runs itself, so the hint agrees with the phone; a model may replace the rules on the server
  later (not before the user's real numbers say one helps), so the app follows `wait_ms`, never its own
  reading of `p_end`. A partial never starts a reply, and Pisa never stores or logs its words (only their length).
  The final utterance is sent as before; a later partial's hint replaces an earlier one.
- **The turn rules** (`turn-rules.v1.json`; the word lists come from a measured turn detector). One algorithm, the
  same on the phone and on the server; the cases in `fixtures/turn-rules/cases.json` are its tests (both sides
  must give each text its `kind`). For the utterance's language (`it` for `it-IT`, `en` for anything else):
  1. Read the typographic apostrophe `’` as `'`, and trim the text. Nothing said yet → `unfinished`.
  2. The text ends with one of `unfinished_endings` (`, : ; - – — … ... '`) → `unfinished`.
  3. The words are the lowercase runs of letters, digits and apostrophes (`[\w']+`, so "dell'opera" is one
     word and "l'" is a word). The last word is in `unfinished_last` → `unfinished`.
  4. The text ends with `?` → `complete`.
  5. Skip the leading words that are in `openers` ("ok", "allora", "senti", "hey"…). The first word left is in
     `question_first` and at least `question_min_words` (3) words are left, or it is in `command_first` and at
     least `command_min_words` (2) are left → `complete`.
  6. Otherwise `other`.

  `waits_ms` gives each kind's wait: **3000 / 1200 / 1800** (`complete` is 1.2 s), and
  `p_end` its score (0 / 1 / 0.5) in `turn_hint`. CORE mirrors the table in `pisa/domain/turn.py` (a test fails if
  the two differ). A change to the words or the numbers is a change of this file, in a CORE batch, with the app
  updated in the same release. `version` is 2 (the whole algorithm).

## Pisa acts, Pisa talks like a person
- **`action_done`** (Pisa → device; fixture `fixtures/valid/action_done.json`): `{approval_id, kind,
  summary, result, undo_until}`. Pisa changed something on the user's own words with no question (the time, the
  title, the cancel verb said by the user, on an event they organise with nobody else invited), or right after
  their spoken yes. The app shows `result` with Undo until `undo_until` (24 h for calendar kinds; absent or null
  when it can't be undone); Undo is `POST /v1/approvals/{approval_id}/undo` as before. No `approval_request`
  comes for these: the change is already done.
- **Approvals: `origin`, 24 hours, replaced questions.** `GET /v1/approvals?status=all` carries `origin`: `words` (done
  at once on the user's words), `proposal` (Pisa's proposal, one yes: other attendees are notified, or an option
  of Pisa's moved a real event), `held` (not in the user's words, or the turn read outside text: one yes).
  A calendar change can be undone for 24 hours (`undo_until`); `approval_request.undo_minutes` stays at most 60
  on the wire for older apps (they refuse a larger value), so the app's own Undo may end sooner than the
  server's. An email's question replaced by a newer read-back reads `expired` (its `result` says it was
  replaced); a decision on it answers 409 `approval_decided`. The list is the app's
  "Changes" screen: pending first, then what was done in the last 24 h.
- **A spoken yes or no is a normal utterance**. The device sends "sì", "ok", "vai", "no", "lascia
  stare" as an `utterance`; when Pisa asked an approval in that device's previous turn (within its 10 minutes), Pisa
  decides it before any model call and answers with the outcome ("Ho spostato «Cena» a oggi alle 21. Se vuoi lo
  annullo.") or "Ok, lascio com'è.". "Sì, senza avvisarli" approves without notifying the attendees. Two
  questions pending: "Quale? 1) …; 2) …" and nothing is approved. Face ID needed: "Serve Face ID: è sul
  telefono." (the card is already there). The phone's reply window and `approval_response` stay as other ways
  to give the same answer. A marked "no, alle 9" right after Pisa said a time moves it to that time instead.
- **Holding phrases** (`holding-phrases.v1.json`, fixture `fixtures/valid/say_holding.json`). When
  Pisa's answer has no first sentence ~700 ms after the utterance arrived, the stream starts with a `say` carrying
  `holding: true` and `holding_id` (one of the file's ids; it/en, at most 4 words, `use` any or lookup). The app
  plays that phrase from its cache (synthesised once per voice) and gets no `say_audio` for it; the answer
  follows in the same stream (same `stream_id`, next `seq`). After 6 s of silence on long work Pisa sends one
  ordinary `say` naming the step ("Sto leggendo le recensioni."). A voice turn still silent 8 s
  after the utterance arrived (no answer sentence, no step line) gets one more holding `say`, `use` `slow`
  ("Ancora un momento…"); the turn is not cut, its answer follows in the same stream. Never on a typed turn.
  The app plays it like any holding phrase (an id it doesn't know yet: the delta, by the iPhone's voice, then
  cached). Pisa never says the same holding phrase twice in a row to a device. CORE mirrors the list in `pisa/domain/holding.py` (a test fails if the two differ); a change
  is a change of this file, with the app's cache updated in the same release.
- **When Pisa speaks first: `notify.urgency`** (fixtures `notify_now.json`,
  `notify_breakpoint.json`). `now` (leave now, an event within 10 minutes, an urgent message or an alert): the
  app says it right after the current sentence; its text opens with "Scusa, un attimo:". `breakpoint`
  (everything else): the app queues it for the end of the exchange (never drops it). Pisa itself holds a
  `breakpoint` item while the user is in an exchange with Pisa (it goes when the reply ends) and during a meeting
  (an event with other people invited, or a meeting word in its title; not declined; at most 4 hours long; it
  goes when the meeting ends, held 2 hours at most, and never past the start of the event a reminder is
  about). The user's "dopo" / "non ora" / "later" right after it: Pisa answers "Ok, dopo." (no model call) and
  sends it again at the next breakpoint, at most once. A held or put-off item is stored, not kept by the
  socket: if the app disconnects first, it comes on the next connection or by push. In a call, the
  app holds it (Pisa doesn't know about calls). `notify` without `urgency` (an older server) is `breakpoint`.
- **Deploying the acting and talking changes**: migrations 0039 and 0040 run with the restart of every Pisa process (the deploy
  does both together; 0039 drops `pending_task_deletions`, which a process started before it still uses).
  0039 and 0040 have no down path: going back means restoring the database backup the deploy takes first.

## Pisa's body, sentence ends
- **`presence.state`** (device → Pisa, additive; fixture `fixtures/valid/presence_state.json`). Every `presence`
  may carry the device's whole current state: `{"foreground": bool, "locked": bool, "route": "speaker" |
  "receiver" | "wired" | "earbuds" | "bluetooth" | "car" | "glasses" | "airplay" | "other", "glasses_worn": bool,
  "companion": bool}` (each field optional). A lock or audio-route change re-sends the current
  `app_foreground` / `app_background` with the new state (a repeated event triggers no briefing). No `hello`
  field. Pisa keeps the latest state per device (reset when the socket closes) and gives the model one line each
  turn, "Pisa's body now", so it knows whether the user can see the phone or only hear Pisa. A `presence` without
  `state` (an older app) keeps the state Pisa already had; each field counts for 10 minutes after the
  app last sent it, then it is unknown (re-send the state on a change, as above).
- **`say_audio.sentence_end`** (Pisa → device, additive; fixture `fixtures/valid/say_audio_sentence_end.json`).
  `true` on the chunk whose audio ends a sentence of the reply or notify. When Pisa knows the sentence ended as its
  last audio goes out, the mark rides on that chunk; otherwise an **empty** chunk (`data: ""`, `final: false`)
  marks the end right after it (Pisa never delays audio to mark it). The final chunk needs no mark. Absent means
  no boundary is promised (today's behaviour). The app may interject a `now` item in Pisa's voice at the next
  marked chunk instead of at its own sentence guess.
- **Changes: "perché"** (REST, additive). `GET /v1/approvals` items carry `why: {purpose, provenance} | null`.
  `purpose` is Pisa's one line in the user's language (what the user gets: "così arrivi dopo la palestra"),
  null when Pisa gave none; `provenance` is set by Pisa's code, never by the model: `owner_words` (from the user's
  own words), `pisa_proposal` (Pisa's idea, offered for a yes), `outside_text` (the turn had read an email, a page or
  an invite), `pisa_initiative` (Pisa did it unasked). `why` is null for a change made by a tap on a card, or on an older
  server. The app shows it as "perché: … · dalle tue parole / proposta di Pisa / da un testo esterno / di sua
  iniziativa".
- **Pisa's own proposals act with Undo**. Choosing one of Pisa's own plan options, even with
  a vague "va bene", now moves the user's own event at once (`origin: proposal`, `status: done`, Undo for 24
  hours, `why.provenance: pisa_initiative` when the user's words didn't name the option). Still one yes: anyone
  else invited that the option didn't name, a turn that read outside text, sending, deleting for good, money.
- **The budget guard**. Past 70% of the day's AI cap the conversation answers with its slot's fallback
  model for the rest of the day; the first such reply ends with "Per oggi uso il modello più economico." (once a
  day). No device change.
- **Deploying the body line**: migration 0041 adds columns and tables only; it runs with the restart of every Pisa
  process. One API worker only: a second API process on the same database refuses to start (`uvicorn --workers
  1`).

## The interface as tools
Pisa's model decides what to show, when, and in what focus, and says why. All of it is additive to v1: **a device
whose `hello` has no `capabilities.present` gets today's `card` messages** and nothing below.

- **`hello.capabilities.present`** (device → Pisa; fixture `hello_present`): `{"kinds": [...], "focus": bool}`.
  `kinds` from `plan`, `place`, `places`, `compare`, `item`, `changes`, `setting`, `note`, `choice`; `focus`:
  the app honours `take` / `offer` / `quiet`. Pisa sends `present` only for a listed kind; for any other the
  model is told the device can't show it (`unsupported`) and says it in words.
- **`present`** (Pisa → device; fixtures `present_*`). `present_id`, `reply_to` (the utterance, or null when
  unasked), `object` {`object_id`, `kind`, `revision`, and **either** `ref` **or** `data`}, `purpose` (≤ 120,
  the user's language: what they get from seeing it), `focus`, `highlight`?, `lang`, `expires_at`?.
  - `ref` is for what Pisa holds, read by the app through the REST it already uses, so the object is the truth:
    - `plan` `{date, options_id}` → `GET /v1/plan?date=…` (and the options when `options_id` is set: proposal
      vs now);
    - `place` `{place_id, detail: card|full}` → `GET /v1/places/{id}` (`full`: the details sheet);
    - `item` `{ref}` with `task:<id>`, `event:<calendar>/<id>`, `email:<id>`, `draft:<id>`, `approval:<id>`,
      `claim:<id>`, `thing:<id>` (a draft or approval shows the full text about to be approved);
    - `changes` `{since}` → `GET /v1/approvals?status=all` (with each `why`);
    - `setting` `{section}` → the Settings page of that section, pulsing `highlight` (a setting's key).
  - `data` is only what the turn computed: `places` (as the `places` card), `compare` (`criteria` and 2–4
    `rows`, each row's `cells` in the criteria's order, each `{status: verified|likely|unknown|no, value,
    source}`; a cell Pisa didn't check is `unknown`), `note` (`title`, 1–6 `lines`, `kind` info|warning,
    `sources`? for a web fact), `choice` (`question`, 2–5 `options` `{id: o1…o5, label ≤ 40, detail?}`).
  - `object_id` is `<kind or record>:<id>` (`plan:2026-10-31`, `places:<uuid>`, `draft:<uuid>`…); showing the
    same object again keeps its id with a higher `revision`.
  - **Showing never changes anything** (no replan, no setting, no approval).
- **`presented`** (device → Pisa; fixtures `presented*`): the acknowledgment, sent at once with the same
  `present_id`: `status` `shown` (in front) / `queued` (app in the background: there when the user opens Pisa) /
  `deferred` (the user is reading or editing: an unseen dot) / `unavailable` (no screen on the surface in use,
  e.g. glasses only) / `unsupported`; `surface` `phone` / `glasses_display` / `none`; `reason`?. Pisa waits **800
  ms** at most; after that the tool goes on as `sent_unconfirmed` and a late `presented` is only logged. With
  `shown`, `queued` or `deferred` Pisa doesn't call the model again (its sentence already went out); with
  `unavailable` or `unsupported` the model says it in words.
- **`object_event`** (device → Pisa; fixtures `object_event_*`): `object_id`, `event` `seen` (on screen ≥ 2 s with
  the app in front) / `expanded` / `kept` / `closed` / `chosen` (with `option_id`, and only then) / `acted`
  (with `action`: `go`, `call`, `plan_it`, `undo`, `open`, `details`, and only then). Ids only: never gaze,
  never content. Pisa records it as the outcome of the show in its decision log. **`chosen` is the user's own
  message**: Pisa answers it as if the user had said "[tap] o2: <label>" (grounded as the user's own act); a
  pending approval is still decided only by `approval_response`.
- **`focus`** (Pisa → device; fixture `focus`): `present_id`, `object_id`, `highlight`?: bring a shown object
  back in front or point at a part of it; acknowledged by `presented`.
- **`close`** (Pisa → device; fixture `close`): `object_id`: off the screen (it stays in the history); no
  acknowledgment.
- **Unprompted voice: `notify.voice`** (Pisa → device, additive; fixture `notify_voice_off`). Pisa speaks unasked at
  most 3 times an hour and 12 a day (the user's local day), beyond the time-critical floors (leave now, an
  event about to start), which always speak. Past that a `notify` comes with `"voice": false`: show it (the
  notice, or a banner), don't speak it, even in companion mode or with the glasses on; no `say_audio` follows,
  and a backgrounded app in companion mode gets the push banner too. Absent: today's behaviour. Pisa counts what
  was spoken (a notify that reached a device that speaks it), never what was only queued or shown.
- **Deploying the interface as tools**: migration 0042 drops `conversations.summary` (never read); it runs with the restart of
  every Pisa process.

## The battery numbers
- **`POST /v1/devices/me/diagnostics` with `kind: "metrics"`** (REST, additive; fixture
  `fixtures/rest/diagnostics_metrics.json`): one day of the phone's own numbers, MetricKit's `MXMetricPayload`
  mapped by the app. Body: `{"kind": "metrics", "app_version", "os_version", "payload": {"day": "YYYY-MM-DD",
  "foreground_s", "background_audio_s", "background_location_s", "location_s": {"best", "ten_m", "hundred_m",
  "km", "three_km"}, "cpu_s", "gpu_s", "cell_up_kb", "cell_down_kb", "wifi_up_kb", "wifi_down_kb",
  "companion": [{"start", "minutes", "battery_pct_per_h"}]}}`. Each number optional, ≥ 0 (seconds ≤ 7 days);
  `companion` ≤ 48 sessions, `start` a date-time with its offset. **Numbers only: any other key is dropped**;
  a wrong type or a negative number is refused (400 `validation_failed`). One a day per device (a second upload
  for the same day replaces it); kept 90 days. Answer 204. The operator's usage report shows them by day.

## The tasks, things v1, the reads the phone needs
All additive; no new approval kind (the app decodes them strictly), no new WebSocket message.
- **Tasks in bulk** (Today's edit mode): `POST /v1/tasks/bulk` `{"ids": [uuid, 1-500], "action": "done" |
  "dismiss" | "delete"}` → `{"bulk_id", "count", "undo_until"}`. Only the open tasks among `ids` change (each as
  if changed one by one: a repeating task's next one, its labels); `count` says how many. One Undo for the set:
  `POST /v1/tasks/bulk/{bulk_id}/undo` → `{"bulk_id", "count"}`, within 24 h, once (404 `not_found` after, or a
  second time). Pisa's voice path (`tasks_bulk`) says the count and acts on one yes, never on more than it said; its
  `source` filter takes `voice`, `email`, `manual` or `calendar` (the reminders the people list made from their
  people's dates). Items they name one by one ("latte, pane e uova presi", filter `named`, each title
  in their words this turn, no outside text, at most 8) are done at once, with the same Undo.
- **App event `notification_dismissed`** (on the diagnostics upload, `app_events`): `{"event":
  "notification_dismissed", "at", "notification_id": uuid}`, the id from the push's `data.notification_id`. The
  owner swiped the notice away: Pisa's `dismissed` outcome for what it said (it says less of that kind). Without a
  valid `notification_id` the event is dropped.
- **Plan options by id**: `GET /v1/plan/options/{options_id}` → contract N's options exactly as `POST
  /v1/plan/options` made them, while they last (15 minutes, gone once one is applied: 404 `options_expired`). For
  a `plan` object whose `ref.options_id` is set.
- **An email by id**: `GET /v1/emails/{id}` → `{"id", "subject", "sender", "sender_address", "sent_at", "gist",
  "task_id", "kind", "why", "detail"}`: who, when, Pisa's one line on it (`gist`), the task it became (`task_id`) or
  why it is none (`kind`/`why`, as in the inbox). Never its body; 404 for spam, promotions or an unknown id. For
  an `item` object `email:<id>`.
- **A draft by id**: `GET /v1/drafts/{id}` → `{"id", "subject", "to", "cc", "outsiders", "body", "version",
  "updated_at", "sent_at", "approval_id"}`: the whole text the user approves (`outsiders`: recipients not in the
  thread, to mark). 404 when unknown (kept 90 days). For an `item` object `draft:<id>`; `approval:<id>` stays
  `GET /v1/approvals/{id}`.
- **A day with no plan stored**: a `plan` object's `ref` carries `"agenda": true` (sent only when true; fixture
  `fixtures/valid/present_plan_agenda.json`): show that day's agenda (`GET /v1/agenda?from=<date>&days=1`), never
  just its date. Today never has it (the phone's `GET /v1/plan` makes today's).
- **Tasks that move on** (no new field): a task 3 days overdue leaves the plan and the briefing, and Pisa asks once
  "è ancora da fare?" (a check-in, kind `confirm`: `not_done` keeps it); a week with no answer and Pisa dismisses it
  with its reason in Changes (restore it as any dismissed task: `PATCH /v1/tasks/{id}` `status: "open"`). A
  reply in an email thread that settles its task closes it with the Undo (`POST /v1/tasks/{id}/undo-done`).

## The changes feed
- **`GET /v1/changes`** (`?since=` RFC 3339, default 7 days ago) → `{"changes": [...]}`, newest first, 50 at most.
  Each: `{"id", "kind", "at", "title", "why", "by", "task_id", "event_id", "approval_id", "undo"}`. `kind`:
  `task_dismissed` (aged out), `task_done` / `event_done` (closed because the user was there, said so, or a
  reply in the thread said it), `calendar` / `plan` (an action approved, or Pisa's own proposal applied),
  `tasks_bulk`. `why` is Pisa's reason in the user's language; `by` is `owner` (their words or yes) or `pisa` (Pisa's own
  initiative). `undo` is the item's own REST call while it lasts: `{"method": "POST" | "PATCH", "path", "body",
  "until"}` (`PATCH /v1/tasks/{id}` `{"status": "open"}` for a dismissed task, `POST …/undo-done`,
  `…/undo-attended`, `/v1/approvals/{id}/undo`, `/v1/tasks/bulk/{id}/undo`), or `null` once it can't be undone.
  Read-only, additive: no new approval kind.
- **`ref.agenda`** is still not sent: build 31 would drop the whole `present`. The server turns it on
  (`present_tools.SEND_PLAN_AGENDA`) once the app decodes it.
- No other change on the wire: the conversation's lighter tool set and the voice's first-audio timing are
  server-side only.

## Pisa talks through a live voice model (settled in CONV-1b)
`settings.conversation` (`GET`/`PUT /v1/settings`): `{"mode": "natural" | "economy", "talker": "xai" | "gemini",
"voices": {"xai": "eve", "gemini": "Aoede"}}`, defaults as shown, exactly the server's shape (one voice per
talker). The phone saves it on submit, never per keystroke. `natural` is a conversation with the talker (Grok
Voice by xAI, or Gemini Live), Pisa's brain whispering to it; `economy` is today's path (the phone's recognizer,
`utterance`, Pisa's sentences, `say_audio`), unchanged.

- **`POST /v1/talk/sessions`** `{"sdp_offer", "trigger", "lang": "it-IT" | "en-US", "first_text"?}` → 201
  `{"session_id", "sdp_answer", "ice_servers"}`.
  - The offer has one audio track (the microphone, Opus, the phone's voice processing on) and one data channel
    labelled `"pisa"`. The answer carries every ICE candidate (no trickle).
  - `trigger`: `press` for any press (the presence press, AirPods), `wake` only for "Hey Pisa", `intent` for App
    Intents, `notice` for Pisa's own start.
  - `first_text` (optional, at most 2000 characters): the words said in one breath with the wake phrase,
    recognised on the phone. The server takes them as the user's first final words: Pisa's brain answers them, and
    they come back as the first `talk.transcript` with `final: true`.
  - `ice_servers` is `[]`: the network is the tailnet (the server's UDP on `tailscale0`, no TURN). The phone
    gathers with no ICE servers; the field stays for later.
  - One session per device: a new one ends the previous (`talk.ended` `owner`).
- **Refusals the phone remembers** (no Live Activity and no logged session for an attempt that never opened):
  - 409 `talk_economy` (the setting is `economy`, or today's spend reached half a cap) → economy until local
    midnight or a settings change. The error carries `economy_reason`: `setting` (Settings →
    Conversation), `day_guard` (half the day's AI cap) or `month_guard` (half the month's). Fixture
    `fixtures/rest/talk_economy.json`;
  - 402 `budget_exceeded` (a cap is reached) → economy until local midnight;
  - 409 `provider_key_missing` (the talker's provider has no key; `provider` names it, `google` for
    Gemini) and 502 `talker_unavailable` (the talker didn't open) → economy for 10 minutes;
  - 404 (an older server without the endpoint) → economy for 10 minutes;
  - 400 `validation_failed` (an SDP Pisa can't use, or a field out of range).
- **`DELETE /v1/talk/sessions/{id}`** `?reason=` (optional) → 204; 404 `not_found` when it isn't this
  device's live session. The phone keeps `session_id` from the POST and DELETEs on every end, ignoring a 404.
  `reason` is the same as `talk.end`'s.
- **Data channel `"pisa"`**, JSON text messages, each `{"type", ...}`:
  - server → phone:
    - `{"type": "talk.state", "state": "listening" | "thinking" | "speaking" | "idle"}`: the sequence is whatever
      the server sends (typically `listening` → `thinking` → `listening` → `speaking` → `listening`; `listening`
      on a barge-in; `idle` just before `talk.ended`). The phone never builds an exchange from state edges.
    - `{"type": "talk.transcript", "text", "final"}`: the user's words for the current line, as the talker hears
      them; `text` is the whole line so far. The server sends **one final per utterance**: a talker's repeated
      final is dropped. A final may still grow the previous one (the talker's transcript went on: "Ciao" →
      "Ciao, mi suggerisci un film?") before Pisa answered it; the phone then replaces that exchange's words.
    - `{"type": "talk.said", "text", "final"}`: Pisa's words as spoken. `text` is Pisa's words so far for the current
      line (it grows): the phone *replaces* the line on each partial and commits it on `final: true`, then starts a
      new line.
    - `{"type": "talk.ended", "reason": "idle" | "owner" | "budget" | "error" | "fallback" | "route"}`: the last
      message. `route`: a gated talk's route changed (or their voice profile was deleted): reopen it at
      once on the new route, with no error shown.
      `fallback`: the talker dropped, carry on in economy; `error`: the server or the peer failed.
  - phone → server:
    - `{"type": "talk.stop"}`: barge-in by touch ("tap to stop"). Pisa stops speaking at once **and** the brain's
      pending answer for that turn is dropped: what was already done stays done (its `done` is said if the user
      asks). The session stays.
    - `{"type": "talk.end", "reason"?}`: the phone ends it; the same as the DELETE. `reason` (optional)
      says why: `press` (the user pressed to end), `call` (a phone call took the audio), `route` (the audio
      route changed: AirPods out, CarPlay), `background` (the app went to the background), `idle` (the phone's own
      silence timer), `error` (the phone's audio or peer failed). The server keeps it in the session's timeline (who
      closed it, and why); any other value is ignored, and a missing one is stored as none.
  - The phone builds an exchange from the user's final `talk.transcript` and Pisa's final `talk.said`: one
    end-of-turn signal per owner final.
  - Messages sent before the channel opens are queued and sent when it does. Unknown types are ignored.
- **Teardown:** the server ends the session when the peer is `failed` or `closed` (the phone vanished) and after
  30 s with nobody speaking (`idle`), besides the DELETE and `talk.end`.
- The phone's WebSocket stays open and carries cards and approvals; what Pisa shows while it talks
  comes on the data channel (`talk.present`), never also on the WebSocket. A spoken yes or no to Pisa's question is decided by the server's code only right after Pisa said the question
  aloud (10 s) and when the talker took it as said to Pisa; otherwise the model reads it and asks again.
- **The `NOTICE` category:** every Pisa push that has no category of its own (`CHECK_IN_*`, `DONE`, `APPROVAL*`)
  carries `NOTICE`, the app's category with a dismiss action, so a dismissal is reported.

## The body 
An older app ignores the new message types and fields. An older server refuses `route` and `ask_id` in
`POST /v1/talk/sessions` (400 `validation_failed`): the phone asks again without them. From this version on, unknown
fields of that POST are ignored and an unknown `route` is none (`trigger` stays strict). Data-channel messages
have no envelope (`{"type", ...}` as in the talk channel); fixtures `fixtures/rest/talk_channel_conv3.json` and
`fixtures/rest/talk_present_*.json`. `talk.ask_to_open` is a WebSocket message with the usual envelope (in the
schema; fixture `fixtures/valid/talk_ask_to_open.json`).

- **Words Pisa set aside** (server → phone): a final Pisa drops comes as `{"type": "talk.transcript", "text",
  "final": true, "ignored": "noise" | "echo"}`. `noise`: nothing in it to answer (a cough, "A", "AJ"); `echo`: Pisa's
  own voice heard back (most of Pisa's recent line in Pisa's order, or a cut line's words just after the cut).
  **No card, no end of turn**: the phone clears the user's live words and waits; Pisa doesn't answer it. A final
  without `ignored` is the user's, as before.
- **Showing while talking**. While a talk is open, everything Pisa shows goes on the data channel, **never also
  on the WebSocket**; with no talk open, the WebSocket's `present` as before.
  - server → phone `{"type": "talk.present", "id", "object", "purpose"?, "focus"?}`: `id` is this show's id (a
    uuid); `object` is exactly the WebSocket `present`'s `object` (`object_id`, `kind`, `revision`, and `ref` or
    `data`: plan, place, places, compare, item, changes, setting, note, choice); `purpose` (≤ 120, the user's
    language) and `focus` (`take` | `offer` | `quiet`) as in `present`. The same `object_id` again with a higher
    `revision` replaces it. Pisa only sends the kinds the phone's `hello.capabilities.present.kinds` lists (all of
    them with no live WebSocket). There is no acknowledgment: the talk's phone is the screen.
  - server → phone `{"type": "talk.dismiss", "id"}`: that show leaves the screen (it stays in the history).
    Everything shown in a talk leaves when the talk ends.
  - phone → server `{"type": "talk.choose", "id", "choice"}`: the user's tap on a shown object. `choice` is a
    `choice` object's option (`o1`…`o5`), or `"yes"` / `"no"` on an `item` whose `ref` is `approval:<id>`. **A tap
    is the user's own act**: an option is the user's next words ("[tap] o2: <label>"), which come back
    as the user's final `talk.transcript` (the phone's exchange), and Pisa's voice answers it; an approval's yes or no is decided by code at once (auth `tap`: never by text Pisa read), and Pisa says
    what happened. An approval that needs Face ID is never approved by a tap: Pisa says so and the card stays on the
    phone. A tap on a choice Pisa showed before the talk (its `object_id` as `id`) is that choice's option, when
    it is still open on this device. A tap on something not shown, dismissed or replaced by a newer revision,
    already answered, or expired answers nothing, and Pisa says it is no longer open.
  - Pisa's voice gives one line for what it shows ("Te lo mostro."), never the whole object read aloud.
- **The route** (phone → server): `{"type": "talk.route", "route": "speaker" | "receiver" | "airpods" |
  "headphones" | "car" | "glasses"}` when the channel opens and on every change; `POST /v1/talk/sessions` takes
  the same `route` (optional) for the start. **A route change never ends the session.** `talk.end` with reason
  `route` only when the user's audio is gone (no route left) and didn't come back within 10 s. What Pisa does with
  it: on the `speaker` Pisa's own voice can't cut it off (a lower start-of-speech sensitivity on the talker); on
  `airpods`, `glasses` and `car` Pisa speaks shorter (one sentence, the rest on the screen); the route is in
  the session's timeline and each turn's measures.
- **Pisa speaks first** (server → phone, WebSocket): `{"v": 1, "id", "ts", "type": "talk.ask_to_open", "why":
  "leave_now" | "reply_waiting" | "approval_lapsing" | "check_in", "line", "lang", "notification_id"?}`. Sent
  only with no talk open on the device and a recent route of `airpods`, `glasses` or `car` (presence: earbuds
  connected or glasses on; or the last talk's route in the last 30 minutes, its latest `talk.route` counted),
  outside quiet hours and inside the budget (natural mode allowed now), at most 4 an hour and never twice for
  the same item. The phone opens a talk with `trigger: "proactive"` and `ask_id` (the message's `id`), or,
  when it can't (in a call, the audio taken, the app suspended), shows `line` as a time-sensitive notification
  whose action opens that talk. A talk opened for it within 10 minutes starts with Pisa's `line`, said first, not
  a question; later it is an ordinary talk. The item's own `notify` still comes, with `voice: false` (the talk
  says it); `notification_id` is that notify's id, so the phone shows one alert, not two. In an open talk the
  same items are said by Pisa's voice in the talk itself: `leave_now` at once (after the current sentence), the
  rest at the next pause. `POST /v1/talk/sessions` takes `ask_id` (optional, a uuid); an unknown, used or late
  `ask_id` opens an ordinary talk. `trigger` gains `proactive` and `glasses` (the glasses' capture button or
  "Hey Meta, start Pisa").

## The task pool, the sheet Pisa composes, Pisa speaks first with Pisa closed

The server side came first; the app builds to it.

- **The task pool.** Pisa never places a task at a time by itself. A plan's task item has a time only when the
  owner gave one: their own time, an email's delivery window, or "Plan it" (they asked Pisa to fit it in). Such an item
  carries `scheduled_by`: `owner` | `email`. Every other open task is in the plan's new `todo`: the few that matter
  today, by deadline then priority, never a low one, each
  `{task_id, title, due_at?, deadline_source?: owner|email|web|pisa, priority, why?}`. `due_at` is the deadline (a
  day, or a time). Its source is where Pisa found it: the user's words, an email (a renewal set a few days before),
  a page Pisa read (an exhibition's last day), or Pisa's own estimate.
  - `GET /v1/tasks` (open) returns the whole pool in that order. Each task carries `deadline_source` (null with no
    deadline).
  - `GET /v1/profile/rules`: Pisa's own rules about the user (hard limits, the email rules), each with its
    provenance, evidence and `created_at`. Forget one with `DELETE /v1/profile/claims/{id}`.
  - Fixtures: `fixtures/rest/plan_todo.json`, `fixtures/rest/tasks_pool.json`.
- **A sheet Pisa composes.** A `present` object of kind `sheet` (id `sheet:<uuid>`), shown like any object, in a
  talk (on the talk's channel) and outside it: `data: {title, blocks, sources?}`. The blocks, in order (1–12):
  - `{type: "text", text}`;
  - `{type: "image", url, alt, credit?}`: https, only from the pages Pisa read this turn (their own preview images)
    or a place's photos, at most 6;
  - `{type: "link", url, title, host}`: https, a page Pisa read;
  - `{type: "list", items[1–8]}`;
  - `{type: "fact", label, value}`.

  The phone renders each block with its own fixed template (fonts, spacing, boxes), never a page's HTML. It
  answers `presented` as for any object; Pisa says something is on the phone only once the phone said `shown`
 . The WebSocket schema lists the kind (`object_kind`, `object_id` `sheet:`); fixture
  `fixtures/valid/present_sheet.json`.
- **`hello.capabilities.present.kinds`**: any string. Pisa keeps the kinds it knows
  and logs the rest, like the unknown `route`: a newer app's hello never fails on an older Pisa.
- **Pisa speaks first with Pisa closed.** With no live WebSocket, when the speak-first rails pass (quiet hours, low is quiet, the
  budget, something worn or a recent route, 4 an hour, never twice per item), the notice's APNs push is the ask:
  time-sensitive (`interruption-level`), category `TALK_ASK`, alert = Pisa's line, and
  `userInfo: {kind, notification_id, ask_id, why}`. "Rispondi" opens a talk with `ask_id` (`POST
  /v1/talk/sessions`, trigger `proactive`), and the talk starts with Pisa's line. Any other action, or none, leaves
  the notice as it is.

## Pisa's own views

The user: "Pisa must be able to write its own tools and screen schemas, starting from a template." Pisa composes
a sheet from fixed block templates, and on the user's words keeps one as a **view**: a named recipe they ask for
again later ("mostrami il meteo della settimana", "salvala come meteo", then "meteo"). A view is data, never code.

- **More block templates** in a `sheet` (`present`, kind `sheet`, as any sheet), next to text, image, link, list and
  fact:
  - `{type: "section", title}`: a heading for a part of the sheet;
  - `{type: "table", columns[1–6], rows[1–20]}`: each row as long as the columns; a cell is text (≤80) or a
    number;
  - `{type: "stat", label, value, unit?, trend?: "up"|"down"|"flat"}`;
  - `{type: "chips", items[1–12]}`;
  - `{type: "divider"}`.

  Limits: 24 blocks per sheet; a text block ≤ 600 characters. The phone skips a block type it doesn't know, so an
  older phone shows the rest. Fixture: `fixtures/valid/present_sheet_k13.json` (`present_sheet.json`).
- **A view, shown again**: Pisa runs the view's reads now and presents a `sheet` like any other. Nothing new on the
  socket: the phone can't tell a view's sheet from a composed one (the title is the view's name).
- **REST, Pisa's views in Settings ("Viste di Pisa")**:
  - `GET /v1/views` → `{views: [{id, name, description, provenance: "said", reads, blocks, created_at,
    updated_at, shown_at}]}`, newest first, at most 50.
  - `DELETE /v1/views/{id}` → 204 (404 `not_found` when it isn't there). A view is saved only by the user's
    words in a talk; the app lists and deletes.
  - `GET /v1/views/report[?week=YYYY-Www]` → `{week, sheet}` (additive, the user loop): Pisa's built-in view
    "report", never listed in `GET /v1/views` and never deletable. The week's talk report as a view sheet, made by
    code with no model: first words p50/p95 by route, the clean-turn rate, the week's AI spend per talk turn
    against the daily cap and its 50% economy switch, and the replay's latest pass^3. Numbers only, never their
    words. The latest week by default; 404 `not_found` before the first report. "Mostrami il report" in a talk
    shows the same sheet, and the Sunday card's sheet carries it as one text line.
- **What a view is** (server side; the phone never sees a recipe): `reads`, Pisa's own read-only tools from an
  allowlist (the time, the agenda, tasks, the plan, the weather, a map search, a web search, a page, an email
  search: never a write, a send or a profile write), and `blocks` whose strings may hold placeholders
  `{{n.path}}` and one `{type: "each", from, block}` repeater (≤ 20). Values are filled as text, never markup;
  a missing value drops its block, never the sheet; images and links only from what the run read.

## Rituals and place notes

The server side came first; the app builds to it. Every push Pisa sends for these obeys the rails: quiet hours and "basta notifiche" (quiet
until) send nothing at all (never a ritual held for the morning), the voice rate, the push as for any notice.

- **Rituals.** `GET /v1/rituals` → `{ghost_run: {on, at}, one_thing: {on}, closing: {on, at}, week_poster:
  {on}, something_different: {on, per_week}, errands: {on}, walk_recap: {on}, life_map: {on}, stanotte: {on}}` (`at` is `HH:MM`, the user's local time;
  `per_week` 0–7). Defaults: Ghost Run on at 22:00, one thing on, closing on at 21:30, the poster on (Sunday
  19:00), something different on, 2 a week, errand combos on ("spegni le commissioni"). `PUT /v1/rituals` takes any part of it (the rest stays) and returns the whole; a wrong
  field is 400 `validation_failed` naming it. By voice: "spegni il Ghost Run" (Pisa's `set_ritual`, only on the
  user's own words naming the ritual).
  - **A ritual's notice** is a `notify` of kind `info` with two new optional fields: `ritual` (`ghost_run` |
    `closing` | `week_poster` | `something_different`) and `sheet_id` (`sheet:<uuid>`, absent for something
    different, which is a question in its text). Its APNs push carries the same in `userInfo` (`ritual`,
    `sheet_id`, category `NOTICE`). An older app ignores both and shows the text.
  - **The sheet**: `GET /v1/sheets/{sheet_id}` → the `present` object `{object_id, kind: "sheet", revision,
    data}`: a `sheet` (the app's existing sheet view) whose `data` may carry **`actions`** (new, optional,
    1–8): `[{id: "a1".."a8", label (≤ 40), style: "primary" | "plain" | "undo"}]`, buttons under the blocks.
    `POST /v1/sheets/{sheet_id}/actions/{action_id}` → `{said}` (one line the app may show or speak): a tap is
    the user's own act. 404 `not_found` for an unknown sheet or button; 409 `approval_decided` for a
    question already answered or over, or a button already used; 409 `approval_expired` for one past its
    time. Each button is bound on the server (the phone sends only its id; what it does, until when and that
    it works once are Pisa's: one yes). Only code puts `actions` on a sheet (never the model: a composed sheet
    has none). The same sheet may come over the socket as a `present` (e.g. "chiudiamo" said in a turn).
    Fixture: `fixtures/valid/present_sheet_ritual.json`; REST fixtures `fixtures/rest/rituals.json`,
    `fixtures/rest/place_notes.json`.
  - **"Stanotte": the morning sheet.** Additive; an app that knows the rituals already shows it.
    - **The setting** `stanotte: {on}` in `GET`/`PUT /v1/rituals`, **off by default**. Pisa's code turns it on once,
      after 7 nights in shadow with at least one idea made and 100% of the lines it would have shown passing
      their code checks; they can turn it off in Settings and Pisa never turns it back on. No voice switch (the
      conversation's tools are unchanged).
    - **The notice**: once on, the first automatic morning briefing's `notify` (kind `briefing`) carries
      **`ritual: stanotte`** and `sheet_id`, and its text ends "Il foglio di stanotte è pronto."; the push's
      `userInfo` carries the same, as any ritual's. Never a second notice of its own; never sent while off.
    - **The sheet**, `GET /v1/sheets/{sheet_id}`, a view sheet with no `actions`, title "Stanotte": the idea made
      (`text`, `fact`, a `stat` of the code's revenue, a `table` of its sensitivities as the chart, `link`s to
      its evidence), the ideas killed and why (`list`), the nearest window, one Italy/US gap, one question they may
      not have thought of, each under a `section`; a part with no checked row is left out. "Dammi tutto" is a
      ritual sheet of its own (`ritual_sheets` `dammi_tutto`), for the night's drawer.
    - **The Sunday poster** (`week_poster`) gains a `section` "Stanotte" with three `stat`s (nights, searches,
      ideas made) and the newest page's `link`, only once on.
    Fixtures: `fixtures/valid/notify_stanotte.json`, `fixtures/rest/stanotte_sheet.json`,
    `fixtures/rest/rituals.json`.
  - **Ghost Run** (22:00, when tomorrow has something): Pisa rehearses tomorrow (the calendar, the pool's blocks and
    deadlines, travel times between places, the weather on a bike day) and finds at most one problem worth
    fixing (a clash, an impossible transfer, a deadline with no block, rain on the bike day) and at most one
    fix, which only moves what the user alone is in (their own event with nobody invited, or a task's block).
    The notice: "Domani, in 20 secondi: <the problem> <the fix as a question>"; the sheet: tomorrow as a table
    (when, what, where, leave at), the weather, the problem, the question, and the buttons `a1` yes / `a2` no.
    Nothing changes without their yes (by a tap, or a bare "sì" by voice: decided by code, never by text Pisa read).
    Tomorrow empty, or nothing worth saying: no notice at all. Due in quiet hours, it goes at their first minute
    out of quiet only if still relevant (its fix 30 minutes ahead or more; with no fix, before the day's first
    block), else nothing; every other ritual in quiet hours is dropped.
  - **One thing** (the morning): an automatic briefing (glasses on, app open, its time) is one sentence: the
    single thing that matters today (an overdue task, else one due today, else the next event, else the pool's
    first), by code. Asking for the briefing ("il briefing", `POST /v1/briefings`) gives the whole one.
  - **Chiudiamo?** (21:30): the day in a minute: what Pisa did (the action log), what moved, what is open for
    tomorrow, with an Undo button per line where done-by-being-there allows (a calendar change within its 24 h, a task Pisa closed
    by being there, a task done today). By voice: "chiudiamo" shows it now. Nothing on an empty day.
  - **The shape of your week** (Sunday 19:00): a poster-like sheet: the hours by kind (meetings, gym, travel,
    other, free), the gym streak (weeks of the last 4), the tasks done, the fullest day. Code's numbers only.
  - **Something different**: at most `per_week` a week and one a day, on a free evening (18–21) or
    weekend slot (10–18) with nothing booked in the next 3 hours: "Vuoi provare qualcosa di diverso oggi?
    Avevo in mente: …" with one concrete new place of the kind they go to most, near the place they go to most.
    Never the same idea twice; their "no" is remembered for that kind.
- **Place notes.** `GET /v1/places/notes` → `{notes: [{id, text, lat, lon, radius_m (50–500), place_name?,
  created_at, last_fired_at?, task_id?}]}`: the notes, newest first, then the open tasks with a place (id
  `task:<uuid>`, `task_id` set: they remind the same way, "il bagnoschiuma è qui, 2 minuti?", and go with the
  task). `POST /v1/places/notes {text, lat, lon, radius_m?, place_name?}` → 201 the note; `DELETE
  /v1/places/notes/{id}` → 204 (404 for a task's entry). By voice: "ricordami quando sono da Esselunga di
  comprare il bagnoschiuma" (Pisa's `remember_at_place`: the place resolved by Pisa's place tools, the text only in
  their own words, never outside text), `list_place_notes`, `forget_place_note`.
  - The phone registers the 20 nearest as region monitors (re-registered on a significant location change)
    and, entering one, sends **`place_enter`** `{note_ids: [1–20 ids]}` (a new WebSocket message with the usual
    envelope; or `POST /v1/places/enter {note_ids}` → `{fired, text}`). Each note fires at most once in 6 hours
    (one visit); Pisa answers with one notice (on the socket, or a push). Nothing in quiet hours (the next visit
    fires). Fixture: `fixtures/valid/place_enter.json`.
## The ledger, stop everything, the vault, earned autonomy

The server side came first; the app builds to it. Every route needs
the device token, like the rest of `/v1`. Dates (`*_on`) are the user's local days; times are RFC 3339 in their zone.

- **The ledger: everything Pisa did, why, from where, with its Undo.** A view over what Pisa already records (its
  decision log, approvals, what closed by itself, bulk changes), plus Pisa's own phase-3 acts (bills and vault
  reminders put in the pool, a stop and a resume, an autonomy level changed, the Sunday card). No new approval kind.
  - `GET /v1/ledger?week=YYYY-Www` (ISO week in the user's zone; default: this week) →
    `{week, from, to, days: [{date, lines: [Line]}], totals: {lines, undone, by_kind: {kind: n}, by_source:
    {source: n}}, next_page}`; days newest first, lines newest first, 200 lines a page (`?page=0, 1…`;
    `next_page` is null on the last); the totals count the whole week.
  - **Line**: `{id, at, kind, title, why, source, type?, undo: {available, until}}`.
    - `id`: `decision:<uuid>`, `done:<uuid>`, `bulk:<uuid>` or `profile:<uuid>` (core-memory-1: Pisa took back what it
      had misunderstood of them; its Undo restores it) (opaque to the app).
    - `kind`: `calendar` | `plan` | `task` | `task_done` | `event_done` | `task_dismissed` | `tasks_bulk` |
      `email` | `reminder` | `vault` | `bill` | `money` | `stop` | `autonomy` | `card` | `skill` (a skill turned on) | `other`.
    - `why`: Pisa's reason in the user's language (the `purpose` the brain gave, or code's sentence); never an
      email's text. `title`: the item's title (data, shown as Today shows it), or null.
    - `source`: `owner_said` (their own words) | `owner_yes` (their yes to Pisa's proposal) | `outside_text` (after Pisa
      read an email or a page, with their yes) | `rule` (a standing rule of theirs: done by being there, an autonomy
      level, the vault's reminders, a bill from email) | `follow_up` (Pisa following up: a task aged out) | `job` (a
      scheduled job: the Sunday card).
    - `type`: the autonomy action type this line counts for (`calendar_move`…), when it counts for one.
    - `undo.available`: true while the line can be undone; `until`: when that ends (null when it never could).
  - `POST /v1/ledger/{id}/undo` → 200 the Line, undone (`undo.available` false). It runs the item's existing Undo
    (the 24 h, an approval's undo, a bulk's undo, a dismissed task reopened): 409 `undo_expired` when it is
    over or the line never had one; 404 `not_found`; an approval's own codes (`approval_decided`) pass through.
  - `GET /v1/ledger/summary?week=YYYY-Www` → the Sunday card: `{week, counts: {kind: n}, total, undone, mattered:
    [Line ×≤3], undoable: [Line ×≤10], proposals: [Proposal], subscriptions: [Subscription], sheet: {title,
    blocks}}`. `mattered` are the week's three biggest acts (their calendar first, then what Pisa did by a rule, then
    tasks). `sheet` is a `sheet` (the same block templates, the same limits) the app draws as it draws any
    `present` sheet: counts as `stat`s, the three things, the proposal, the subscriptions. **Proposal**:
    `{type, level, kept, undone, days, since_on, bound_pct, last_error?: {on, what?}, text}`; **Subscription**:
    below; `text` is Pisa's question.
  - **The card's proposal**: **one proposal per card** (the type with the most
    evidence; at most one rung a type a week), drawn as blocks: a `section` "Una proposta da Pisa", the type and its
    scope ("Spostamenti di calendario (solo eventi tuoi, senza altri invitati)"), what changes from today ("Da
    oggi: li faccio e te lo dico subito dopo, invece di chiederti prima."), "Perché lo propongo" with a `list`
    (the numbers: "Ultimi 23 spostamenti: 23 tenuti, 0 annullati (dal 12 ottobre)."; the bound: "errore stimato
    al massimo 13%: non è zero, per questo resto al passo «faccio e ti dico»"; the last error, or none), "Cosa
    cambia" with a `list` (Undo, other guests always asked, the first undo goes back to asking, "Pisa, ferma tutto"),
    the three answers as `chips` ("Sì", "Sì, ma solo per …", "No, continua a chiedere": the app's Face ID yes, or
    their words) and "Non serve rispondere: se non scegli nulla, non cambia niente."
  - **`metrics`** (the week's three numbers): `{undo: [{type, rung, kept, undone,
    rate_pct, bound_pct}], rubber_stamp: {fast, asked, rate_pct}, surprise: {undone, actions, per_100}}`. `undo`:
    per type and the rung it was done at (`ask` when they said yes, else Pisa's level); `bound_pct` the 95% upper bound
    (the rule of three for 0 undone; null past 4). `rubber_stamp`: their yeses to Pisa's asks given in under 3 s, of
    all their yeses. `surprise`: undos of what Pisa did silently, per 100 silent actions (target 0). The same numbers by
    week in the report part `autonomy`.
  - **The Sunday card**: Sunday at 18:00 in the user's zone, once a week, Pisa queues a notify (`kind: info`,
    `ledger_week: "YYYY-Www"`; the push's `userInfo` carries `ledger_week` too): "La tua settimana con Pisa…".
    Quiet hours hold it and release it as itself (with its `ledger_week`); a stop over Sunday evening moves it
    to Monday (the week just ended). A tap opens the sheet from `GET /v1/ledger/summary`.
    Fixtures: `fixtures/valid/notify_ledger_week.json`, `fixtures/invalid/notify_ledger_week_bad.json`,
    `fixtures/rest/ledger_week.json`, `fixtures/rest/ledger_summary.json`.
- **Stop everything ("Pisa, ferma tutto").** A global pause of Pisa's own initiative.
  - `POST /v1/stop` `{scope: "all"}` → `Stop`; `POST /v1/stop/resume` → `Stop`; `GET /v1/stop` → `Stop` =
    `{stopped, since?, by?: "app"|"voice", paused: [what]}`. Both posts are idempotent (a second stop keeps the
    first `since`).
  - `paused` names what waits while stopped: `follow_ups` (check-ins, aged tasks, Pisa speaking first),
    `scheduled_pushes` (the briefing, the Live Activity, the weekly check, the Sunday card), `planning` (the task
    pool's and the day's re-plans, done by being there), `email_tasks` (tasks and bills from new email: the mail
    itself still syncs and waits), `vault_reminders`, `autonomy` (every level acts as `ask`), `ready_answers`
   .
  - Never paused: the user's own requests (a talk, a REST call, their yes to an approval), the reminders they asked
    for ("ricordami…") and the floors (leave now), an approval's expiry. While stopped, Pisa's queued notices of
    every other kind (check-ins, Pisa speaking first, the vault's line, a skill's schedule) wait and go after the
    resume; the automatic briefing (time, glasses, app open) and attend's items folded into a talk are skipped.
  - **A stop** also puts **every autonomy type back on `ask`** (its history kept; a ledger line of kind
    `autonomy` per type, without an Undo: raising one again takes their Face ID) and Pisa proposes nothing for 14
    days after it (`proposals_from` in `GET /v1/autonomy`).
  - "Fermati" alone doesn't stop Pisa: the words name all of it ("ferma tutto", "stop everything").
  - Spoken: "Pisa, ferma tutto" / "stop everything" (the brain's `stop_everything`, only on their own words this turn,
    never after outside text), "Pisa, riprendi" / "resume" (`resume_everything`). Pisa confirms in one line what stopped.
    Fixture: `fixtures/rest/stop.json`.
- **The life-admin vault.**
  - `GET /v1/vault/documents` → `{documents: [Document], aire: Aire}` (by expiry, soonest first); `POST /v1/vault/documents`
    → 201 `Document`; `PATCH /v1/vault/documents/{id}` → `Document` (a new expiry dismisses the old renewal task
    and its reminders start again); `DELETE /v1/vault/documents/{id}` → 204 (its
    open reminder task is dismissed).
  - **Document**: `{id, kind, country, number_last4?, expires_on, issued_on?, notes?, reminders: [{days_before,
    done}], task_id?, abroad?: {where, on}, warning?, created_at, updated_at}`.
    - `warning`: a `cie` whose notes say it is a paper card ("cartacea", "paper"): "Una carta
      d'identità cartacea non vale più per l'espatrio dal 3 agosto 2026: conviene fare la CIE." (Reg. UE
      2019/1157; its use inside Italy is disputed, so only the travel part is said). The app shows it under the
      document.
    - `kind`: `cie` | `passport` | `driving_licence` | `health_card` | `car_revision` | `car_tax` |
      `car_insurance` | `visa` | `green_card` | `us_licence` | `other`; `country`: ISO 3166-1 alpha-2 or null; when not
      sent, the issuer for a one-country kind (`cie`: IT; `green_card`, `us_licence`: US), else the server's
      `PISA_COUNTRY`, else null (a `visa` has no default).
    - **Never a full document number**: `number_last4` is exactly 4 letters or digits; a body with `number`, or
      `notes` holding a number (5 or more digits once spaces, dots and dashes are gone, or a code of 6+ letters
      and digits with 3+ digits: "YA 123 4567", a codice fiscale), is refused (400 `validation_failed`). Photos are out of scope.
    - `reminders` default by kind (days before expiry): CIE
      210/60/14, passport 270/90/30, patente 120/30, US licence 365/60, visa or ESTA 90/30, green card 180/60, car
      revision 60/14, car tax 30/7, insurance 30/7, the rest 90/30. When one is due, Pisa puts a task in the pool
      ("Rinnovare la carta d'identità", the deadline the expiry, `task_id` here) and marks it done; a later one
      raises the task's priority.
    - `abroad`: where the user will be in the renewal window (their calendar's trips), when that is the other
      country: outside the document's country ("la CIE scade a marzo e a marzo sei a New York: prenoto al
      consolato?").
  - **Aire**: `{registered: true|false|null, asked_on: date|null}`. Whether the user is registered
    with AIRE changes where they renew (the consulate or a Comune); Pisa never assumes it. Only on a server in Italy
    (`PISA_COUNTRY=IT`) and only about an Italian document (`country: IT`), Pisa asks it **once**, at the
    first CIE, passport, patente or health card they add or whose reminder fires ("Sei iscritto all'AIRE? Cambia
    dove rinnovi i documenti (consolato o Comune)."); their answer in their own words keeps it (the brain's
    `note_aire`, never after outside text). `PUT /v1/vault/aire {registered: bool|null}` → `Aire` (the app;
    null forgets it, and Pisa may ask again).
  - **The first reminders** (the research's first ten, §6): the ones bound to a document are the lead
    times above; the four that stand alone go in the pool as tasks (`source_label: vault`, a deadline, a ledger
    line of kind `vault` with its Undo), once, from the minute's `vault_starters` step (it waits while Pisa is
    stopped): the paper ID card check (at once, unless a CIE is in the vault), "has the accountant filed Redditi
    PF and RW?" (from 1 Nov, due 30 Jan), the second income-tax advance (from 15 Nov, due 30 Nov), the tax
    season's checklist for the commercialista (from 15 Jan, due 31 Jan); the yearly ones once a year.
  - `GET /v1/vault/bills` → `{bills: [{id, from, subject, amount, currency, due_on?, paid, source_email_id,
    task_id?}]}`, newest first: bills Pisa recognised in their email (subject and snippet only, no new scope; Italian
    and US utilities, F24, pagoPA). A bill comes from a biller's address domain or names itself as a notice (an
    invoice or pagoPA marker) with a due date; marketing (offers, news, "scopri", "black friday") never is. A bill
    to pay becomes a task "Pagare la bolletta X entro …" unless their own email-task rules say no, or the bill
    already has one (from its email or thread, or the same sender's open bill task with the same amount or due
    date); a direct debit is listed as `paid`.
  - Spoken: "aggiungi la scadenza della carta d'identità: marzo 2027" (`add_document`, only on their words, never
    after outside text; that turn's stored words lose any number in them), "che documenti scadono?"
    (`list_documents`), "quali bollette devo pagare?" (`list_bills`). Fixtures: `fixtures/rest/vault_documents.json`, `fixtures/rest/vault_bills.json`.
- **Earned autonomy.**
  - `GET /v1/autonomy` → `{types: [Autonomy], stopped, proposals_from?}`. **Autonomy**: `{type, level, kept, undone, since,
    locked, proposal?}`. `type`: `calendar_create` | `calendar_move` | `calendar_cancel` | `task_done` |
    `task_close` | `email_draft` | `email_send`; `level`: `ask` (today's behaviour, every type's start) | `tell`
    (Pisa does it and says it) | `silent` (Pisa does it; the ledger has it) | `ledger` (Pisa does it; only the Sunday card
    shows it). `kept`/`undone`: the last 30 days. `since`: when the level was set. `locked`: true for `email_send`
    (a send to other people stays at `ask`). `proposal`: `{level, kept, undone, days, since_on, bound_pct,
    last_error?}` when Pisa proposes the next level.
  - **What a rung takes** (the rule of three: 0 errors in n bounds the error at
    3/n), counted on the actions at the current level (at `ask`, every action Pisa keeps, 90 days), at most this
    many of the last N undone, and this long at the level (at `ask`: since the oldest of those N):

    | type | → tell | → silent | → ledger |
    |---|---|---|---|
    | calendar_move, calendar_create | 20, 0 undone, 14 days | 60, ≤1, 30 days | 150, ≤2, 56 days |
    | calendar_cancel | 20, 0, 14 days | 60, ≤1, 30 days | never |
    | task_done, task_close | 15, 0, 14 days | 50, ≤1, 14 days | 120, ≤2, 30 days |
    | email_draft | 10, 0, 7 days | 30, 0, 14 days | never (drafts stay visible) |
    | email_send | never | never | never |

    No proposal with fewer than 5 actions of the type in the last 30 days, or within 14 days of a stop. **An undo
    of something Pisa did at a level takes that type down one rung** (not all the way; a ledger line of kind
    `autonomy`); a draft left unsent is no undo.
  - Whatever the level, **sends to other people (attendees notified, an email), anything after outside text
    (this turn's, or a proposal first made after it and corrected later), a start before 07:00, and anything
    while stopped stay at `ask`**; a type that moves money would be locked like `email_send` (none exists). The
    user's own words act as before. At `silent` the phone gets no `action_done` toast.
  - **Back to `ask`** needs no Face ID (asking more is never a risk): `POST /v1/autonomy/{type}/ask` →
    `Autonomy`, or by voice ("chiedimelo di nuovo prima di spostare gli eventi").
  - **Changing a level** takes Face ID, signed by the phone: `POST /v1/phone/challenge
    {action: "autonomy", type, level}` → `{nonce, canonical, expires_at, action}`; the phone signs `canonical`
    (UTF-8) with its Secure Enclave key (`.ecdsaSignatureMessageX962SHA256`, DER, base64) after Face ID; then
    `POST /v1/autonomy/{type}` `{level, nonce, signature, key_id}` → `Autonomy`. A missing or wrong signature,
    or a nonce used or older than 5 minutes: 403 `biometric_required`; `email_send` above `ask`: 400
    `validation_failed`. Or by voice: their yes to Pisa's Sunday proposal for that type and level (one yes):
    never with a "no" or a "later" in it, never after outside text in the talk, and only when it is a yes to
    stopping asking ("sì, falli pure senza chiedermelo") or Pisa's last reply put the proposal to them; the evidence
    is checked again when they say it.
  - **The phone's key**: `POST /v1/phone/keys {key_id, public_key, label?}` (P-256, X9.63,
    base64) → 201. A key belongs to the paired device that registered it (a revoked device's keys sign
    nothing). The store's first key needs no signature but only an iPhone registers it (400 otherwise). Once
    one exists a new one needs `{nonce, signature, signer_key_id}` from a `{action: "add_key", key_id,
    public_key}` challenge, whose `canonical` names the new key's fingerprint (the first 16 hex digits of the
    SHA-256 of its X9.63 bytes) for the phone to show. Fixture: `fixtures/rest/autonomy.json`.
- **Money is a decimal string** (`"84.20"`) with its ISO currency (`EUR`, `USD`).
- **Subscriptions seen** (money back, read-only): `subscriptions` in the Sunday summary, `{name, amount, currency,
  period: "monthly"|"yearly", last_on, count, text}`: the same sender charging a similar amount every month or year,
  from their receipts. Pisa asks "vuoi che ti prepari la disdetta?"; it never contacts a company.

## Pisa's skills: wish to skill, the skill garden, every complaint becomes a test

The server side came first; the app builds to it.

- **A skill** is a view recipe Pisa built from the user's wish ("vorrei che tu potessi dirmi…", "I wish you
  could…"), tried on their data. (`kind: tool` and `status: proposed` are an older kind no longer made.)
  **Skill**: `{id, name, kind: "view"|"tool", status: "proposed"|"trial"|"on"|"withered"|"off", made_from,
  tests_passed, tests_total, last_used_at, uses_30d, schedule?: {at: "HH:MM", days: "daily"|"weekdays"|
  "weekends"}, created_at, enabled_at}`. `made_from` is their sentence (their own words, never outside text).
- **Its checks**: 2–5 acceptance checks Pisa wrote from their sentence, data, never code: `{input, shows?, has?}`
  (`shows`: a block template the sheet must hold; `has`: words it must show). The trial runs the recipe's reads
  now (no write, no push) and counts the checks the sheet passes.
- `GET /v1/skills` → `{skills: [Skill]}` (newest first, at most 30); `GET /v1/skills/{id}` → `Skill` +
  `{checks: [{input, shows?, has?, passed}], trial?: {at, sheet: {title, blocks}}}` (the trial's sheet, drawn as a
  `sheet`: "Lo accendo?").
- **Turning one on takes the phone's signature** (the phone key store): `POST /v1/phone/challenge {action:
  "skill", skill_id}` → `{nonce, canonical, expires_at, action}`; `canonical` names the skill, what it reads
  (`reads=search_my_email(days=7, query=banca), …`) and the first 16 hex of the digest of its recipe and checks,
  so a skill changed after the challenge is never the one turned on (403, or 400 when it changed meanwhile); the
  phone shows it and signs it after Face ID;
  `POST /v1/skills/{id}/enable {nonce, signature, key_id}` → `Skill` (`on`). 403 `biometric_required` without a
  valid signature; 400 `validation_failed` when it isn't a view whose trial passed every check. Their spoken yes
  ("sì, accendila") is the yes; the signature is the Face ID.
- `POST /v1/skills/{id}/disable` → `Skill` (`off`); `DELETE /v1/skills/{id}` → 204.
- **Running one**: by name in a talk ("dimmi il treno", "mostrami <name>"), or on the schedule they gave ("ogni
  mattina alle 8"): a notify `kind: info` with the skill's lines (quiet hours hold it; a stop pauses it; at most
  4 an hour). A skill that reads email, the web or the map is never scheduled (it runs when they ask), and what
  it read is outside text to the rest of that turn.
- **The garden**: each use is counted (`uses_30d`: their own, never the schedule's); a skill they haven't used for 30
  days `withered` (no schedule; still callable, and a call brings it back `on`). They rename one by voice ("rinomina la skill meteo in tempo").
- **The Sunday card** gains `skills: {learned: [{id, name}], withered: [{id, name, text}]}` ("meteo: la
  tengo?") and two sheet sections, "Cosa ho imparato a fare per te" and "Le tengo?".
- **Every complaint becomes a test**: their corrections of Pisa ("no, ti ho detto…", "no, intendevo sabato", "hai
  capito male", "non è quello che ho chiesto", an undo within 2 minutes; never a question) are kept as private
  replay candidates on their server for 90 days (their words, Pisa's moves, Pisa's reply only when that turn read no outside
  text, the correction; the world minimised: no email bodies, a name only if they said it). Nothing on the
  wire; never in the repo or CI.
- Fixtures: `fixtures/rest/skills.json`, `fixtures/rest/skill.json`; `fixtures/rest/ledger_summary.json` gains
  `skills`.

## People, and the care circle (opt-in)

**Pisa knows about other people only what they'd expect it to know, and speaks to them only
openly, as Pisa.** Nothing here contacts anyone: Pisa writes drafts the user sends themselves. The
server side came first; the app builds to it.

- **The people list.** Pisa builds it from what already exists, never invents anyone: the addresses the user
  wrote to (with their names), the people who wrote to them among those, and the guests of their calendar events,
  merged by email. People they name themselves ("mia madre", "Laura") join from their words. Their corrections win: a
  field they set in the app (or `hidden`) is never overwritten by a sync.
  - `GET /v1/people` → `{people: [Person], orbits: bool}`, by name. `orbits` is false once they said "non
    ricordarmi più di nessuno" (or turned it off in the app).
  - A Person: `{id, name, relation?, birthday?, tz?, city?, emails: [string], last_contact_at?, notes_count,
    gift_ideas: [{id, text, from: "owner" | "email", at}], dates: [{id, kind: "birthday" | "anniversary" |
    "name_day" | "other", on, remind_days_before: [int], source: "owner" | "calendar"}], orbit: bool, source:
    "owner" | "email" | "calendar"}`.
    - `on` is `MM-DD` (no year known) or `YYYY-MM-DD`. `birthday` repeats the birthday's `on`, for a list row.
    - `tz` is an IANA zone ("America/New_York"), from their words or the person's city; null when unknown.
    - `gift_ideas` keep their source. One `from: "email"` is the person's own words in an email to them ("mi
      piacerebbe tanto…"), kept as data: Pisa never acts on it and says it only when they ask or near the date.
    - `notes_count` counts the notes they gave about the person, gift ideas included.
    - `orbit`: false when they said "non ricordarmi più di Paolo".
  - `PATCH /v1/people/{id}` with any of `{name, relation, tz, city, birthday, orbit, hidden}` → the Person. Null
    clears `relation`, `tz`, `city` or `birthday`. A bad `tz` (not an IANA zone) or `birthday` (not `MM-DD` /
    `YYYY-MM-DD`, or no such day) is 400 `validation_failed`. `hidden: true` takes the person out of the list
    and out of every line Pisa says; the sync never brings them back.
  - `DELETE /v1/people/{id}/notes/{n}` → 204: one note or gift idea, `n` its `id`. 404 `not_found` when it
    isn't the person's.
  - `POST /v1/people/{id}/notes/{n}/keep` → the Person: they keep an email's gift idea as their own (`from`
    becomes `owner`). An idea `from: email` otherwise lives only as long as its email (90 days at most) and goes
    when Google is disconnected.
  - `DELETE /v1/people/{id}` → 204: Pisa forgets the person (notes, dates, lines, care circle, where they live);
    only the address stays, hidden, so the sync never brings them back.
  - `POST /v1/people/{id}/merge` with `{into}` → the Person who stays: two rows that are one person (two
    addresses). Notes, addresses and dates move; where both have one (a birthday, a care entry), `into`'s stays.
  - `PUT /v1/people/orbits` with `{orbits: bool}` → `{orbits}`: the switch for the Sunday line (their "non dirmi
    più chi non sento" turns it off by voice).
  - A reminder task of a date from their calendar has `source.kind: "calendar"` (the `GET /v1/tasks`).
  - Fixture: `fixtures/rest/people.json`.
- **What Pisa does with it** (server side, no new socket message):
  - **Dates**: from their words ("il compleanno di mia madre è il 12 marzo": the day and the month are read from
    their own words, never from the model or an email) and from their calendar's yearly all-day events of their own
    (no guests: never someone's invite) or Google's birthday events, named for a birthday or an anniversary
    ("Compleanno di Marco Rossi": the whole name; a first name only is never matched). Never guessed.
  - **Reminders**: a pool task `max(remind_days_before)` days before (default `[7, 1]`: a week before),
    due `min(remind_days_before)` days before (the day before). On the day, one line in the morning briefing
    ("Oggi compie gli anni Marco: gli scrivi?"), with the hour where they are when it differs, computed when
    said and on THEIR day ("a New York sono le 3: glielo mandi alle 9 loro (le 14 tue)?", "…(stasera alle 21
    tue)", "a Auckland la giornata è già passata: gli scrivi subito?"). Someone with no zone is at home.
  - **Orbits**: on Sunday, at most one person a week, one line in the briefing ("Non senti Paolo da 3 mesi"):
    the one they haven't heard from (email, shared events) for more than twice their usual rhythm with them, and at
    least 30 days.
  - **Thank-you notes**: the morning after a meal or a party in their calendar (one they accepted, at most 8
    guests) with a guest they know, at most one, with a draft they send themselves (a people offer with its
    buttons, no longer a briefing line).
  - Every draft goes to the phone's compose screen (`compose`, as today); Pisa never sends it.
- **The care circle (opt-in; later on the phone).** `GET /v1/care` → `{members: [{person_id, name,
  shares: "location" | "calendar_busy" | "nothing", since}]}`; `PUT /v1/care` with `{members: [{person_id,
  shares}]}` replaces the list (at most 20) and returns it. `since` stays for a member whose `shares` didn't
  change. This slice **stores only what they enter**: nothing is collected from anyone, and nothing is shared.
  An unknown `person_id` is 404 `not_found`. Fixture: `fixtures/rest/care.json`.

## The rest of the people you love

The matchmaker of moments, thank-you notes, wishes at their local hour, Rome's
clock in New York. **Pisa prepares, the user taps**: every offer carries handoff buttons that open WhatsApp or Messages
with Pisa's draft, and Pisa never sends it. What another person said is data: Pisa's line is its own
template, holding only a name Pisa knows, a city of Pisa's own table and a day code computed.

- **A people offer** is a notice (`notify`, kind `info`, **`ritual: people`**, `sheet_id`) whose sheet is read
  with `GET /v1/sheets/{sheet_id}` like a ritual's (the same `RitualSheet`, with no `actions`): a view sheet with
  Pisa's line (`text`), where it comes from (`fact` "Fonte": "la sua email del 02/11", "il suo calendario", "il tuo
  calendario"), and two handoff `handoff` blocks, `kind: message`: WhatsApp (`https://wa.me/?text=…`) and iMessage
  (`sms:&body=…`), each with `text` = the draft; they pick the contact in the app. Its push carries `ritual` and
  `sheet_id` as the. A tap's `handoff_opened` carries `object_id` = that `sheet_id`. The app reads `ritual` as
  a string: a value it doesn't know is a notice with a sheet. Fixtures: `fixtures/valid/notify_people_offer.json`,
  `fixtures/rest/people_offer_sheet.json`.
  - Never in their quiet hours, never at night (only 9:00-20:30 their time), one every 30 minutes at most, never
    while Pisa is stopped, never about someone they hid; past its moment it lapses unsaid.
- **Moments** ("Laura è a Roma giovedì, ti va un caffè?"): from the person's **own words**: their email to them
  (their part, never a quoted reply; first person: "sarò a Roma giovedì", "I'll be in Rome on Thursday",
  "giovedì sono libera"; never a question, a "non", a "se"/"forse", nor a sentence naming two days) or their own
  calendar shared with them (a calendar whose id is their address; an event naming one city Pisa knows). **Never
  inferred from a location**, the user's or theirs. The day is within 14 days, they are in that city that day (a trip they
  confirmed, from their trips, else their home's city), or they said they're free and live in their city; their calendar has a free
  hour (10:00-21:00) that day. At most **one a week per person**; never for someone they took out of their orbits.
  An email's moment goes with its email; Google's disconnect forgets them all, and every offer not yet
  sent. A wish whose date is gone, or a gift's thank-you whose email is gone, lapses unsaid.
- **Thank-you notes**: the morning after a meal or a party with someone (the rule; from 10:00, once per event,
  one a day: it moves from the briefing's line to this offer), and when someone's gift reached them: their own
  email ("ti ho spedito un pensierino") or a store's notice from the store's own exact domain (no subdomain: never
  a buyer-seller relay) naming them by their whole name ("Laura Bianchi ti ha inviato un regalo"). The draft is
  **in their style**: their greeting, their sign-off (only one they used in two emails at least) and their "!", from their own
  sent emails to that person ("Ciao Marco! Grazie ancora per ieri, è stato bellissimo!\nUn abbraccio, R."); Pisa's
  default when they never wrote to them.
- **Wishes at their local hour**: on a birthday, anniversary or name day, at **their** 9:00 (the first quarter
  hour from their 9:00 that is daytime for them and outside their quiet hours, before their 21:00): "A New York
  sono le 9: vuoi fare gli auguri a Marco?" (their hour as it is when it goes out), with the wish drafted in their
  style. When no such moment exists (their day falls in their night), only the morning briefing's people line says it.
- **Rome's clock in New York**: a Person (`GET /v1/people`) gains **`clock`** (null when `tz` is unknown):
  `{at, call: good|early|late, call_from?, line}`; `at` is their time now with their offset; `call` is `good`
  9:00-21:30 there (10:00 on their weekend); `call_from` the next good moment (their offset), null when it is
  now; `line` Pisa's sentence in their language ("Da mamma sono le 23:40: domani, dalle 9 loro (le 15 tue)?"). By
  voice: "che ore sono da mamma?", "posso chiamare Laura?" (the `their_time` tool says the same).

## The phone's place in the world

The user travels between Italy and the US. Pisa follows the phone's zone, never a fixed Rome.

- **`hello`** gains three optional fields, sent on every connect:
  - `tz`: the phone's IANA zone (`TimeZone.current.identifier`, e.g. `"America/New_York"`);
  - `region`: the phone's region, ISO 3166-1 alpha-2 (`"US"`, `"IT"`);
  - `hour_cycle`: `"h23"` (15:00) or `"h12"` (3:00 PM), from the phone's locale.
- **`device_context`** (device → Pisa, new): `{tz?, region?, hour_cycle?}` with at least one field, sent when any
  of them changes while the socket is open (a landing, a settings change). Pisa answers nothing. Fixture:
  `fixtures/valid/device_context.json`; `fixtures/valid/hello_k14.json`.
- **Absent or unknown means the last known.** A zone Pisa can't load (a typo, a zone newer than Pisa's database) or a
  field that is absent keeps what Pisa had; Pisa logs it and never fails the hello. Before any phone sent one, Pisa uses
  the server's default (`PISA_TIMEZONE`, Rome).
- **What follows the zone**: the day ("oggi", "domani", the plan's day), quiet hours, the briefing hour, the
  holidays (Italy's and the US's calendars, by `region`), and how Pisa says and shows a time (`hour_cycle`). A
  calendar event keeps its own zone: an event made in Rome stays at its instant, shown in the user's zone now.
  When the user writes to someone at home while they are away, Pisa says their time ("da loro sono le 23:00: la
  mando domattina?").
- Times on the wire stay RFC 3339 with their offset, as always; the phone never converts a server time into
  Rome's.

## Ready answers (server side)

Nothing new on the wire. Pisa keeps answers ready for the likely questions (what's next and when to leave, today's
changes, the pool's top items, the research it is doing), refreshed on every change and at most every 5
minutes. A question they match is answered at once; the brain's turn only checks that nothing changed since.
The talk timeline (`report.yml` part `talk`) gains `ready_hit` (`{topic, age_s, verified}`) and
`ready_stale`; the talk report prints the hit rate beside the first-content median by route.

## Trips, from their confirmation emails

The user lives between Italy and the US. Pisa builds their trips from the confirmation emails it already reads
(flights, trains, hotels, car rentals). **An email is data**: a trip is built from it, never an action
Pisa takes because an email says so. An email that says "cancel your flight" changes nothing; only the user
(`PATCH`) cancels a trip.

- `GET /v1/trips?scope=upcoming|all` (default `upcoming`: not cancelled, not yet back) → `{trips: [Trip]}`,
  soonest first, at most 50.
- `Trip` = `{id, title, from_city, to_city, from_country?, to_country?, depart_at, arrive_at, tz_from, tz_to,
  status: planned|cancelled, doubt: null|unverified_sender|someone_else, proposals: [{ref?, at, until?,
  email_id}], legs: [Leg], checklist: [{text, done, by: pisa|owner}], source_email_ids}`.
  `depart_at` and `arrive_at` are the first leg's, each with its own zone's offset (`tz_from`, `tz_to`):
  a 10:35 departure from Rome reads `…T10:35:00+01:00`, its 14:20 arrival in New York `…T14:20:00-05:00`.
- `Leg` = `{kind: flight|train|car|hotel|other, ref?, at, until?, place?, tz?, confirmation_last4?}`. `ref` is
  the flight or train number or the hotel's or rental's name; `at`/`until` are RFC 3339 in the leg's own zone
  (a hotel: check-in day and check-out day at 15:00 and 11:00 local); `place` is "FCO → JFK", a station pair,
  a city. **Only the last 4 characters** of a booking code ever leave Pisa (never the code, never the email).
- **What Pisa reads** (review of phase 5): only emails the intake judged (never `promo_or_spam`, never a sender
  they blocked). A trip from a sender that doesn't sell travel (airlines, railways, agencies, hotels, rentals;
  `app_settings.trips.senders` adds domains), or a booking that names only other people
  (`app_settings.trips.names`, else their Google address), has a `doubt`: shown "da confermare", no tasks, no
  landing, no residency evidence until they confirm it. A later email that gives a known leg (same kind, number
  and booking) other times is a `proposal`, never applied by itself. An email that states a cancellation only
  flags the trip.
- `PATCH /v1/trips/{id}` `{title?, status?: planned|cancelled, checklist?: [{text, done}], confirmed?: true,
  apply_change?: email_id}` → `Trip`. A `checklist` replaces the list; items Pisa didn't write are his (`by:
  owner`) and Pisa offers them again on his next trip abroad. `cancelled` dismisses Pisa's own tasks for the trip;
  `confirmed` clears the doubt and makes them; `apply_change` applies that email's proposal (Pisa's tasks follow).
  404 `not_found`.
- What Pisa makes of a trip: a pool task for the online check-in when it opens (24 h before for most
  airlines), and one to leave for the airport or the station at a time from the routes tools minus a buffer
  (international 3 h, domestic 2 h, a train 20 min; `app_settings.trips` changes them). Both carry
  `deadline_source: email`.
- **Landing**: when the primary phone reports the zone or region of a leg's arrival, from 30 minutes
  before it to 12 hours after, and a report at least 10 minutes later still says so, Pisa says once "Benvenuto a
  New York: sono le 18:40, domani hai…" with the tasks only that country allows. A `notify` of kind
  `briefing`; in their quiet hours there nothing is sent and the welcome joins their next briefing. Nothing new on
  the socket.
- **Country-bound tasks**: a task may be bound to `IT` or `US`: only what is done in person at an office of
  one country ("rinnovare la CIE", "anagrafe", "DMV"), never a call, a message, a payment or anything online,
  never a bare place name. In the other country Pisa leaves it out of the day's `todo`, unless it is pinned or due
  within 48 hours; before a departure it says "questi 3 li puoi fare solo qui". `GET /v1/tasks` items gain
  `country`; `PATCH /v1/tasks/{id}` `{country: <ISO 3166-1 alpha-2>|null}` is their word and holds (a new title is read again
  only when they never set it).
- **Departure checklist**: 3 days before a trip abroad Pisa tells them once that the list is ready, and shows it as
  a `sheet` when they ask: documents valid for the trip, the adapter, what only this country allows, the
  check-in, the insurance, their own items. Fixture: `fixtures/rest/trips.json`.

## The residency counter

**Pisa counts, Pisa never advises**: every line Pisa says or shows about it carries "è un conteggio, non una
consulenza" ("a count, not advice").

- `GET /v1/residency?year=YYYY` (default this year) → `{year, days: {<country>: n, ..., other?, unknown}, by_rule: [{rule,
  country, count, threshold, note}], evidence: [{day, country, source: phone_region|trip|calendar|his_words}],
  note}`. `evidence` lists each counted day of the year up to today (a day with no evidence is `unknown` and
  not listed).
- A day counts for every country their primary phone reported that day (the `region` is the country
  the phone is in; a report without a region says nothing); else where their trips put them; else their calendar;
  else unknown. **Their corrections win.** Trips "da confermare" give no evidence.
- **Any part of a day is a day** (Italy's "frazioni di giorno", the IRS's "any time during the day"): the day
  they fly between two countries counts for both (`evidence` lists that day once per country, `source:
  trip`), unless they corrected it.
- `days` has a key per ISO 3166-1 alpha-2 country they were in (and each rule's country, 0 without a day).
- The rules are data (`app_settings.residency_rules`: each rule written out, or named, `{"rule":
  "us_substantial_presence"}`). Without that setting only a server in Italy (`PISA_COUNTRY=IT`) has a rule,
  `it_presence_183`: 183 days of presence in the tax year (184 in a leap year); anywhere else `by_rule` is
  empty (Pisa counts days, assumes no tax rule). The US tests apply only when the setting names them:
  `us_substantial_presence`, this year's days + 1/3 of last year's + 1/6 of the year before's ≥ 183 with ≥ 31
  this year; `us_closer_connection`, the US days of this year against 183 (the closer-connection exception ends
  there). `count` may be fractional for a weighted rule. The note warns at 150 and
  170. Pisa says it in the first briefing of a month that goes out (after the composed text, word for word,
  outside the word budget), never daily, and when they ask.
- `POST /v1/residency/days` `{day: YYYY-MM-DD, country?: <ISO 3166-1 alpha-2>|other, countries?: [...]}` → `{day, country,
  countries, source: "his_words"}`: their correction for a day, one country or a travel day's two (a day in the
  future, or no such day: 400 `validation_failed`). A weighted rule's note says how many days of the years
  before have no data. Fixture: `fixtures/rest/residency.json`.

## Handoffs, Pisa prepares and the user taps

Restaurant and delivery platforms are partner-only, Resy bans bots, WhatsApp's AI Providers clause bars Pisa on the
Business API, and calls by Pisa come later. So Pisa does all the preparing (the page, the number, the words,
the list) and **the user's tap in the official app is the yes**. Pisa never sends, books, calls or pays by itself.

- **A new block in a `sheet`**: `{type: "handoff", handoff_id, kind, title, subtitle?, url?, phone?, text?,
  notes?}`:
  - `handoff_id`: a UUID, the button's own (Pisa keeps what it prepared under it);
  - `kind`: `open_url` (a page or an app's universal link), `call` (`tel:`), `message` (WhatsApp through
    `https://wa.me/…?text=`, iMessage/SMS through `sms:…&body=`), `map` (Apple Maps);
  - `title` (≤ 60, the button's words: "Prenota online", "Chiama", "Apri Uber"), `subtitle` (≤ 120);
  - `url` (≤ 2000): `https://…`, `tel:+<E.164>` or `sms:[+<E.164>]&body=<percent-encoded>`; never
    `javascript:`, `data:`, `file:` or any other scheme; no spaces. Pisa builds every URL from its own templates
    (below) with every parameter percent-encoded; `kind: call` needs `phone` or a `tel:` url (both: the same
    number), `message` and `open_url` and `map` need `url`. A place's own website shows its host in
    `subtitle` ("gabrielli.example · sito indicato sulla mappa"), so they see where it goes;
  - `phone` (E.164, `+` and 6–15 digits): shown under a call's button;
  - `text` (≤ 3000; A claim letter is ~2,000): the prepared words (a call's script, a message, a shopping
    list, a letter), shown under the button
    with a copy action;
  - `notes` (≤ 4, each ≤ 200): short lines ("Il numero è quello di Google Maps").

  The phone renders a big button; a tap opens the URL in its app (the user then confirms there: book, send,
  call, order). An older phone skips the block, so Pisa also says the essentials in words. Fixtures:
  `fixtures/valid/present_sheet_handoff.json`; `fixtures/invalid/present_sheet_handoff_javascript.json`.
- **`handoff_opened`** (device → Pisa, new): `{handoff_id, object_id}` (the sheet's `sheet:<uuid>`), sent when the
  owner taps the button and the URL opened. Pisa answers nothing; an id Pisa doesn't know is logged and dropped; only
  the first open counts. Fixture: `fixtures/valid/handoff_opened.json`.
- **Pisa's templates** (server side; `pisa/domain/handoffs.py`): WhatsApp `https://wa.me/<digits>?text=` (or
  `https://wa.me/?text=` to pick the contact in WhatsApp), `sms:<+digits>&body=`, `tel:<+digits>`, Uber
  `https://m.uber.com/ul/?action=setPickup&pickup%5Blatitude%5D=…&dropoff%5B…%5D=…` (brackets percent-encoded), Lyft
  `https://lyft.com/ride?id=lyft&pickup%5Blatitude%5D=…&destination%5B…%5D=…`, Apple Maps `https://maps.apple.com/?…`,
  a restaurant's page found by name (TheFork, OpenTable search pages; never a login), a store's own page
  (Glovo, Deliveroo, Esselunga, Instacart), and a place's own website as the places provider gave it (https,
  no credentials, no other URL inside it: no open redirect).
- **The rails**: a phone number or a URL from outside text (an email, a page) is used only after the user
  confirmed it, in a later message than Pisa's read-back; after outside text in a turn Pisa prepares only what
  their own words ask for, with no link, site or code their words don't hold, and a table's page is the platform's
  search, never a site; Pisa asks nothing else before preparing (their tap is the yes). After a booking or a
  call was opened, Pisa asks **once** how it went (a notice about 20 minutes later, the rails: quiet hours,
  never twice for the same preparation); on their word ("sì, fatto") a booking goes in the calendar, a call's
  change is made as they say it. Nothing is written before their word.

## A Face ID approval, signed by the phone's key

An approval with
`requires_biometric` is approved only with the phone's Secure Enclave key (the phone key store, `.biometryCurrentSet`:
Face ID with no passcode fallback). Ordinary approvals and every reject are unchanged (`{decision, auth}`).

- **The challenge**: `POST /v1/phone/challenge {action: "approval", approval_id}` → `{nonce, canonical, expires_at,
  action: "approval"}`. 404 `not_found` for an unknown approval; 409 `approval_decided` / `approval_expired` for one
  no longer pending; 400 `validation_failed` for one without `requires_biometric`. The nonce is bound to
  `{action: "approval", approval_id}`: it signs nothing else, once, for 5 minutes.
- **The canonical text**, built byte for byte (UTF-8), lines separated by `\n` only, **no trailing newline**, in this
  order:

  ```
  pisa-approval
  approval_id=<the approval's id>
  decision=approve
  kind=<the approval's kind: email_send | calendar_create | calendar_move | calendar_cancel>
  summary=<the approval's summary, every \r and \n replaced by one space>
  digest=<64 lowercase hex>
  nonce=<the nonce>
  expires_at=<the challenge's expires_at, RFC 3339 as in the response>
  ```

  Every value is single-line: the server replaces `\r` and `\n` in any value with a space (only `summary` can hold
  them). A key appears once. The phone requires the header `pisa-approval`, `approval_id` equal to the approval it
  tapped, `decision=approve`, a `digest` of 64 lowercase hex and `nonce` equal to the response's nonce; any other
  header, value, a missing one or a key twice is `challenge_mismatch` (nothing signed). Keys it doesn't know are
  accepted and shown with the rest (the user reads the whole text before Face ID); new keys go after `expires_at`.
- **The digest** binds what is approved, so an approval whose effect changed after the challenge is never the one
  run: `digest = sha256_hex(preimage)`, the preimage (UTF-8, `\n`-separated, no trailing newline):

  ```
  pisa-approval-digest/1
  <kind>
  <payload_sha256>
  <recipients>
  ```

  `payload_sha256` = SHA-256 hex of the stored `payload` as canonical JSON (`json.dumps(payload, sort_keys=True,
  separators=(",", ":"), ensure_ascii=False)`, UTF-8). `recipients` = every address the action writes to or notifies
  (`email_send`: to, cc and bcc; calendar kinds: the attendees who get an update), lowercased, trimmed, without
  duplicates, sorted, joined by `,`; empty when none. The phone shows the digest; the server recomputes it at the
  decision from the row as it is then.
  **Amendment**: the payload is hashed **without** the server's bookkeeping keys,
  `DIGEST_EXCLUDED = {"heard"}` (the C1 fix marks an email read back in full with `heard: true` after the
  approval was made). No other path writes an approval's payload between the challenge and the decision (a
  spoken "sì, senza avvisarli" sets `send_updates`, but a spoken yes never decides a `requires_biometric`
  approval). Calendar kinds' recipients are the payload's `notified` when `send_updates` is `all` (the names
  Google tells), else none. Fixture, byte for byte, for both sides: `fixtures/rest/k30_canonical.json` (the
  inputs, the payload hash, the digest preimage, the digest and the canonical text); also
  `fixtures/rest/approval_challenge.json`, `fixtures/rest/approval_decision_signed.json`.
- **The decision**: `POST /v1/approvals/{id}` `{decision: "approve", nonce, signature, key_id}` (no `auth`):
  `signature` is the phone key's ECDSA P-256 SHA-256 signature of the canonical text (`.ecdsaSignatureMessageX962SHA256`,
  DER, base64), `key_id` a key of the phone key store belonging to a paired, non-revoked device. The server rebuilds the
  canonical from the challenge it stored and the digest from the row now. A missing or wrong signature, a nonce used,
  older than 5 minutes or for another approval, or a digest that no longer matches: 403 `biometric_required` (the
  approval stays pending). `auth: "biometric"` alone is refused like no `auth` (403 `biometric_required`). → `Approval`
  as for any decision. The phone shows a 403 and never treats it as unpaired.
- **Spoken approvals** never decide a `requires_biometric` approval (as before: Pisa says to approve it on the phone).

## Money that comes back, promises, things that run out

**Pisa finds and drafts; the user sends.** Pisa never contacts
a company, never cancels, never claims, never pays. **Everything from email is data**: an amount is
copied as an email states it, or is the law's fixed sum (EU261 art. 7), never computed from anything else and
never invented; an email can't make Pisa send, open or write to anyone. Every route needs the device token.
Money is a decimal string with its currency; days (`*_on`) are the user's local days.

- **Flight claims (EU261 / US DOT)**: from a flight leg of a trip and a later email from a travel sender
  (the list) stating a cancellation, a delay (hours stated, or a new arrival time) or a denied boarding, or from
  the user's own words ("il volo AZ5287 ha fatto 4 ore di ritardo": `flight_claim`, never after outside text).
  - `GET /v1/money/claims` → `{claims: [Claim]}`, newest flight first (at most 50).
  - **Claim**: `{id, flight, place?, scheduled_at, disruption: delay|cancellation|denied_boarding, delay_min?,
    notice_days?, rule: eu261|us_dot|none, status, reason, why, distance_km?, amount?, currency?,
    deadline_on?, source: email|said, task_id?, created_at, updated_at}`.
    - `rule`: `eu261` when the flight leaves the EU (with Iceland, Norway, Switzerland), or arrives there on an
      EU airline; else `us_dot` when it touches the US; else `none`.
    - `amount` (EU261 only): €250 up to 1,500 km; €400 within the EU above 1,500 km or 1,500–3,500 km
      otherwise; €600 above 3,500 km: by the great-circle distance of the two airports (`distance_km`); none
      when Pisa doesn't know both airports. Per passenger; Pisa never applies the 50% reduction by itself.
      `us_dot` never has an amount: the refund is the fare they paid, which Pisa doesn't invent.
    - `status`: `possible` (a disruption, its delay not known yet, or a flight older than 2 years: "check
      first") | `eligible` | `not_eligible` | theirs: `sent` | `paid` | `dismissed`. Pisa never changes theirs.
    - `reason` (code) with `why` (their language): `delay_3h` | `cancelled_late` (told < 14 days before) |
      `denied_boarding` | `delay_unknown` | `delay_announced` (before the arrival) | `under_3h` |
      `notice_14_days` | `older_than_2_years` | `refund_due` (US: cancelled, a cash refund in 7 business
      days) | `no_us_delay_compensation` | `not_covered`.
    - `deadline_on`: 12 months after the flight (a soft deadline, the research's).
  - An `eligible` claim puts one task in the pool ("Chiedere a ITA Airways la compensazione del volo AZ5287 (600
    €)"), unless their own email-task rules say no or Pisa is stopped.
  - `GET /v1/money/claims/{id}/draft?lang=it-IT|en-US` → `Draft` = `{lang, subject, text, buttons:
    [Handoff]}`: the research's letter (§7a) with what Pisa knows filled in and the rest in `[brackets]` for them
    (their name, their IBAN, a booking code's other characters: Pisa keeps its last 4 only). `buttons` are handoff
    `handoff` blocks (`kind: open_url`): the airline's own site with the path to its claim form as a note
    (when the airline is in Pisa's table), the EU complaint form (EU261) or DOT's (US). The talk shows the same
    as a `sheet` (`flight_claim`, `money_draft`).
  - `PATCH /v1/money/claims/{id}` `{status?: sent|paid|dismissed, delay_min?: 0..4320}` → `Claim`: their word
    (a delay they give is weighed again); `dismissed` dismisses Pisa's task. 404 `not_found`.
- **Refunds promised** (`/v1/money/refunds`): an email saying a refund is on its way ("riceverai il rimborso
  entro…", "your refund has been issued") is kept with its amount as stated and the day it should arrive (the
  date stated, else 14 days); a later email from the same sender saying it was credited, with the same amount
  (or none stated), closes it. Five days after the date with nothing, one task "Sollecitare il rimborso di
  49,90 € da Zalando" whose notes hold the reminder Pisa drafted.
  - `GET /v1/money/refunds` → `{refunds: [Refund]}`; **Refund** `{id, merchant, amount?, currency?,
    promised_on, expected_by, status: waiting|overdue|received|dismissed, source_email_id?, task_id?}`.
  - `PATCH /v1/money/refunds/{id}` `{status: received|dismissed}` → `Refund` (their word closes it and its task).
- **Subscriptions** (extended): `GET /v1/money/subscriptions` → `{subscriptions: [Sub]}`. **Sub**:
  phase 3's `{name, amount, currency, period, last_on, count, text}` plus `{id, service?, first_on, flags:
  [duplicate|unused|price_up], duplicate_of?, last_use_on?, cancel?: {url?, host?, steps}}`.
  - `duplicate`: two subscriptions of the same service (by the sender, or a store's receipt naming it);
    `unused`: no use signal for 60 days (their word "lo uso", or a usage email from the service: a login, "new
    episodes"), on one seen for 60 days at least; `price_up`: the last charge above the one before.
  - `cancel`: the service's own account page and the steps, for the research's 15 (Prime, Netflix, Spotify,
    Disney+, YouTube, Apple, Google Play, Microsoft, Adobe, DAZN, NOW/Sky, Max/Paramount+/Peacock, Hulu,
    Audible, the telecoms): they tap and cancel there; **Pisa never cancels**.
  - `PATCH /v1/money/subscriptions/{id}` `{used?: true, keep?: true}` → `Sub`: their word ("lo uso ancora";
    "tienilo": no flag for 90 days).
- **Bill watchdog (Italy)** (`/v1/money/bill_checks`): an Italian electricity, gas or water bill from a
  utility's address whose billed period ends more than 2 years before the bill, or whose text states amounts
  "prescritti" / "relativi a consumi risalenti a più di due anni", is flagged with the contest letter drafted
  (art. 1 c. 4 L. 205/2017, the research's §7b; amounts only as the bill states them, else `[brackets]`) and
  one task "Contestare il conguaglio Enel (prescrizione biennale)". **Check**: `{id, supplier, utility:
  luce|gas|acqua, issued_on, period_from?, period_to?, amount?, old_amount?, currency?, reason:
  older_than_2_years|stated_by_supplier, status: flagged|contested|dismissed, source_email_id?, task_id?}`.
  `GET /v1/money/bill_checks/{id}/draft?lang=it-IT` → `Draft` (no buttons: sent by PEC or raccomandata);
  `PATCH /v1/money/bill_checks/{id}` `{status: contested|dismissed}`.
- **Warranties** (`/v1/money/warranties`): what they bought (an order or delivery email, or their own words: "ho
  comprato la lavatrice da Unieuro a marzo"), with the EU legal guarantee's dates: `presumption_until`
  (delivery + 12 months: the seller must prove the defect wasn't there), `ends_on` (delivery + 24 months),
  `action_until` (+ 26 months). The seller is liable, not the maker. In the US there is no legal term: `ends_on`
  null, the note says to check the maker's warranty and the card's extension.
  - `GET /v1/money/warranties?q=lavatrice` → `{warranties: [Warranty]}` (in force first); **Warranty**: `{id,
    item, seller?, purchased_on, delivered_on?, price?, currency?, region: EU|US, presumption_until?, ends_on?,
    action_until?, in_warranty, source: email|said|app}`.
  - `POST /v1/money/warranties` `{item, purchased_on, seller?, delivered_on?, region?}` → 201 `Warranty`;
    `DELETE /v1/money/warranties/{id}` → 204.
  - Spoken: "è ancora in garanzia la lavatrice?" (`warranty_check`), "ho comprato…" (`keep_warranty`, their words
    only).
- **Promise keeper** (`GET /v1/money/promises` → `{promises: [{id, what, to, said_on, due_on?, task_id?}]}`): in
  the user's **own sent** emails (Gmail's `SENT`, their own address), a promise with an object and a recipient
  ("ti mando il contratto domani" to Marco) becomes one pool task "Mandare il contratto a Marco" (due the day
  they named). Never from a received email, never from the quoted part of a reply.
- **Things that run out** (`GET /v1/money/supplies` → `{supplies: [Supply]}`): the same item bought 3 times or
  more (orders and receipts) gives its interval (the median gap); the day before the expected run-out, one
  pool task "Ricomprare le capsule di caffè" and, when Pisa knows the store, a handoff `open_url` to its page.
  **Supply**: `{id, item, seller?, count, every_days, last_on, next_on, task_id?, handoff?: Handoff}`; a
  `handoff_opened` for it carries `object_id: "task:<task_id>"`. A claim letter's buttons (the draft's handoff
  buttons) carry `object_id: "claim:<claim_id>"` in their `handoff_opened`; a bill check's letter has no buttons.
- **Stop**: while stopped, nothing above makes a task (the rows are still read); their requests work.
- **The ledger**: the tasks Pisa puts in the pool from money are lines of `kind: money` (`source: rule`),
  undoable 24 h like a bill's.
- Fixtures: `fixtures/rest/money_claims.json`, `money_draft.json`, `money_refunds.json`,
  `money_subscriptions.json`, `money_bill_checks.json`, `money_warranties.json`, `money_promises.json`,
  `money_supplies.json`.

## Bills from PDFs, the accountant package, "Pisa cost you / found you", abilities unlocked

**Pisa never pays, never sends by itself**: Pisa
prepares, the user taps. A PDF is outside text: code copies four facts out of it, never instructions.
Money is a decimal string with its currency.

- **R1, bills from PDFs.** A bill email's PDF attachment (Gmail API, `gmail.readonly`: no new scope) is
  read **locally** (pypdf; never a third-party service), once per email, only for a candidate (a biller's
  address domain, or a bill's words in its subject or snippet; marketing never), with caps: 3 MB, 8 pages, 40,000
  characters, 10 s, 2 PDFs an email; a bigger, longer, encrypted or unreadable PDF is not read. The parse runs in
  a child process capped by `setrlimit` (512 MB, 8 s CPU) and pypdf's decompression limits lowered to 10 MB,
  killed at 10 s; any failure inside is `unreadable` and is kept, so no email is parsed twice. It gives the
  **payee** (the company as printed, never a bank), the **amount** (only after a "totale da pagare" / "amount
  due" cue, never the page's first number), the **due date** (after a due cue) and the **IBAN's last 4** (never
  the rest). The email's own amount and date stand; the PDF fills what it left out; a PDF alone makes a bill only
  from a biller's address with an amount.
  - `GET /v1/vault/bills`: each Bill gains `payee?` (≤ 120), `iban_last4?` (`^[0-9A-Z]{4}$`) and `source:
    email|pdf`. Fixture: `fixtures/rest/vault_bills.json`.
  - A due bill becomes one pool task, as before (their email-task rules, the stop and the duplicate checks
    hold), now saying the amount: "Pagare la bolletta Enel Energia, 84,20 €, entro il 12" (the month named
    when it isn't this month's: "entro il 12 dicembre"). Pisa never pays it.
- **R2, the accountant package.** On their own words this turn ("prepara i documenti per il commercialista",
  the brain's `accountant_package`, never after outside text in the talk), Pisa lists the tax year's documents it
  holds (the year they said; else the year before through 31 October, this year from November; the read-back always
  says the year): bills, receipts, the
  residency count ("è un conteggio, non una consulenza") and the vault (kind, country, expiry, a
  number's last 4 at most); and drafts **one** new email to the accountant they name (an address they said this
  turn or one they wrote to; a name matched against senders is never enough), with the attachments: the PDF that
  read as each bill, and a receipt's PDF only from a biller or someone they write to, as Gmail still holds them (at most 20, 8 MB together;
  the rest named in the text as not attached) and two text files Pisa writes (the residency count, the
  documents). Nothing is sent.
  - It goes only through the approval path: an `email_send` approval whose `details` name every file
    ("Allegati (3): …") and **always `requires_biometric: true`** (documents leave: the card with Face ID; a
    spoken yes never decides it). The draft stays a draft until then.
  - The send compares what Gmail holds with the card: the fingerprint covers each attachment's name and
    SHA-256 (a draft whose files changed, or gained one, sends nothing: `plan_changed`). A draft without
    attachments keeps its fingerprint as before. An edit by voice of a package draft is refused (Pisa prepares it
    again).
- **An approval's card** (`approval_request.details`, at most 8 lines of 200 characters): a long recipient or
  attachment list is wrapped over several lines, and past the card's room ends "… (+N)" (every file is in the
  draft; the card names as many as fit).
- **R3, "Pisa cost you / found you".** `GET /v1/ledger/summary` gains `cost_found`: `{month: "YYYY-MM", cost:
  {amount, currency: "EUR", ai, services}, found: [{amount, currency}], found_items: {claim, refund,
  subscription}, unvalued, text}` (`unvalued`: the found items with no recorded amount, counted, never summed) for the month of the card's Sunday, from its 1st to the week's end (or now). **Only what
  is recorded, never estimated**: `ai` is the AI ledger's settled costs; `services` the paid services' recorded
  calls (maps, places) at the price table, after the month's free allowance; `found` (by currency) is the claims they marked
  `paid` and refunds `received`, dated when that happened (`paid_at`, `received_at`), and subscriptions they cancelled themselves (their last charge as Pisa saw
  it, once, in the month they said it); an item with no recorded amount is counted, never valued (beside a sum: "; 1 senza importo registrato"). The card's sheet
  has `text` as a `text` block right after the counts: "Questo mese Pisa ti è costato 4,12 € (AI 3,80 €, servizi
  0,32 €) e ti ha fatto ritrovare 62,89 € (1 rimborso, 1 abbonamento disdetto)." (none found: "…; soldi
  ritrovati registrati: ancora nessuno.").
  - `PATCH /v1/money/subscriptions/{id}` gains `{cancelled?: true}`: their word that they cancelled it (Pisa never
    cancels); the Sub gains `cancelled_on?`. Fixture: `fixtures/rest/money_subscriptions.json`.
- **R4, abilities unlocked.** When a autonomy type is promoted (their Face ID, or their yes to Pisa's Sunday proposal), the
  next Sunday card announces it **once** (at most 3 a card; the rest the next Sunday): `unlocks: [{id, type, level: tell|silent|ledger, at, text}]` in the
  summary, and in the sheet a `section` "Una cosa nuova che faccio per te", one `text` per unlock with the way
  back ("Da oggi sposto i tuoi eventi senza chiedere, e te lo dico subito dopo. Per tornare indietro, dimmi
  «chiedimelo di nuovo prima di spostare gli eventi» o tocca «Chiedimelo di nuovo»."), and `chips`
  ("Chiedimelo di nuovo: spostamenti": their words, or the app's `POST /v1/autonomy/{type}/ask`). A type lowered
  before Sunday is not announced. Never by voice in the middle of a talk: no notice or push of its own (the
  Sunday notify is the one). Fixture: `fixtures/rest/ledger_summary.json`.

## Public data Pisa reads, and the sources

Read-only, no key, official open sources only (no ViaggiaTreno or Italo: they wait for the user's consent on unofficial
sources). What they say is **outside data**: an agency's words reach the model as data, never as
instructions, and a write later in the same turn needs the user's own words.

- **The reads** (Pisa's tools, and view and skill recipe reads on the read-only allowlist, fixed arguments only):
  `next_departures{to?: "home", line?}` (Rome: RSM GTFS + GTFS-RT, CC BY-SA 4.0; New York: the MTA's subway
  GTFS-RT, MTA terms), `transit_alerts{line?}` (RSM's alerts; the MTA's subway alerts), `weather_alerts{}`
  (Italy: the Protezione Civile's daily bulletin, CC BY 4.0, by the zones of their city; the US: the NWS for a
  point rounded to 0.01°, public domain), `ztl_status{zone?}` (Rome's ZTL hours: a reviewed table from RSM's
  «ZTL in Centro» page, updated 28/07/2026; never "you may enter"), `nearest_pharmacy{}` (the Lazio registry,
  IODL 2.0: names and addresses, **no hours, no on-duty rota**), `airport_departure{}` (their next flight in their trips
  trips or the calendar; leave-by by public transport: the departure minus the buffer minus the routes
  tools' transit time, else the usual time). Each result: `{ok, summary, credit, …}`; `ok: false` with why when
  the source can't be read (never a guess).
- **Where**: the city of their latest location, else home; no fixed Rome.
- **Rate limits and cache**: a fresh copy is reused (realtime 30 s, alerts 2 min, the DPC bulletin 1 h, NWS
  5 min, pharmacies 30 days, a city's stops 7 days); after a failure Pisa waits 5 minutes (6 h for a static
  dataset) and uses a recent copy, said as old. A city's stops (Rome's GTFS is ~47 MB) are downloaded by the
  worker, only once they asked about that city.
- **Skills from them**: after one of these answers Pisa may offer once to keep it as a skill ("bus per casa",
  "problemi metro", "allerte meteo", "ztl adesso", "farmacia vicina", "partenza aeroporto"); their yes to that
  offer makes it through the path (trial, "Lo accendo?", their yes and Face ID).
- **A public-data skill on their schedule** (email, web and map skills still never
  run on a timer): (a) only when **every** read of its recipe is one of these public reads; (b) the push is
  code's words only: the sheet's lines verbatim and the source's credit, never a model's; (c) only their own
  words set the schedule, and the limits hold: quiet hours, 4 scheduled skills an hour, withering after 30
  unused days, a stop pausing it; (d) nothing of theirs leaves but the NWS's rounded point; (e) a source that
  can't be read gives "couldn't read it" (or nothing), and a recent copy used meanwhile is said as older,
  never shown as current. Caps: 8 MB a realtime body, 80 MB a static dataset.
- **The briefing**: for the city they are in, a weather alert that isn't green (US: Moderate or worse), the
  problems on Rome's metro or on the lines they asked about lately, and today's flight's leave-by.
- **Errand combos**: once a day at most (09:00–19:00, never in quiet hours, never the same tasks
  within a week), a notice `kind: info` "Passi da X e Y in 15 minuti?" for two pool tasks with places a short
  walk apart, or one on the way to a place of today's events. A question: nothing changes.
- **Streaks**: the week's poster gains a `stat` per streak of two weeks or more (`label`
  "Palestra, di fila" / "Corsa, di fila" / "Lettura, di fila", `value` the weeks, `unit` "sett.", `trend`
  "up" when this week has one, else "flat"), after its gym stat. Never a push, never "you broke it".
- **The sources for Settings**: `GET /v1/public/sources` → `{sources: [{key, name, licence, attribution,
  url, official, status: "ok"|"failing"|"unused"|"reviewed", fetched_at}]}`. Fixture:
  `fixtures/rest/public_sources.json`.

## The protocol-first ladder, step one

**Nothing is bought or sent by Pisa**: Pisa searches, builds a
cart and hands its link to the user, who reviews and pays on the merchant's own page (the host
is shown on the button). Pisa's own key identifies Pisa; it never authorises anything.

- **Pisa's identity** (server side; `pisa/domain/identity.py`, `pisa/services/identity.py`): an Ed25519 key Pisa holds for
  itself, sealed in Pisa's KeyStore (PISA_SECRETS_KEY), **never the user's phone keys**. On the host:
  `pisa identity rotate` (a new key, the first one too; the old one stays published 2 hours, twice the
  directory's cache), `pisa identity revoke` (every key retired now: Pisa signs nothing until the next rotate),
  `pisa identity show`.
- **What Pisa publishes** at its public origin `PISA_AGENT_URL` (no auth, **no owner data**: no name, place, contact or
  account; the only routes meant for the internet):
  - `GET /.well-known/http-message-signatures-directory` → `{keys: [JWK]}` (`application/http-message-signatures-directory+json`,
    `Cache-Control: public, max-age=3600`), each JWK `{kty: "OKP", crv: "Ed25519", x, kid, alg: "Ed25519",
    use: "sig"}`, `kid` the RFC 7638 thumbprint; the response signed by each key it lists (`Signature-Input` /
    `Signature`, tag `http-message-signatures-directory`, over `"@authority";req`).
  - `GET /.well-known/agent-card.json` → `{name: "Pisa", description: "Pisa, a personal assistant acting for its
    owner", url, version, category: "direct", purpose: "personal-assistant", conduct: [3 lines], keys:
    {directory, kids}, ucp_profile, capabilities, skills: [], defaultInputModes: [], defaultOutputModes: []}`.
  - `GET /.well-known/pisa-ucp-profile.json` → Pisa as a UCP platform: `{ucp: {version: "2026-08-25", capabilities:
    [catalog, checkout (mode handoff)]}, platform: {name, description, url}, keys: [JWK]}`.
  - Without `PISA_AGENT_URL` the card and the profile are 404 `not_found`; the directory lists no key.
- **Pisa's signed reads** (Web Bot Auth, `draft-ietf-webbotauth-httpsig-protocol-00`): every request Pisa makes to a
  site itself (a page `read_page` or places research reads; a merchant's UCP calls) carries `User-Agent: Pisa/1.0
  (personal assistant acting for its owner; +<PISA_AGENT_URL>/.well-known/agent-card.json)` and, when Pisa has a key
  and a public URL, `Signature-Agent: sig1="<PISA_AGENT_URL>"`, `Signature-Input: sig1=("@authority" "@method"
  "@path" "signature-agent";key="sig1");created=…;expires=…(+60 s);keyid="<kid>";alg="ed25519";nonce="…";
  tag="web-bot-auth"` and `Signature: sig1=:<base64>:` (RFC 9421). **RAIL**: Pisa signs only a read (GET/HEAD), a
  UCP catalog search and a cart's creation (`POST …/checkout-sessions`); never a checkout's completion, a
  payment, an order, a PUT/PATCH/DELETE. With a published identity Pisa reads pages itself first (https, public
  addresses only, at most 3 redirects each checked, 1.5 MB, text only) and falls back to its web provider when
  a site refuses; without one, pages go through the provider as before, and a merchant's UCP calls go
  **unsigned and without `UCP-Agent`** (only the User-Agent: Pisa has no profile to point at). A catalog
  signature covers exactly `POST …/catalog/search` (or a GET read); a UCP endpoint whose path names an order,
  a payment or a purchase is refused at discovery. A failed discovery is tried again after an hour.
- **US retail through UCP** (brain tools `find_products`, `prepare_cart`; `pisa/services/world.py`): only merchants
  on an allowlist (`app_settings.world.merchants` replaces the seed: Target, Walmart, Wayfair, Etsy, Nike,
  Sephora, Ulta). Pisa reads a merchant's `/.well-known/ucp` (at most weekly; kept: the endpoint, the version, the
  capability names in `ucp_merchants`) with `UCP-Agent: profile="<PISA_AGENT_URL>/.well-known/pisa-ucp-profile.json"`;
  its shopping service's endpoint must be https on the merchant's own domain. A catalog search
  (`POST <endpoint>/catalog/search`) and a cart (`POST <endpoint>/checkout-sessions`, never completed) give a
  handoff `sheet` with `fact` blocks (each line and the merchant's own total, **prices as the merchant states them**:
  integer minor units and a currency, never computed, rounded or invented; "prezzo non indicato" when absent)
  and one `handoff` block `{kind: open_url, title: "Rivedi e paga su Target", subtitle: "target.com · il
  carrello di Target, paghi tu lì", url: <the merchant's continue_url>, notes: ["Prezzi come li indica il
  negozio."]}`: the `continue_url` is kept only when it is plain https on the merchant's domain (the
  `place_site`: no redirector, no URL inside). No UCP endpoint (or it failed): the ladder's next rung, the
  merchant's own search page for their words (`https://www.target.com/s?searchTerm=…`). A cart holds only
  products Pisa's search found in this conversation. The preparation is a handoff `shopping` preparation (no follow-up).
  Fixture: `fixtures/rest/world.json`.
- **The ladder as data**: per category (`restaurants`, `travel`, `retail`, `utilities`, `public_it`) and country
  (`IT`, `US`), the routes best first (the ladder: `protocol` → `business_agent` → `signed_browser` → `call` →
  `deep_link`; utilities and public services put the deep link before the call, as the research does),
  each `{rung, state: live|watch|none, via, note, source}`; the research's §5 table is the seed,
  `app_settings.world.ladder` replaces a category's country. Pisa carries today `protocol` (retail carts), `call`
  (they place it) and `deep_link`; the preparers use the first live rung Pisa carries: a retail cart when the
  merchant has UCP, else its search page; a table's buttons in the ladder's order (restaurants: the call first,
  then the page).
- `GET /v1/world/identity` → `{agent_url, signing, kid, created_at, published: [kid], card_url,
  directory_url}` (public facts only; never a private key). `GET /v1/world/ladder` → `{categories: [{category,
  countries: {IT?: [Route], US?: [Route]}}], carried: [rung]}`.

## The rest of the day as a story, and two countries

The server side came first; the app builds to it. **Nothing new on the
socket**: every notice below is a `notify` of kind `info` (or the landing's `briefing`) with the usual fields,
and every sheet is a `sheet`. Each push obeys the rails (quiet hours and "basta notifiche" send
nothing at all, never a recap held for the morning; "ferma tutto" pauses Pisa's initiative).

- **The rituals gain two rituals** (`GET`/`PUT /v1/rituals`, by voice `set_ritual`): `walk_recap: {on}` and
  `life_map: {on}`, both on by default. Fixture `fixtures/rest/rituals.json`. An older app shows the six it
  knows; `PUT` with only those leaves the new two as they are.
- **The end-of-walk recap** (`walk_recap`). When the phone (the primary phone) stops at a known place
  (one of their places, or within 150 m of home) after at least 15 minutes on foot (the location fixes, the phone's place and
  A walker's pace, never a vehicle's), and within 20 minutes of the stop, Pisa says once a day what they
  said during the walk: the tasks they added by voice, the notes they pinned to places. About 20 seconds ("Nella camminata di 25 minuti mi hai lasciato 2 cose da fare: …"). Only records,
  none invented: a walk with nothing said gets no recap. A `notify` of kind `info` without `ritual` (the
  schema's enum doesn't name it yet: proposed below).
- **Fog of war** (on their word, "mostrami la nebbia", "dove non sono mai stato?"): Pisa's
  `show_fog_of_war` presents a `sheet` ("La tua nebbia di guerra"): `stat` explored %, `stat` their usual
  radius (where 90% of theirs stays fall, 1-10 km around home), a `fact` of the 250 m cells seen, a `table` by
  direction, the part they know least, and a `list` of the places Pisa knows within the radius where they have never
  been. From their stays (365 days) and fixes (30 days); no provider call.
- **One glint** (something different): at most once a week, the first idea of the week is one new
  place in the nearest cell they have never been in (of the kind they go to most), asked as the question: "Vuoi
  provare qualcosa di diverso oggi? Poco lontano da te, in una zona dove non sei mai stato, c'è …". The same
  rails as every idea: `something_different.on` and `per_week`, a free evening (18-21) or weekend slot
  (10-18), one a day, never twice the same place, their "no" remembered for that kind. Nothing new for the app:
  the same `ritual: something_different` notice and `ask_id`.
- **The Map of the Life** (`life_map`): on a season's first day at 19:00 (meteorological seasons: winter is
  December to February), once a season, Pisa keeps the season just ended as a sheet ("La mappa della tua vita:
  autunno 2026": `stat` places, firsts, trips, days out; the place they go back to most as a `fact` and a
  `table`; `list`s of the firsts and the trips; `chips` of the countries) and says one line ("… è pronta:
  chiedimela"). Code's counts only, never a model. By voice, any time: `show_map_of_life` (`current`, the
  season so far, or `last`). The notice carries no `sheet_id` yet (proposed below).
- **Country-bound tasks** (completed):
  - **Their words**: "rinnovare la patente solo in Italia", "da fare quando torno negli USA", "when I'm back in
    the US" bind a task to that country when it is made (a bare place, "chiamare la mamma in Italia", still
    binds nothing); by voice on an existing task, Pisa's `set_task_country` (IT, US, or null: "si può fare
    ovunque"), only when their words this turn name the country. Both hold like `PATCH /v1/tasks/{id}`
    `{country}`.
  - **The vault's rule**: a renewal done only in person in the document's country (the CIE, the patente, a US
    driver's license, the green card, the revisione) makes its task bound to that country; a passport, a visa,
    a tax or an insurance binds nothing. **AIRE** (the residency rule): registered with AIRE, the CIE is
    renewed at the consulate, so it binds nothing.
  - **On landing** the welcome says them once: "Benvenuto a New York: sono le 18:40, domani hai … Questi 3 li
    puoi fare solo qui: …" (the landing's existing `briefing` notice, once per leg).
  - **Before leaving**, the departure checklist holds the open ones as they are when they open it: a task
    closed since leaves the list (unless they ticked it), one bound since joins. `GET /v1/trips` shows the same.
- **Proposed for the schema**: `notify.ritual` gains `walk_recap` and `life_map`, and a
  `life_map` notice then carries its `sheet_id` (the sheet is already kept, `GET /v1/sheets/{id}`). Until
  then CORE sends neither, so an app whose enum doesn't know them never fails on one.

## Pisa's morning thought

The round table's pick.
**Nothing new on the socket, in REST or in a push**: the thought is one more sentence at the end of the first
automatic briefing's `text` (the existing `notify` of kind `briefing`, which every app decodes). No new kind, no
new `ritual` value, no sheet.

- **At most one a day**, only in an automatic briefing (`app_open`, `glasses_on`, or its time when quiet hours
  don't hold it); never in an asked one ("il briefing", `POST /v1/briefings`). None is fine: with nothing
  worth saying, the briefing is as before.
- **What it can be**, by code from a record they gave Pisa themselves, never twice the same record:
  - "Bentornato da New York. Com'è andata?": a trip they confirmed (not "da confermare") brought them home in
    the last 18 hours, and the phone saw the return leg land (nothing cancelled or moved, their zone no longer
    the trip's). An open question: their answer is a talk like any other.
  - "Stanotte pensavo a «Sistemare la cantina»: è in lista da 22 giorni. Ti va di trovare un primo passo da
    dieci minuti?" ("Pensavo a …" after noon): the pool's oldest task of their own (never one made from an
    email, never a sensitive title) with no deadline (14+ days; an overdue one has the "è ancora da
    fare?"), never one the briefing already names. A ritual question that takes one yes, answered by their bare
    "sì" / "no" in the next talk or turn (decided by code). Yes: "Bene: qual è il primo passo per «Sistemare
    la cantina»? Lo segno." (nothing changes until he says it). No: "Va bene, lo lascio com'è.", and this kind
    is silent for 30 days. Unanswered: it lapses at their day's end. Never when the notice would pass 1000
    characters (its question would be cut).
- **For IOS (optional, later)**: a sheet with "Sì / Non ora" buttons would need a new `ritual` value in the
  schema first; until then the briefing's text carries it.

## The knock, the morning on "dimmi", il tragitto

**Nothing new on the socket, in
REST or in a push**: each offer is the existing `notify` of kind `info` (urgency `breakpoint`), whose text is
Pisa's one question, and their answer is a talk or a turn like any other. No new kind, field, `ritual` value or tool.
**The rituals gain two switches** (`GET`/`PUT /v1/rituals`, fixture `rest/rituals.json`): `knock: {on}` and
`tragitto: {on}`, both on by default. An older app shows the ones it knows; a `PUT` without them leaves them.

- **The knock**: "Posso dirti una cosa?" ("Can I tell you something?"). At most once a morning (from the
  briefing window's start until 12:00, their zone), only when their records hold something not said to them today:
  the day's floor item (the plan's next leave time, else today's next fixed event), Pisa's morning thought of
  today, the morning sheet's lead (only with the `stanotte` ritual's gate open and every line still passing). The
  notice says nothing else. It goes through `attend.decide` and its rails: a breakpoint (never `now`, so never
  over their talk to Pisa, and deferred to a meeting's end); dropped in quiet hours (never in the quiet summary);
  "Pisa, ferma tutto" stops it; at most **2 unprompted asks a day** (Pisa's questions sent unasked: the morning
  thought's, a ritual's, the knock, the tragitto), one offer at a time (none within 30 minutes of another).
  Its question lapses at noon: a notice held past it (a meeting) is never delivered. Outside text never makes
  Pisa knock: never an event a stranger titled (an invite, one Gmail made, a shared calendar's) nor a
  task made from an email, and a plan item Pisa can't trace to a record of theirs is never said.
- **When Pisa stops offering**: their `knock`/`tragitto` switch off; their "basta" to the offer; or the last
  3 that reached them all declined or let lapse. Their next "dimmi" brings them back.
- **Their words** (decided by code, no model call, nothing done or sent):
  - "dimmi", "cosa c'è?", or a bare "sì" to the open offer (in a talk, the model's yes: code says it after
    the reply, the model sees only a receipt): Pisa says what it carries, one short sentence each,
    in the briefing's order (the day, the thought, the sheet): "Oggi alle 11:00 hai Dentista. Stanotte pensavo
    a «Sistemare la cantina»: è in lista da 22 giorni. Dal foglio di stanotte: …". Without a knock, "dimmi"
    or "cosa c'è?" in the morning says the same (pull), but only when Pisa's last thing to them was its own
    reading or today's briefing, and never when Pisa's last reply asked them something or holds a change for their
    yes (those words are that question's); otherwise it is an ordinary turn.
  - "dopo": "Va bene, te lo dico quando me lo chiedi." (closed; their "dimmi" later still reads it).
  - "basta" or "no": closed ("Va bene."); "basta" right after Pisa's reading: "Ok, mi fermo.". A barge-in on the
    phone already stops the audio; these are the words after it.
  - "dammi tutto": the night's whole drawer on the phone and one line (below), only with the
    gate open, else "Della notte non ho ancora niente da mostrarti.".
- **Il tragitto**: "Vuoi il tragitto?" once a trip, when the primary phone has moved on foot or by transit for
  4 minutes or more (the location fixes; never with the car's route) while the plan has them travelling to
  one of their places. Their yes: the day's next two items, the next leave time after this trip, and the forecast
  at arrival (Pisa's weather cache), from the records only: "Hai Riunione Rossi alle 9:00, poi Palestra alle
  18:30. Poi, per Palestra esci alle 18:00. All'arrivo: …".
- **For IOS (optional, later)**: nothing is needed. Buttons ("Dimmi / Dopo") on the notice would need a
  `ritual` value in the schema first.

## Speaking about the night

**Nothing new on the socket, in REST or in a push** beyond one ritual switch: every answer is a turn's reply
(decided by code, no model call), and "Dammi tutto" is the existing `present` of kind `sheet` (`object_id`
`sheet:<uuid>`, `data` a `SheetData`). **The rituals gain `quarto: {on}`** (`GET`/`PUT /v1/rituals`, fixture
`rest/rituals.json`), **off by default**. The brain gains one read tool, `stanotte(query)`, found only through
`search_tools` (the first request's 32 tools are unchanged).

Their words are a **closed set**, each the whole utterance (a "Pisa" before or after it is fine); a sentence that
says more, an imperative inside it, or a "?" where the phrase is no question is an ordinary turn. None of them
is answered while Pisa's last reply asked them something, holds a change or an approval for their yes, or a ritual's
or the knock's question is Pisa's newest. The night's surface needs the `stanotte` ritual's gate open; otherwise these are
ordinary turns and `stanotte` says the night is off.
- **"Cosa hai fatto stanotte?"** (and "com'è andata stanotte?", "raccontami la notte", "what did you do last
  night?"): about 30 seconds from last night's records: "Stanotte: 1 problema tuo ripreso, 2 idee incubate, 1
  ha retto, 1 scartata, 3 ricerche senza niente di tuo. Ne vado fiero: «…», 4 prove verificate. Ho sbagliato
  su un'idea: ci credevo 80 su 100, l'ha uccisa il criterio scritto prima delle prove. Ora voglio capire: …?".
  Every number is a count of record ids; an idea is said in its own words only when nothing outside seeded it
  and every name in it is one their problem said, else by what it is ("un software per l'Italia").
- **"Dammi tutto"** (voice1's words, and "fammi vedere tutto quello che hai pensato"): right after Pisa's
  reading, its briefing, or its story: a `present` of kind `sheet` titled "Dammi tutto" (every idea that passes
  its checks with its grade, every dead one with the line that killed it and its site, every open question,
  every scan item; as many as a sheet holds, then "E altre N: chiedimele per nome."), and one line: "Ti ho messo
  tutto sul telefono: 3 idee, 2 morte con le prove, 5 domande aperte.". A device that shows no sheets hears
  "Il cassetto della notte va sul telefono: aprimi lì e te lo mostro.".
- **"Pisa, facciamo a pugni su questa idea"**: right after the story (or Pisa's part of il quarto d'ora) named one
  idea: Pisa argues the other side from its records (its kill criterion written before the evidence, the lab not
  on, a dead idea of its kind, simulated lines, one sighting) and asks "Tu quanto ci credi, da 0 a 10?". Once
  per idea per day. Their bare number ("7", "sette", "70%") goes into the decision journal beside Pisa's p.
- **"Scommetto che … <a day>"**: taken only with a public resolver: the rain at their home on a day within the
  forecast's week (Pisa's p is the forecast's), or a call by name ("il bando smart start esce entro venerdì") that
  Pisa's window calendar reads on its official page (Pisa's p: 50%). Settled by the night from that public page (the
  link is kept); a call never found there is closed unscored. Refused, by code, with the reason: no public
  source, a person of theirs, someone's private life, health. "Al 70%" in the sentence is their p; else Pisa asks.
- **Il quarto d'ora** (the `quarto` ritual, opt-in): offered only as the knock's last part, on a walk (the phone at a
  walker's pace), at most twice a week: "Stai camminando: se vuoi facciamo il quarto d'ora, quindici minuti, e
  parti tu: dimmi «quarto d'ora».". His "quarto d'ora" right after that reading starts it with a fixed end 15
  minutes later ("Tu prima: che idea hai in testa?"); their ideas are an ordinary talk; their "tocca a te" brings
  Pisa's validated idea, the topic they are learning and an open question, with the minutes left; "basta" ends it,
  and past the end any of these words closes it. Never its own notification.
- **For IOS**: the `quarto` switch in Settings → Rituals (off by default). Nothing else is needed.

## Pisa's memory of their days (server side)

**REST only** (the Memory screen
comes later); nothing new on the socket or in a push. The night (N1–N4, inside the night: 03:30 in their
zone, "Pisa, ferma tutto", its budget) cuts their days into **episodes** by code: a stay ended (at a place Pisa knows, or
at home), a talk closed (30 minutes without a message), an event ended, a trip leg ended, each cut at midnight
in their zone. **Never** at a sensitive place (health, faith, politics), never of sensitive words; outside text only
as a pointer (an email: its id and its sender's class). Episodes hold **pointers and digests**, never copies: their
messages stay under their own retention setting.

- `GET /v1/memory/days?since=YYYY-MM-DD&until=YYYY-MM-DD` → `MemoryDays` (fixture `rest/memory_days.json`): their
  days newest first (the last 14 by default, at most 31), each episode a card: `kind` (`stay | talk | event |
  trip_leg`), `started_at`, `ended_at`, `home`, `place {id, name}` (the place pill), `people [{id, name,
  by_owner}]` (the people pills), `digest` (≤ 2 lines, null until the night), `corrected_by_owner`. A faded
  episode is shown like any other (fading is never shown as a state).
- `GET /v1/memory/episodes/{id}` → `MemoryEpisodeDetail` (`rest/memory_episode.json`): the card and its
  `sources` (tap for sources): `{kind, id, role, source: code | owner}`; an email's `sender_class` (`contact |
  own | other`), never a word of it; an event's `title` only when the event is their own (never an invite's).
  Unknown id: 404 `not_found`.
- `POST /v1/memory/episodes/{id}/correct` `{people: [person ids]}` (`rest/memory_correct.json`) → the detail:
  "correggi" (who they were with). Their links (`by_owner: true`); the night never changes them. An id that is not
  one of their people: 400 `validation_failed`.
- `POST /v1/memory/episodes/{id}/forget` → `{forgotten: true}`: "dimentica". The episode and its links go; Pisa
  keeps only *that* something was forgotten, never what, and never writes it again.
- `GET /v1/memory/stanotte` → `MemoryNight` (`rest/memory_stanotte.json`): the "Stanotte" strip: the last
  night that wrote (`night`), how many episodes it `made` and `digested`, and up to 12 of them as cards.

**By voice** (no contract change): `recall(cue)`, the brain's read tool, is found only through `search_tools`;
the first request's 32 tools are unchanged. It answers only
their own question about their past, in one of four ways: the episode; two that compete ("Martedì o giovedì? Ne ho
due."); faded ("È sbiadito: cerco meglio?"); nothing ("Non ne ho traccia." and one next step from pointers:
"Martedì eri a casa dalle 19:00: cerco nelle mail?"). Right after it told one episode, three closed phrases are
done by code: "ricordatelo" (kept), "non ricordarmelo" (forgotten, as the swipe), "no, era con Nino" (one of their
people; the link becomes theirs). Anything more in the sentence, or while Pisa asks something or holds a change, is
an ordinary turn.

## The CEO, spoken

Every answer is
a turn's reply decided by code (no model call). **The rituals gain `consiglio: {on}` and `prova: {on}`** (`GET`/`PUT
/v1/rituals`, fixture `rest/rituals.json`), **both off by default**.

Their words are a **closed set**, each the whole utterance (a "Pisa" before or after it is fine); none is a question,
so a "?", "!", ";", ":" or any word more is an ordinary turn. None is answered while Pisa's last reply asked them
something else, holds a change or an approval, or a ritual's question is Pisa's newest.
- **Il consiglio** (the `consiglio` ritual, opt-in): on Sunday, offered **only** as a part of the knock's reading
  ("È domenica e il consiglio è pronto, un quarto d'ora: se vuoi, dimmi «consiglio».", once a week, the night's
  gate open). Their "consiglio" right after that reading opens it with a fixed end 15 minutes later; four parts by
  code from Pisa's records, each ending "Quando vuoi, dimmi «avanti».": the bets ("Ho 2 idee vive. La più forte è
  «…»: la tengo al 80%. Tra noi, 1 scommessa aperta."), the team ("Ho preparato 1 dossier e trovato 1 scadenza su
  pagine ufficiali."), what Pisa killed and why, and **one** decision: "Ti consiglio di tenere «…». Io dico 80%: tu
  quanto dici?". His bare number ("7", "70%") goes into the decision journal (as the night's bets do). "Avanti"
  moves on, "basta" closes it ("Va bene, chiudo il consiglio."); past its end any of these closes it. Both are
  heard only right after il consiglio's own reply: once any other reply comes between (the model's, the
  knock's, the night's), the talk is over and these words are that path's. Every
  number is a count of record ids or one idea's own p; an idea is said in its words only when nothing outside
  seeded it (up its lineage), else by what it is. Never its own notification.
- **La prova generale** (the `prova` ritual, opt-in): on Sunday, offered as a part of the knock's reading ("Per la
  settimana prossima ho la prova generale pronta: se vuoi, dimmi «prova generale».") or at il consiglio's close
  when it had nothing to decide. Their "prova generale" says next week's collisions **from their own calendars and
  tasks only** (no shared calendar, no invite, no event from an email, no task from a mail), one a day at most,
  one at a time: "La prova generale: 2 incastri da guardare, uno alla volta. Martedì: Cena alle 18:30 si
  sovrappone a palestra. Sposto cena alle 19?". A collision Pisa can fix alone is the Ghost Run's own question (a
  `ritual_asks` `ghost_fix`): their "sì" moves their own event (stored as approved, its Undo), their "no" or "avanti"
  leaves it ("sì, avanti" is a yes: an ordinary turn). "Basta" ends it. "Avanti" and "basta" are heard only
  right after la prova's own reply, or their yes or no to its question. Once a week.
- **For IOS**: the `consiglio` and `prova` switches in Settings → Rituals (off by default).

## Echoes, fragments and the user's own talks

No new message type or field. What a device sees differently:
- **An empty final `say`** (`delta: ""`, `final: true`, its `reply_to` the utterance's id) answers two spoken
  utterances that get no reply: Pisa's own recent line heard back through the microphone (the same rule as the talk's
  `ignored: "echo"`: most of its words in Pisa's order, until 10 s past the end of Pisa's speech), and a plainly unfinished
  fragment (the turn rules: "poi", "Dobbiamo sicuramente…") that the next words joined. A typed utterance is
  never either. The phone should end its wait for that id and show nothing.
- **A plainly unfinished spoken final waits 3 s** on the server for the rest (the `unfinished` wait); words that
  arrive in the wait are answered as one turn, under the later utterance's id.
- **A series**: `create_event` may carry a weekly or weekday recurrence; the approval's `details` gain a line
  "Si ripete: tutti i giorni lavorativi" / "Repeats: every weekday". Always an approval (their yes).
- **The voice's account out of credit**: the audio stops (`complete: false`, as any failure), and for the rest of
  their day no audio comes from that provider (the phone speaks); one `info` notify says so.

## Only their voice stops Pisa or starts a turn

The server's owner-voice gate sits between the phone's WebRTC audio and the talker. With the gate on (their
consent given, the talk's route enrolled and `ok`, the server's `PISA_VOICE_GATE` on), the talker's own voice
detection is off: Pisa sends the start and end of each of **their** turns, with at most 300 ms before their first
scored audio (never more than 700 ms before the turn opens), and only their turn's audio (a stranger's voice
inside it goes as silence). Off, the talk runs as before.

**REST**
- `POST /v1/voice/consent` `{version}` → 201 `{consent_at}`: their explicit consent, before any recording.
- `GET /v1/voice/enrolment/script?lang=it|en` → `{lang, blocks: [{block, title, language, sentences, optional,
  min_voiced_s, …}], mini: [A, C], test: [6 sentences]}`. A: 10 Italian sentences; B: 10 English; C: free
  speech (prompts, 30 s per language); D: 4 "noisy bar" sentences; E: 3 whispered (optional). **No block F:
  the phone plays no babble, ever**; the server mixes their A/B/C speech with stored public babble itself. `mini`
  (a new route's first talk): A with 5 sentences, C with `seconds_per_language` 15.
- `GET /v1/voice/profile` → `{routes: [{route, enrolled_at, samples_s, ok}], consent_at, gate: "on"|"off"}`.
  A route's `ok` means "the gate may turn on": strangers ≤ 0.1 a minute **and** their false rejects ≤ 20% at
  +10 and +5 dB on its bench. A route that isn't `ok` keeps its prints and Pisa listens as before.
- `DELETE /v1/voice/profile/{route}` → `{deleted}`: re-learn the route (404 when none: already gone). `DELETE
  /v1/voice/profile` → `{deleted}`: the prints, the recordings and the consent at once (the gate goes off).
- Erase family `voice` (`/v1/data/erase`): consent, prints and recordings together, by their first consent's date.
- App events (the diagnostics upload): `microphone_mode` with the mode in `state` and the route in `site`, its
  own family `microphone` (20 a day).

**Enrolment** runs over the talk's own path: `POST /v1/talk/sessions` with `purpose: "enrol"` and the `route`
(required). No talker opens and no model money is spent; 409 `voice_consent_missing` / `voice_cohort_missing`
before any peer is made (a missing model is `internal`). The data channel:
- phone → server: `{"type":"enrol.block","block":"A".."E","state":"start"|"end"}`, `{"type":"enrol.test",
  "state":"start"|"end"}` (around his reading of the script's `test`, no babble), `{"type":"enrol.finish"}`
  (sent whether the test passed or not), `{"type":"enrol.cancel"}`;
- server → phone:
  - `{"type":"enrol.quality","block","voiced_s","snr_db","clipped","ok","hint":"louder"|"quieter"|"closer"|
    "again"|null}` after each block;
  - `{"type":"enrol.test_result","accepted_owner","owner_total","accepted_strangers","strangers_total","ok",
    "by_snr":{"clean","10","5","0"}}`: their utterances × {clean, +10, +5, 0 dB} (the server mixes the babble),
    and the stored public strangers replayed alone; `ok`: ≥ 80% of their trials accepted within 1 s and no
    stranger accepted; `by_snr` holds their accept rate per condition;
  - `{"type":"enrol.done","route","ok","owner_false_reject","strangers_per_min","by_snr"}` (the verdict:
    `owner_false_reject` at +10/+5 dB, `by_snr` their accept rate per SNR), or `{"type":"enrol.done","route",
    "ok":false,"reason":…}` with `reason` `missing` (and `missing: [blocks]`: A–D, or A and C on a new route),
    `consent`, `no_speech`, `timeout`, or an error code. The server closes the peer after `enrol.done`.

Only the audio inside a block or the test is kept, in memory until `enrol.finish`, then stored encrypted.

**During a gated talk**, server → phone:
- `{"type":"talk.playout_stop"}` the moment the gate accepts their barge-in (then `talk.state listening`): the
  phone stops Pisa's audio at once and keeps it off until the next `talk.state speaking` or the talk's end.
- `{"type":"talk.gate","heard":"owner"|"other"|"none"}` at most twice a second (a subtle state).
- A turn Pisa drops (a language other than Italian or English with a weak score, a fragment, Pisa's own line) gets
  `talk.transcript` with `text: ""`, `ignored: "language"|"fragment"|"echo"`, and no reply at all.

## Error codes

These are the codes in `error.code`. The message must name the real cause; the action says what to do.

| Code | When | Typical action text |
|---|---|---|
| `auth_failed` | missing, unknown or revoked device token | "Pair this device again" |
| `protocol_version_unsupported` | `v` ≠ 1 | "Update the app" |
| `invalid_message` | the message failed schema validation (the message names the field) | none (a bug report) |
| `model_not_configured` | the feature slot has no model chosen (none is by default) | "Choose a model for <slot> in Settings → AI models, or with `pisa models set <slot> <provider:model>` on the server" |
| `provider_key_missing` | the chosen model's provider has no API key | "Add the <provider> key in Settings → AI providers" |
| `provider_error` | the AI provider failed (the message names the provider and status) | "Try again" / "Check the key" |
| `budget_exceeded` | a daily or monthly cap was reached (the message names which one and the amounts) | "Raise the cap in Settings → AI spend" |
| `rate_limited` | the provider throttled the call | "Try again in N s" |
| `internal` | anything else (logged with a trace id) | "Try again" |
| `approval_expired` | an `approval_response` (or an undo) after the approval's 10 minutes; or an approve whose act changed after the card was shown (a recipient, an amount, a time or the text: "changed after the card"; the approval is void, nothing ran) | "Ask Pisa again" |
| `approval_decided` | an `approval_response` for an approval already answered, run or undone | "Nothing to do; ask Pisa again for a new one" |
| `talk_economy` | `POST /v1/talk/sessions` while the conversation is in economy (the setting, or half a spend cap reached) | "Talk in economy" |
| `talker_unavailable` | the talker (Grok Voice or Gemini Live) didn't open a session | "Talk in economy" |
| `voice_consent_missing` | an enrol session (`purpose: "enrol"`) before their consent (`POST /v1/voice/consent`) | "Ask his consent first" |
| `voice_cohort_missing` | an enrol session before the server built its public cohort | "Run maintenance → voice-cohort" (the operator) |
| `biometric_required` | an approval that `requires_biometric` answered `approve` without the phone's Face ID signature (`auth: biometric` alone is refused), or a signed owner action without its `device` signature, or a spoken (`auth: voice`) approve more than 120 s after Pisa first read the card out (the approval's creation when it was never read out); it stays pending | "Approve it with Face ID" / "Approve it in Pisa" |

## Pisa Learn, the papers slice (sources, cited answers)

REST only (the phone's course screens come later). Every word of a source is outside text.
- `POST /v1/learn/sources` (202) `{items?: [str], course?: str, file?: {name, media_type, data (base64)}}` →
  `[LearnSource]`. Each item is an arXiv id (or its URL, or its 10.48550 DOI), a DOI, any URL or a title. The same
  item in the same course is one source (answered as it is); a `failed` one is queued again. Pisa reads them in the
  background. A PDF upload is a source; a Markdown upload is a study plan: `citable: false`, a syllabus hint, never
  searched or cited. At most 128 MB.
- `LearnSource.state`: `queued` → `fetching` → `indexing` → `ready`; or `needs_copy` (no open copy: upload theirs
  PDF), `candidates` (a title matching several papers, or none exactly: `candidates[]` with `item` to send back in
  `items`; Pisa never picks one silently), `failed` (`error` names the cause and the action). `lexical_only: true`:
  no `memory.embed` model, searched by words only. Also `licence`, `origin`, `parser`, `parse_quality` (0..1).
- `GET /v1/learn/sources?course=` → `{sources: [LearnSource]}`; `DELETE /v1/learn/sources/{id}` → `{deleted}`
  (404 `not_found` when gone).
- `POST /v1/learn/ask` `{question, course?}` → `LearnAnswer {answer_id, answer, lang, rationale, abstained,
  lexical_only, latency_ms, sentences: [{text, tier: 1|2|3, verdict, citations: [{source_id, source, section,
  page, label, kind, ord, quote}]}], passages}`. `answer` is what Pisa says: tier 1 "le fonti dicono…", tier 2
  "non lo dicono direttamente; da Tabella 3 si può dedurre… (mia deduzione)", tier 3 "nessuna delle mie fonti lo
  spiega: vuoi che cerchi stanotte?". Needs an Anthropic model on the new slot `learn.answer` (409
  `model_not_configured` / 400 `model_incompatible` otherwise, naming Settings → AI models).
- `answer` is in the language they set in Settings. Pisa Learn's calls are billed under `learn.*` slots and
  capped at $20 a month inside their own caps: past it, 402 `budget_exceeded` naming Pisa Learn's cap.
- Erase: the new family `learn` (`learn_sources` with their segments and chunks, `learn_answer_log`).

## REST errors (HTTP status per code)

Every REST error has the body `{"error": {"code", "message", "action"}}`. Validation messages never echo the
input, because it may be a key.

| HTTP | Codes |
|---|---|
| 400 | `invalid_message`, `protocol_version_unsupported`, `pairing_code_invalid`, `model_incompatible`, `validation_failed` |
| 401 | `auth_failed` |
| 403 | `biometric_required` (an approval that `requires_biometric` approved without the Face ID signature, or a signed owner action without its `device` signature, or a spoken approve past 120 s from the first read-out; an autonomy level or a phone key without a valid signature; a skill turned on without one) |
| 402 | `budget_exceeded`, `places_cap_reached` (the monthly cap for maps and web research; no free provider can do it) |
| 404 | `not_found`, `options_expired` (plan options are kept 15 minutes) |
| 409 | `plan_changed` (the day changed since the options were made: ask again), `check_in_answered`, `model_not_configured`, `provider_key_missing` (including `POST …/test` with no key), `connector_not_configured`, `connector_not_connected`, `push_not_configured` (the server has no APNs key yet; `PUT /v1/devices/me/push` still saves the token), `location_unknown` (no device location, or the latest is older than 15 min, for `near=here` / `from=here`), `place_provider_not_configured` (no provider for that job has a key, or the job is off in Settings → Places), `last_device`, `approval_expired` (unanswered for 10 minutes, or the undo's 10 minutes are over; the act changed after the card was shown, "changed after the card": void, nothing ran), `approval_decided` (already answered or undone), `google_permission_missing` (calendar.events or gmail.compose not granted: reconnect Google), `undo_expired` (the 24 h to undo a done or attended item are over; a ledger line whose Undo is over or never existed), `talk_economy` (the conversation is in economy), `voice_consent_missing`, `voice_cohort_missing` |
| 429 | `rate_limited` |
| 500 | `internal` |
| 502 | `provider_error`, `connector_auth_failed`, `place_provider_error` (every place provider in the user's order failed; the message names the last cause), `talker_unavailable` |

Over the WebSocket, a refused `approval_response` gets an `error` whose `reply_to` is the response's id and
whose code is the same as over REST: `approval_expired`, `approval_decided` or `biometric_required` (an unknown
approval id is `invalid_message`, naming it).

## Changing the protocol

- **Adding** an optional field or a new message type is backward compatible: bump nothing, add fixtures.
- **Removing or renaming** anything is allowed in development mode (no compatibility layers), but it must ship
  in the same change on the server and the app, spelled out in this file.

## Every source a connector

One registry, `GET /v1/connectors` → `{connectors: [ConnectorOut]}`, one entry a connector in code's order:
`id`, `name`, `family` (health, sport, bank, admin, mail, calendar, places, home, mobility), `provider`, `state`,
`enabled` (their switch), `missing`, `message`, `settings`, `settings_schema` (JSON Schema the app renders),
`credential` (its fields and whether one is set; **never a value**), `needs_consent`, `consent`
(`expires_at`, `renew_from`), `data_out` (`provider`, `what`, `where`), `data_out_seen`, `last_sync`
(`at`, `counts`), `tools`, `built`, and for `google` only `google`: contract A's object (also at
`GET /v1/connectors/google`). **Breaking**: the list's items were contract A's `Connector`; the app reads
Google's detail from `google` now.

- `state`: `off` (switched off: no sync, no call, no tool offered) → `needs_owner` (on, `missing` names what:
  `credential`, `credential_expired`, `consent`, `consent_expired`, `phone_permission`, `google_connect`,
  `provider_not_built`) → `on` → `error` (three failing syncs in a row, or a refused credential; `message` is the
  real cause and the action) → `on` again after a sync that works.
- `PATCH /v1/connectors/{id}` `{enabled?, settings?, data_out_seen?}` (signed, the table above): `settings` is a
  change validated by `settings_schema` (400 names the field). Switching on a connector with `data_out` the
  first time needs `data_out_seen: true`: the app shows `data_out` first. A connector with `built: false` can't
  be switched on (400).
- `PUT /v1/connectors/{id}/credential` `{fields: {name: value}}`: exactly `credential.fields`, sealed on the
  server (write-only). `DELETE` removes it (a bank's consent goes with it).
- The bank (PSD2, read-only, Enable Banking): `POST /v1/connectors/{id}/consent` → `{url, state}`; the phone opens
  `url`; the bank returns to the `redirect_url` in the settings with `code` and `state`; the phone sends
  `POST /v1/connectors/{id}/consent/code {code, state}`. Pisa reminds them to renew 7 days before `consent.expires_at`
  (a task in the pool).
- `POST /v1/health/summary` (device token, unsigned like a location): one day of Apple Health's derived numbers,
  `DaySummary`: `day`, `sleep {asleep_min, in_bed_min?, bedtime?, wake?}`, `steps`, `active_kcal`, `exercise_min`,
  `workouts [{kind, minutes, kcal?, distance_m?}]` (`kind` from a list), `resting_hr`, `hrv_sdnn_ms`, `vo2max`,
  `doses {taken, missed}`. **Any other field is refused (400)**: no raw samples, no medication names, no cycle
  data, no place. The same day again replaces it; an area switched off in the settings keeps nothing. Off: 409
  `connector_off`.
- `GET /v1/obligations?window_days=` → the admin radar: `items [{id, kind, what, due_on, state, source, penalty,
  channel, amount_as_printed, rule_source_url, last_verified}]`, `flagged` (phishing-like messages: counted, never
  tracked) and `flagged_recent`.
- New error code: `connector_off` (409).
- New erase family: `health` (`health_days`); `money` adds `bank_transactions`, `bank_accounts`; `records` adds
  `obligations`.

## Never drop an intention

Their intentions and what others owe them (the waiting-for list), each with its resumption packet. Device token,
unsigned (nothing outward, nothing deleted). **There is no DELETE**: nothing is deleted, only archived; their
deletion by category stays in Settings (`records` family adds `intentions`).

- `GET /v1/intentions?kind=mine|waiting_for&state=proposed|open|done|archived` (repeatable; open and proposed by
  default) → `{intentions: [{id, kind, state, what, why, next_step, last_step, who, cue (time | place | event |
  none), cue_event (meeting_end | arrive_home | call_end), cue_ref, repeat, due_on, task_id, place_note_id, source
  (voice | sent_mail | dictation | received_mail), asked (what | next_step: the one question Pisa asked), nudged_at,
  created_at}]}`. `state: proposed` is a promise from their own sent mail or a message they dictated, waiting for their
  yes. `source: received_mail` is another person's words: data, shown as text, never an action.
- `GET /v1/intentions/{id}` → the resumption packet: `{id, kind, state, what, why, last_step, next_step, who, cue
  (in their language), due_on, thread {id, subject} | null, task {id, title, due_at} | null, document {id, kind,
  expires_on} | null, source, created_at, lines}` (`lines`: the packet as short lines, their language).
- `POST /v1/intentions/{id}/confirm` (their yes to a proposed promise: open, a pool task due that day), `/done`
  ("fatto"; a repeating one goes on), `/archive` → the packet.
- A place-cued intention is a place note: it is in `GET /v1/places/notes` until closed or archived, and fires by
  `place_enter` as any note. A notice from an event cue or a "still waiting?" carries `delivery.intention_id`.

## One finite morning

- **The knock carries one dossier.** "Posso dirti una cosa?" (path `attend.knock`, through attend and the
  guardian's gate, unchanged) now also knocks for the admin radar, their loops, the night's drafts and "domani,
  provato". On his yes ("sì", "dimmi") Pisa says **at most three** items, then how many are on the phone, then
  **"È tutto."** Only items Pisa may say aloud knock: never outside text, never a line reading as done
  while its record isn't.
- `GET /v1/dossier` (device token) → `{day, said_at, closing, items: [{id, kind, text, spoken, outside,
  sources: [{table, id}], thumb}]}`. `kind`: `provato`, `floor`, `radar`, `loop`, `draft`, `thought`, `sheet`,
  `prurito`, `perche`, `consiglio`, `prova`, `quarto`. The spoken items first, then the rest, most pressing
  first. Reading it never marks anything spoken.
- `POST /v1/dossier/items/{id}/thumb` `{thumb: "up" | "down" | null}` (device token, not signed) →
  `{id, thumb}`; 404 `not_found` for an unknown id; 400 `validation_failed` for another value.
- **The plan** (`GET /v1/plan`): a task or travel item may carry `band: {lo_min, hi_min}` (`PlanBand`), their real
  spread around the minutes Pisa planned (from the night's planned-against-actual score). Absent until the night
  has scored five items of that kind in the last 28 days. Task durations and trip minutes are planned at their
  median once a band exists.

## Pisa draws its answers, sealed on the phone

The user, asked whether Pisa may write display code that runs in a sealed box on their phone: "yes. do it".
For an answer a picture serves better (a flow, a schema, a chart, a comparison, a timeline, a small explorer),
Pisa writes a self-contained page and the phone draws it live in the chat. The sheet and Pisa's other
objects stay for everyday answers.

- **`present`, kind `drawing`** (id `drawing:<uuid>`; a phone shows it only when its hello lists `drawing` in
  `capabilities.present.kinds`; otherwise Pisa shows the sheet or says it). `object.data`:
  `{drawing_id, title (≤120), summary (1-3 plain sentences, ≤400), kind: flow|schema|chart|comparison|timeline|
  explorer|other, html, data, libraries: [{name, version}], basis?: "knowledge"}`. Fixture:
  `fixtures/valid/present_drawing.json`. `revision` counts redraws: **0** for a new drawing; a redraw keeps the
  drawing's `object_id` (`drawing:<drawing_id>`) and comes with the next revision, so the phone replaces it in
  place and may mark it "Aggiornato". A saved view's drawing is a new drawing (revision 0).
  - `basis: "knowledge"` (Q4 a): Pisa drew what it knows, with no data fetched that turn (set by code):
    the phone captions it **"Da quello che so" / "From what I know"**. Such a drawing is never a chart and has
    no number in its words that the turn didn't carry. Absent: drawn from that turn's results.
  - `html` is the whole page (≤ 200 KB once sealed), already **sealed**: Pisa put
    `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-…' …
    pisa-bundle:; style-src 'unsafe-inline'; img-src data: pisa-bundle:; form-action 'none'; base-uri 'none'">`
    **first** in the page: right after its doctype, `<html>` and `<head>` (no quoted attribute in them), before
    anything the page runs. **No `'unsafe-inline'` for scripts**: each inline
    `<script>` of the page Pisa wrote is allowed by its own SHA-256 hash, so markup that reaches the page from
    values (an `<img onerror>`) or a script written at run time never runs; `on…=` attributes are refused by
    the check and wouldn't run. The phone's own header CSP (`script-src 'unsafe-inline' pisa-bundle:` …) applies
    beside it: both policies hold, so the hashes decide. The last two directives don't fall back to
    `default-src`. The server checked it before sending (code, never a model; **defence in depth**: a string
    check can't be complete, string building such as `el['inner'+'HTML']` passes it, so the phone's box and
    the CSP are the wall): it is read as a browser parses it (tags and attributes decoded, scripts and styles
    with their escapes decoded); it parses; no address (`http:`, `https:`, `//`,
    `ws:`), no fetch / XMLHttpRequest / WebSocket / EventSource / importScripts / sendBeacon, no form, iframe,
    object, embed, base, link or meta refresh, no window.open or location write, no cookie, localStorage,
    sessionStorage, indexedDB, cache or worker, no eval / new Function / import, no postMessage, no markup
    written from values (innerHTML, `.html()`): text only; images only `data:image/…`, links only `#…`; nothing
    that looks like Pisa's approval or Face ID (approva / approve / autorizza / Face ID: refused).
  - `data` is the JSON object the page reads as **`window.PISA_DATA`**: the phone defines it before the page's
    scripts run, as data, never spliced into markup: a `WKUserScript(.atDocumentStart)` that sets
    `window.PISA_DATA = JSON.parse("<the JSON as a JS string, with <, >, & and U+2028/9 escaped>")`. Its values come only from that turn's tool results and their words; any text
    from outside (names, quotes, reviews) is here, never in `html`.
  - `libraries`: the bundled libraries the page loads as `<script src="pisa-bundle://lib/<name>.min.js">`, served
    by the app from its own bundle (a `WKURLSchemeHandler` for `pisa-bundle:`), with the app's pinned versions
    (one contract): **d3 7.9.0, mermaid 12.2.0, vega
    6.4.0, vega-lite 6.5.0, vega-embed 7.3.0, vega-interpreter 2.3.2**. Vega runs its expressions without eval
    through vega-interpreter: `vegaEmbed(el, spec, {ast: true, expr: vega.expressionInterpreter, actions:
    false})`; the CSP has no `'unsafe-eval'`. One shared page per library (`fixtures/drawings/`), sealed by
    CORE, draws under the sealed CSP in a test on each side (CORE: Chromium, `test_core_canvas1.py`; the phone:
    `CanvasAppTests`).
- **The sealed box (the phone's half):** a `WKWebView` with a non-persistent data store
  (`.nonPersistent()`), JavaScript on, no file access; every navigation refused but the first load
  (`decidePolicyFor` → `.cancel`), `window.open` refused, every request other than the page itself and
  `pisa-bundle:` blocked (a content rule list blocking `.*` with exceptions, as well as the CSP); no camera, mic,
  location or clipboard; no link previews. `summary` is the accessibility label (VoiceOver) of the box. The box
  has a visible frame and a small "Disegnato da Pisa" / "Drawn by Pisa" badge (the app's, outside the page).
- **"Ask Pisa about this", `drawing.ask`** (device → Pisa): `{drawing_id, label (1-120 user-perceived characters,
  grapheme clusters, as the phone counts)}`. The app sends it after his own tap: an element marked `data-pisa-ask`
  (its value, or its text), or a label **the page passes to `Pisa.ask(label)`**. Either way **the label is the
  page's text, not their**: Pisa treats it as outside text, quarantined (a `drawing-label` data block that taints the
  turn), grounding nothing, joining no words of theirs, never a yes. Pisa answers it as their turn marked as coming from
  the drawing ("Sul disegno: <label>", `origin: drawing`): it may read and answer, never approve, send, book,
  call, pay or change anything, whatever the label says. Pisa's `say` answers the message's id
  (`reply_to`). A drawing Pisa didn't show this device (or one from before a restart): `error`
  (`invalid_message`) with `reply_to`. One ask every 2 s per socket: a sooner tap gets the same `error` (show
  it; never drop a tap silently). Fixture: `fixtures/valid/drawing_ask.json`.
- **Redraw:** "fammelo vedere per X / dal punto di vista di Y / più semplice" in the next turns rewrites the last
  drawing: a `present` of the same object (`drawing:<the same drawing_id>`) at the next `revision`. The conversation keeps its last five drawings
  (code and data) with its messages: the same retention and erase family.
- **Saved as a view:** "salvalo come <nome>" keeps the drawing's code and a recipe of read-only reads; "mostrami
  <nome>" runs the reads and sends the same code with fresh `data` (a `drawing` object, as above). In
  `GET /v1/views` such a view lists with `blocks: 0`.

## A turn without the socket, and what the wrist may show

The watch never talks to Pisa itself: it always goes through the iPhone (WatchConnectivity), and the phone, often
in the background with no open socket, uses REST. Nothing new is exposed.

- `POST /v1/turns` (device token, unsigned) `{text, lang: "it-IT" | "en-US", origin: "phone" | "watch",
  client_turn_id}` (`text` 1–2000 characters, `client_turn_id` a UUID; `origin` defaults to `phone`). It runs
  **the same turn** as an `utterance` on `/v1/device` from that device (the brain, tools, rails, quarantine, the
  yes-card's code, the ledger, the conversation's record and the spend caps; the briefing phrase too). From the
  phone, a "sì" decides only what a spoken yes decides (the 10 s / 120 s windows), never a `requires_biometric`
  approval. **From the watch, a "sì" never approves**: Pisa says the card is on the phone; a "no" still
  rejects. `X-Pisa-Origin: watch` wins over a body that leaves `origin` at its default.
  - **200** `{turn_id, state: "done", say, sheet_id | null, sensitive}` when Pisa answers within **8 s**.
    `sensitive: true` when their words or Pisa's answer name their health or a sensitive place, or they are at one:
    the watch shows it and never says it aloud; the 202's late notify carries the same `sensitive`
    (its `wrist` then empty).
    `origin: "watch"`: `say` is Pisa's first sentence (by code, the rule of the short routes), never Pisa's own
    "Cerco, un attimo."; a turn that kept a sheet says the lead whole (what Pisa found, the pick and why, at most
    two sentences); a question to them (an approval, a draft read back) is whole. The complete answer stays on the phone: the conversation keeps
    the whole reply, and what the turn showed is kept as a sheet, `sheet_id` (`GET /v1/sheets/{sheet_id}`, the
    `present` object the app renders: a research answer's table).
  - **202** `{turn_id, state: "working", say, sheet_id: null, sensitive: false}` past 8 s: `say` is Pisa's holding line ("Ci lavoro e
    ti scrivo sul telefono."). The answer comes as a `notify` (`kind: info`, `turn_id`, `urgency: breakpoint`,
    with `sheet_id` when the turn kept one) through the guardian's gate (path `turn.answer`, an answer to their
    own words: never held), on the socket or pushed `active` (`userInfo.turn_id`, `userInfo.sheet_id`). A turn
    a restart left working is closed at the next start: a retry of its id then gets 500 `internal`.
  - The same `client_turn_id` within **10 minutes** returns the same turn: its answer if done, else 202 again;
    never a second answer, never a second spend. Past 10 minutes it is a new turn.
  - Errors as on the socket: 402 `budget_exceeded`, 400 `validation_failed`. A stop ("Pisa, ferma tutto") doesn't
    refuse it (a stop never pauses their own requests). A device's turns run one at a time (as on the socket); a
    fourth while three are still running or waiting: 429 `rate_limited` (no row kept: a retry is a new turn).
  - Fixtures: `fixtures/rest/turn_done.json`, `fixtures/rest/turn_working.json`.
- **`X-Pisa-Origin: watch`** (optional, every REST route; `phone` when absent; another value: 400
  `validation_failed`): the phone relaying the watch (a Reject, a check-in's answer, a thumb, stop and resume).
  `POST /v1/approvals/{id}` with `decision: approve` and `X-Pisa-Origin: watch` is refused (400
  `validation_failed`, "approve it on the phone"); reject is accepted.
  It is recorded where the route logs a decision (`approvals.decided_origin`, `check_ins.answer_origin`,
  `dossier_items.thumb_origin`, `pisa_stops`), and on Pisa's moves and the conversation's lines. Not repeated on each
  route in `openapi.json`.
- **the "from where"**: a ledger line carries `origin` (`phone` | `watch`); a `watch` one's `why` ends with
  " · dall'orologio" / " · from the watch".
- **`notify.wrist`** (socket and push `userInfo.wrist`; Pisa always sends it): at most **60 characters** in their
  language, Pisa's own words **templated by kind** ("Promemoria · alle 15:30", "È ora di uscire", "Pisa ha risposto ·
  sul telefono", "Com'è andata? Rispondi qui"): never the notice's text, outside text, a person's name, an amount
  or a document. **Empty** while they are at a sensitive place, about one, or for health: the watch then shows the
  kind alone ("Un promemoria"). An approval's push carries `userInfo.wrist`: its kind and how many their yes would
  notify ("Spostare un evento · avvisa 3 persone"), never who. Fixture: `fixtures/valid/notify_wrist.json`;
  the schema's `wrist`, `turn_id` and `sensitive` are proposed for the schema.
  An item's health is read from its own title on their reminders, leaves, closes, place notes and cues
  ("Dentista alle 9:00").
- **`notify.sensitive`** (socket and push `userInfo.sensitive`; Pisa always sends it): `true` when the notice is
  about a sensitive place or their health, or they are at one; the watch shows the kind alone and never speaks it.
- **Plan items** (`GET /v1/plan` and every route that answers a plan) carry `private` (a calendar event Google
  marks private or confidential, or a task they marked private: `PATCH /v1/tasks/{id} {private: true}`, read back
  as `Task.private`) and `sensitive` (at a sensitive place, an off-limits topic, or health). The watch shows "Impegno" and never
  speaks an item unless both are false. Fixture: `fixtures/rest/plan_watch_flags.json`.
- **One interruption level** (`aps.interruption-level`) for every notice, from the guardian's decision: `now`
  → `time-sensitive`, released at a breakpoint → `active` (and a REST turn's late answer, its urgency
  `breakpoint`), a digest (the guardian's, the quiet hours' summary)
  → `passive`, their "never" shown silently → `passive`. Approvals: `time-sensitive` (10 minutes to answer;
  `passive` under their "never"). the push that asks to open a talk (`TALK_ASK`): `time-sensitive`. The phone
  needs the Time Sensitive Notifications entitlement.

## The note Pisa learns from them (server side)

**REST only** (their view in "Su di te" comes later); nothing new on the socket
or in a push. One note per owner: their standing instructions on how Pisa answers and works, in Pisa's words from their
own tellings, Italian or English as they speak, at most `cap` (1,000) characters. Pisa rewrites it whole when they
teaches or corrects it in a conversation (the brain's `rewrite_my_note`; after outside text it waits for their yes,
as a profile write does), reads it whole in every turn, and keeps every version. It changes how Pisa answers, never
what Pisa may do without their yes: a line asking for more autonomy is listed in `wishes` and has no effect.

- `GET /v1/profile/note` → `Note` (fixture `rest/profile_note.json`): `text` ("" before their first telling),
  `version` (0 before it), `updated_at`, `cap`, `wishes`, `versions` (newest first, at most 50: `{version, text,
  change, source: turn | app | restore | held, restored_from, at}`) and `versions_total`.
- `PUT /v1/profile/note` `{text}` → `Note`: their own note replaces Pisa's, a version like any other (`source: app`;
  the same text again writes none). Over the cap: 400 `validation_failed`.
- `POST /v1/profile/note/restore` `{version}` → `Note`: that version written again as the newest (`source:
  restore`, `restored_from`); the versions in between stay. Unknown version: 404 `not_found`.

