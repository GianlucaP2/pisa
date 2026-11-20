# Notifications

How Pisa's notices reach a device when its client is closed, and what each way shows and reveals. Each client
chooses its mode when it registers (`PUT /v1/devices/me/push`, see [Push](build-a-client/push.md); in the PISA
app, Settings, Notifications); the server only needs to be ready for that choice (`pisa setup` tells you what is
missing). While a client is open, notices reach it on its live socket in every mode.

## The three modes

| Mode | Who it is for | What the server needs |
|---|---|---|
| **Relay** | the PISA app | `PISA_PUSH_RELAY_URL` (the relay's https address) |
| **Direct** | an iOS client of your own, with your own Apple key | `PISA_APNS_TEAM_ID`, `PISA_APNS_TOPIC`, and `pisa secrets set-apns` |
| **None** | any client | nothing: notices arrive only while the client is connected, and in the morning briefing; a client catches up when it connects |

Both push modes are Apple's push service: there is no Android, web push or other push mode today. The relay
serves only the PISA app.

In direct mode, `PISA_APNS_TOPIC` is **your own client's bundle id** (the app id you registered with Apple), and
`PISA_APNS_TEAM_ID` is your own team. Pisa has no default for either: the PISA app's own id is only for the
relay, which its publisher runs.

The rest of this page describes what the PISA app shows in relay and direct mode. A client of your own decides
what it shows.

## Relay mode: what the relay and Apple see

Your server seals each notice to a key that only your phone holds, then hands the sealed blob to the relay,
which passes it to Apple. The relay and Apple see:

- an opaque handle for your phone, and your phone's push token;
- the time each notice is sent, and its type (a notice, a background wake, a Live Activity update);
- that your subscription is valid.

Every sealed notice has one of two fixed sizes, so its length says little. The relay never sees the text, a
title, a name, a place, or your server's address.

## What the lock screen shows

- **A notice:** your phone opens the sealed blob itself and shows the notice. Notices about your health or your
  money show a generic line ("A reminder from Pisa") on the lock screen, unless you turned on full previews for
  that kind in the app. A notice about a sensitive place is always generic.
- **The "Next" Live Activity:** in relay mode it shows **no stop, no time and no progress** of your day. Apple
  draws a Live Activity itself and nothing on the phone can decrypt it, so Pisa sends the relay nothing that
  describes your day: the activity only starts, refreshes and ends. Open the app to see your next stop and when
  to leave; the app fetches them over its own connection to your server. In direct mode, with your own Apple
  key, the Live Activity shows the full content.
- **If a notice cannot be opened** (for example after a key change), the phone shows "Open Pisa to see it".

Buttons on a notice (Approve, Undo, a check-in's answer) go from the phone straight to your server, never
through the relay.

## Turning it off

Choose **None** (from the client, `PUT /v1/devices/me/push` with `{"mode": "none"}`), or unpair the device (in
the PISA app, Settings, Server, Unpair and delete). Either way your server also asks the relay to forget the
device's registration. Revoking a phone from another device or with
`pisa devices revoke` does the same.
