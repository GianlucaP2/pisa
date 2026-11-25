# Connections

Connections (Collegamenti in Italian) is one place for every outside service Pisa can reach: which ones are set
up, which cost money, what leaves your server and to whom. A client shows it from `GET /v1/connections`; the
PISA app's Settings → Connections is one such client.

## The six categories

| Category | What is in it |
|---|---|
| AI models | each AI provider (Anthropic, OpenAI, Gemini…) and each model server of yours, with the feature slots whose model is on it |
| Voice | the phone's own voice, the server's voices (OpenAI, ElevenLabs) and natural voice (Gemini Live, and xAI when it is on) |
| Maps & places | your phone's own maps, Apple Maps, Google Maps, OpenStreetMap and OpenRouteService |
| Web research | Tavily |
| Accounts | your Google account (Gmail and Google Calendar) |
| Notifications | on the phone itself, through a push relay, or direct to Apple's push service |

A feature that is off on your server is absent: the xAI voice unless `PISA_EXPERIMENTAL` names it, and in App
Store review mode the maps, web, Google account, push relay and server voices ([App review](app-review.md)).

## What each entry says

- **state**: `on`, `off` (switched off on the server, or by you), `not_set_up` (no key yet, or a paid provider
  waiting for your OK), `error` (its last test failed, or Google's sync keeps failing) or `needs_reconnect`
  (Google no longer accepts Pisa's authorization: connect again).
- **paid**: whether it costs money: a metered service with a price in Pisa's tables. The AI providers, the
  server's voices, natural voice, Google Maps and Tavily do. Your phone's maps, Apple Maps (a daily quota, never
  billed), OpenStreetMap, OpenRouteService's free plan, your own model server, your Google account and the
  notification paths don't.
- **data_out**: what leaves your server and to whom, in one fixed sentence ("Web searches and page addresses,
  to Tavily"). It is the code's own wording, never your data. Two of them are worth reading before you rely
  on the free fallbacks ([OpenStreetMap](places-and-web.md#what-is-sent-where)):
  - OpenStreetMap is **on by default**, with no key: "On by default: the words of each place search and a
    point rounded to about 100 m, to the public OpenStreetMap services Photon, Nominatim and Overpass, or to
    your server's own instances of them". Those public services are third parties run by volunteers; point
    `PISA_OSM_*_URL` at your own instances, or set `PISA_OSM_ENABLED=0`, to keep searches on your network.
  - OpenRouteService gets a route's real ends: "The real start and end points of each trip (to about 1 m,
    which can be your home), to OpenRouteService".
- **actions**: what a client may offer, each an endpoint that already exists for that kind: set or delete a key,
  test it, connect or disconnect Google, give the paid OK.
- **docs**: the page of this site about it.

Nothing secret is ever in the answer: of a key, only its last four characters, as each provider's own endpoint
already shows them.

## The paid OK

Google Maps and Tavily bill above a free allowance. Pisa never calls either one, not even to test its key and
not for the night's searches, until you have given your OK to it **and to its own monthly cap**, in euro cents.
The cap can't be more than the server's hard monthly limit (`PISA_HARD_MONTHLY_EUR`); past it, the provider is
skipped until the month ends and Pisa uses the free providers. It counts every call made to that provider this
month. Connections shows what each one has cost so far this month.

Until you give it, a turn that needs the provider says so in one line (the place search waits for your OK in
Connections), the same way it says a provider isn't set up. A key set before v0.1.4 is treated the same: it is
kept, but not used until you give the OK.

Giving the OK is a signed operation, like storing a key: the client signs a `device` challenge with op
`paid_ack` and target `<provider>:<cap in cents>`, for example `tavily:500`, with Face ID or its own device
key ([Pairing](pairing.md#signed-operations)), and then sends `PUT /v1/connections/paid/<provider>` with
`{"cap_cents": 500}`. It is always signed, even before the first Face ID key. A new OK replaces the old one;
`DELETE /v1/connections/paid/<provider>` withdraws it. Deleting the provider's key keeps your OK.

From the command line, a paired `pisa client` gives the OK with its own device key:
`pisa client paid-ack google 500` (the provider, then the cap in cents). It is the same signed request
(`pisa client call PUT /v1/connections/paid/google --data '{"cap_cents": 500}' --sign paid_ack google:500`
sends it by hand).

The AI providers and the voices don't need this OK: they already have their own guard, your AI consent (which
provider sees which data) and the daily and monthly AI caps ([AI providers](ai-providers.md#spending)). The
hard limit holds over everything together.

## Deleting a key

Every key a client can set, it can delete: the AI providers' and voices' (`DELETE
/v1/ai/providers/<provider>/key`), the places and web providers' (`DELETE /v1/places/providers/<provider>/key`),
the APNs key (`DELETE /v1/push/key`) and your Google web client (`DELETE /v1/connectors/google/credentials`,
after disconnecting Google).

An AI key that a feature slot still uses can't simply go: the answer is `409 provider_in_use` with the slots it
serves. Either set those slots to another model first, or delete it with `?replace_with=<another provider>`:
each slot then moves to that provider's model that can do what the slot needs, in the same step as the
deletion. When the replacement has no model for a slot, nothing changes (`400 model_incompatible`, naming the
slots).

For a client's view of the same API: [Build your own client → Connections](build-a-client/connections.md).
