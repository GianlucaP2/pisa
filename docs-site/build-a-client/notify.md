# Local notifications (contract 2.3)

Pisa can hand the phone its reminders, task and calendar alerts and "time to leave" alerts to schedule as local
notifications, and places to watch (geofences), so they fire even when the phone is offline. This is contract
2.3, the extension `pisa.notify` (major 1) and the feature `notify.local`.

## Turning it on

List `pisa.notify` among the extensions in the `hello` (`"extensions": {..., "pisa.notify": 1}`). A server that
grants it in the `welcome` (and lists the feature `notify.local` in `GET /v1/server`) sends the three frames below
on the notices thread; a client that did not list it never gets them.

## What the server sends

| Event | Value | |
|---|---|---|
| `pisa.notify.schedule` | `{revision, items}` | 1 to 50 items `{id, fireAt, title, body?, kind, deepLink?}` (`$defs/NotifyItem`) to schedule; an `id` already scheduled is replaced |
| `pisa.notify.cancel` | `{revision, ids}` | 1 to 50 ids to remove (notifications or watched places); an unknown id is no error |
| `pisa.notify.regions` | `{revision, regions}` | the whole set of watched places, 0 to 20 `{id, lat, lon, radiusMeters, title, on, deepLink?}` (`$defs/NotifyRegion`): it replaces the previous set, and an empty one stops them all |

- `revision`: one counter per device, shared by the three, growing with each frame. Apply a frame only if its
  revision is above the last one you applied; a lower or equal one is a replay: drop it, and ack it again.
- `id`: 1 to 64 of `A-Z a-z 0-9 _ -`. `fireAt`: RFC 3339 with an offset. `kind`: `reminder`, `task`,
  `calendar` or `leave`.
- `title` (up to 80 characters) and `body` (up to 200) are what the lock screen shows. Pisa keeps them minimal
  (no health or money detail, no address, no coordinate); show them as the user's preview setting allows.
- `radiusMeters`: 100 to 5000. `on`: `enter` or `exit`.

`deepLink`, where a tap leads, is one of exactly these (the `<uuid>` in lower case):

| Deep link | Opens |
|---|---|
| `pisa://chat` | the conversation |
| `pisa://riepilogo` | the riepilogo |
| `pisa://task/<uuid>` | one task |
| `pisa://reminder/<uuid>` | one reminder |

Nothing else is valid (never a web address, never `pisa://pair`), and a client opens no other link from a
notification.

```json
{"type": "CUSTOM", "name": "pisa.notify.schedule", "value": {"revision": 12, "items": [
  {"id": "rem-1", "fireAt": "2026-11-25T08:30:00+01:00", "title": "Promemoria", "body": "Ritirare il pacco",
   "kind": "reminder", "deepLink": "pisa://reminder/5b0e4c1e-8a7d-4c4b-9a39-2f4f1c9d7e10"}]}}
```

## What the phone answers

After each frame, `pisa.notify.ack` with its revision: the ids it applied (scheduled, cancelled or watched) and
those it could not, with why (`denied`: notifications or location not allowed; `limit`: the system's cap on
pending notifications or regions; `past`: `fireAt` already gone; `invalid`; `unsupported`; `error`). Both lists
are required, each up to 50, either may be empty.

```json
{"op": "custom", "name": "pisa.notify.ack", "value": {"revision": 12, "scheduled": ["rem-1"], "failed": []}}
```

The ack is untrusted input to the server: ids it did not send are ignored. Neither side logs a region's
coordinates.

The fixtures are in `protocol/v2/fixtures/gaps/{valid,invalid}/notify_*.json`; the kit checks them
([Testing your client](testing.md)).

## What this server does

- **The schedule upserts.** Each change sends, in one short run on the notices thread, a `pisa.notify.cancel`
  for ids no longer wanted, then one `pisa.notify.schedule` with the items that are new or changed, then
  `pisa.notify.regions` when the set of places changed. On every connection (after the `welcome`) the whole
  schedule and the regions go again, so a reinstalled app gets everything back. Items: the next 7 days, at most
  50, soonest first. A reminder that fired leaves with a cancel two minutes after its moment, never before.
- **Ids** are stable for the thing they alert about (`rem-…`, `task-…`, `cal-…`, `leave-…`; regions `reg-…`,
  `note-…`): a moved event comes again with the same id and a new `fireAt`.
- **Text:** `title` names the kind only ("Promemoria", "Attività", "Evento", "È ora di uscire") and `body` is
  absent unless the server's operator turned details on. Coordinates have 4 decimals.
- **The ack** is checked against the schema (an ack outside it is `invalid_frame`); an id this device was not
  sent in that revision is ignored, and a revision never sent is dropped. Send `failed` with `denied` when the
  user has not allowed notifications: the server then pushes as before and Pisa says the reminder shows when
  the app opens.
- **No double alert:** while the phone's ack of an item is fresh (within 7 days, notifications not denied),
  the server does not push that item to it; a notification for it on the live socket still comes while the app
  is connected, so drop the local one when the app is in the foreground if it would show twice.
