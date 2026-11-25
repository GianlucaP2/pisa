# Notifications

How Pisa's notices reach a device when its client is closed, and what each way shows and reveals. Each client
chooses its mode when it registers (`PUT /v1/devices/me/push`, see [Push](build-a-client/push.md); in the PISA
app, Settings, Notifications); the server only needs to be ready for that choice (`pisa setup` tells you what is
missing). While a client is open, notices reach it on its live socket in every mode.

## The modes

| Mode | Who it is for | What the server needs |
|---|---|---|
| **On the phone** (the default) | an app that speaks contract 2.3 (`pisa.notify`) | nothing: no key (`PISA_PHONE_NOTIFY_ENABLED`, on by default) |
| **Relay** | an app whose publisher runs a push relay: none is offered by the PISA project today | `PISA_PUSH_RELAY_URL` (the relay's https address) |
| **Direct** | an iOS client of your own, with your own Apple key | `PISA_APNS_TEAM_ID`, `PISA_APNS_TOPIC`, and `pisa secrets set-apns` |
| **None** | any client | nothing: notices arrive only while the client is connected, and in the morning briefing; a client catches up when it connects |

"On the phone" works beside the others: a phone that has it gets its alerts scheduled locally whatever its
push mode, and the relay or direct adds Pisa's other notices (a briefing, a re-plan, Pisa speaking first) while
the app is closed. Both push modes are Apple's push service: there is no Android, web push or other push mode today.

**About the relay.** Notifications are scheduled on the phone by default, and direct mode uses your own Apple
key: neither needs anyone else. Relay mode exists in the code for an app publisher who chooses to run a relay
for its app; **none is offered by the PISA project today.** If one is ever offered, its operator, the metadata
it sees and how long it keeps it will be stated before it is available.

In direct mode, `PISA_APNS_TOPIC` is **your own client's bundle id** (the app id you registered with Apple), and
`PISA_APNS_TEAM_ID` is your own team. Pisa has no default for either.

The rest of this page describes what the PISA app shows in relay and direct mode. A client of your own decides
what it shows.

## On the phone: local notifications, no key

Your server hands the phone the next 7 days of alerts and the phone schedules them itself, so they fire with
Pisa closed and with no connection. Nothing goes through Apple's push service or the relay.

- **What is scheduled:** timed reminders ("ricordami alle 9 di…"), open tasks with a time (at the reminders'
  task lead), calendar events with a time (at the event lead), and "time to leave" for the day's trips (while
  leave-now is on): the soonest 50 within 7 days. An alert inside your quiet hours is left to the server, which
  holds it as it holds every notice. Places to watch: a reminder for arriving at or leaving a place, and the
  notes pinned to a place, the 20 nearest.
- **When:** each time the app connects (the whole schedule again), and within a second or two of any change: a
  reminder set, cancelled or fired, a task dated, done or deleted, an event moved or cancelled, a new plan.
  Each change is a `revision` the phone applies only once; the phone answers with what it scheduled.
- **No double alert, no lost one:** when the phone confirmed it holds an alert (within the last 7 days, with
  its notifications allowed), the server does not push that alert to it too. With no confirmation, the server
  pushes as before.
- **What Pisa says:** Pisa says "te lo ricordo alle 9" only when the reminder will reach you: the phone's own
  notifications work (it confirmed recently, notifications allowed), or a push mode is set up. Otherwise it
  says the reminder is set and you'll see it when you open the app.
- **What the lock screen shows:** by default only the kind ("Promemoria", "Attività", "Evento", "È ora di
  uscire"; "Reminder", "Task", "Event", "Time to leave"), never the text: no title, no name, no place. With
  `PISA_PHONE_NOTIFY_DETAILS=true` it also shows the item's own words; a stranger's words (an invite's title, an
  email's task) never. A tap opens the reminder, the task or the riepilogo.
- **What the server keeps:** per phone, the revision, when it last confirmed, and each alert's id, kind, moment
  and state; never a title. Neither side logs a title, a time or a place's coordinates.

Turn it off with `PISA_PHONE_NOTIFY_ENABLED=false` (App Store review mode keeps it off). The protocol is in
[Local notifications](build-a-client/notify.md).

## Relay mode: what the relay and Apple see

What a relay would see, by design, if one were in use (none is offered today). Your server seals each notice to
a key that only your phone holds, then hands the sealed blob to the relay, which passes it to Apple. The relay
and Apple see:

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
