# Connectors

Every source Pisa reads from is a connector, switched on or off from a client (`PATCH /v1/connectors/{id}`:
your own client, or the PISA app's Settings → Connections). The list is the code's own (`core/pisa/domain/connectors.py`); each connector has its own settings, and some take a
credential or a consent.

| Connector | What it reads | What it needs | On by default |
|---|---|---|---|
| Google (Gmail + Calendar) | your mail and calendar | your own Google project and OAuth client, and an https address for your server ([below](#google)) | yes |
| Apple Health | sleep, activity, workouts, heart, medications (each can be left out) | a client on the iPhone that reads Apple Health and sends a daily summary (`POST /v1/health/summary`), as the PISA app does | yes |
| Garmin | activities, sleep, daily summary | a Garmin Personal API key and its expiry date | no |
| Bank (read-only), Second bank, Third bank | accounts and transactions, never a payment | an Enable Banking application (id and private key) and your consent on the bank's page (PSD2, 90 to 180 days) | no |
| Radar: mail | official notices in your mail (pagoPA, SEND, insurers, condominium, tax agency) | Google | yes, Italy only |
| Radar: PEC | your certified mailbox, over IMAP | the PEC address and its app password | yes, Italy only |
| Radar: documents | the expiry dates of the documents in your vault | nothing | yes |
| Radar: calendar | the official calendar's dates that apply to you (car, property, condominium, ISEE, income tax) | nothing | yes, Italy only |
| Agenzia delle Entrate: tax drawer | your CU forms, read-only | Fisconline tax code, password and PIN | no, Italy only |
| Polar, Withings | not built yet: the row and its settings exist | a token | no |

"Italy only" connectors serve a user whose `PISA_COUNTRY` is `IT`, and stay off anywhere else.

## States

`GET /v1/connectors` gives each connector's state, for a client to show:

- `off`: switched off. Pisa takes nothing from it and offers none of its tools.
- `needs_owner`: on, but something is missing; `missing` says what (`credential`, `credential_expired`,
  `consent`, `consent_expired`, `phone_permission`, `google_connect`, `google_off`, `provider_not_built`).
- `error`: three syncs in a row failed; `message` names the cause. The next successful sync brings it back.
- `on`.

## Credentials and data sent elsewhere

- A credential is write-only: a client can set or replace it, never read it back. Secret fields are encrypted
  with `PISA_SECRETS_KEY`.
- A connector that sends your data to a provider other than Pisa (Garmin, the banks, the tax agency) shows that
  provider, what goes there and where, before it can be switched on (`data_out`).
- A bank's consent is renewed on the bank's page; Pisa reminds you a week before it ends.

The connectors' names and settings are in English, or in Italian when `PISA_LANGUAGE=it`.

API: `GET /v1/connectors`, `PATCH /v1/connectors/{id}`, `PUT /v1/connectors/{id}/credential`,
`POST /v1/connectors/{id}/consent`; Google has its own `/v1/connectors/google` (see [API](api.md)). Once the server holds a Face ID key (the PISA
app's), changes are signed operations: your client signs them with its own device key
([Pairing](pairing.md#signed-operations)).

## Places

Places and web search are not connectors (no switch in `GET /v1/connectors`): they are providers. Place search
and travel times are asked, in order, of **your phone's own maps** (no key: an app that grants `pisa.maps`),
then **Google Maps or Apple Maps with your key**, then **OpenStreetMap**, free (search with no key; travel
times with your free OpenRouteService key). With none of them the rest of Pisa works and the assistant says
plainly that places aren't set up (error `place_provider_not_configured`). Data from OpenStreetMap comes with
its credit, "© OpenStreetMap contributors", for your client to show. The weather needs no key (Open-Meteo).

| Provider | What it does | Where to get a key | How to set it |
|---|---|---|---|
| Apple Maps | travel times, place search | a paid Apple Developer Program membership: a Maps key (a `.p8` file and its 10-character key id) in your Apple developer account, Certificates, Identifiers & Profiles, [Keys](https://developer.apple.com/account/resources/authkeys/list) | your team id in `PISA_APNS_TEAM_ID`, then `pisa secrets set-maps --key-id <key id> < AuthKey_<key id>.p8`, or `PUT /v1/places/providers/apple/key` with `{"key": "<the .p8 text>", "key_id": "<key id>"}` |
| Google Maps | travel times, place search, place details (hours, phone, website, reviews, photos) | a Google Cloud project with billing on, the Places API (New) and the Routes API enabled, and an [API key](https://console.cloud.google.com/apis/credentials) | `pisa setup` (its Maps & places section), or `PUT /v1/places/providers/google/key` with `{"key": "<the API key>"}` |
| Tavily | web research (the assistant's web search, page reading, a place's research, the night's searches) | a Tavily account and its [API key](https://app.tavily.com) (1,000 free credits a month) | `pisa setup` (its Web research section), or `PUT /v1/places/providers/tavily/key` with `{"key": "<the API key>"}` |
| OpenStreetMap | place search, after the phone and your keys | no key: on unless `PISA_OSM_ENABLED=0` | nothing to set |
| OpenRouteService | travel times on foot, by bike or by car, after the phone and your keys (no public transport) | a free account and its [API key](https://openrouteservice.org/dev/#/signup) | `PUT /v1/places/providers/openrouteservice/key` with `{"key": "<the API key>"}` |

Google Maps and Tavily cost money above their free allowance: Pisa calls them only after you give your OK to
each and to its own monthly cap, in Settings → Connections ([Connections](connections.md#the-paid-ok)). A key
set before v0.1.4 waits for that OK too.

Keys are stored encrypted with `PISA_SECRETS_KEY`, which must be set first. From a client, storing a key is a
signed operation once the server holds a Face ID key (op `place_key`, target `<provider>:<SHA-256 hex of the
key>`); on a headless install, `pisa client call PUT /v1/places/providers/tavily/key --data @tavily-key.json`
makes the same request. `GET /v1/places/providers` shows each provider's state, and
`POST /v1/places/providers/{provider}/test` makes one real call to check its key (Tavily's spends one search).
Which provider Pisa tries first for each job, the monthly cap and the costs: [Places and web
search](places-and-web.md).

## Operator responsibility

Whoever runs a Pisa server is its operator: you install it, configure it and decide what it connects to.

- The Google Cloud project and the OAuth client that connect your mail and calendar are yours, and so is the
  Google Maps API key if you add one. You accept the providers' terms for them: Google's API terms and, from
  2027, the Google Workspace AI Tools Addendum.
- You choose your AI provider (or your own model server), with your own key, under that provider's terms.
- PISA is software provided under the AGPL-3.0 "as is", without warranty of any kind (sections 15 and 16 of
  the license). This is not legal advice. See also [Privacy](privacy.md#operator-responsibility).

## Google

Pisa reads Gmail and Google Calendar with your Google account's permission. You make your own Google Cloud
project and OAuth client for it, in a few minutes: Pisa (the project) has no shared Google client, and never
receives your keys, tokens or data. Until you connect, mail and calendar tools answer "Google is not
connected".

`pisa google setup` prints these steps for your server, with its redirect URI filled in (`--lang it` for
Italian). The PISA app shows the same guide.

With the [gcloud CLI](https://cloud.google.com/sdk/docs/install) installed and logged in on the server,
`pisa google setup --gcloud` does steps 2 and 3 for you: it shows the exact command
(`gcloud services enable gmail.googleapis.com calendar-json.googleapis.com --project=<your project>`) and runs
it only on your yes at the terminal, or with `--yes`. `--maps` adds the Places API (New) and the Routes API
(step 7), `--project` names the project (otherwise gcloud's own). Without gcloud, or not logged in, it prints the
steps as usual. It never prints what gcloud says (its messages can name your account), only whether it
worked; the OAuth client (step 1) is still made by hand.

### What you need first

An https address for your server in `PISA_ADDRESSES`, with a name: your tailnet name
(`https://<name>.<tailnet>.ts.net`) or your own domain. Google sends you back to your server after you sign in,
and it accepts only https addresses with a name, not a raw IP address. The redirect URI is that address's
scheme, host and port followed by `/oauth/google/callback`, for example
`https://pisa.example-tailnet.ts.net/oauth/google/callback`. Pisa uses the first such entry of
`PISA_ADDRESSES`. Your phone or browser must reach that address when you connect; it needn't be public.

### The steps

1. **Create your OAuth client in Personal mode.** Open Google Auth Platform's Personal mode page
   (<https://console.cloud.google.com/auth/overview/personal>), signed in with the Google account whose mail
   and calendar Pisa will read. It creates a project for you, or you pick one of yours. Choose **Web
   application**, add the redirect URI as an authorized redirect URI, accept the terms and continue. Copy the
   Client ID and the Client secret at once: Google shows the secret only once.
2. **Enable the Gmail API** in your project
   (<https://console.cloud.google.com/apis/library/gmail.googleapis.com>), if it isn't on already.
3. **Enable the Google Calendar API**
   (<https://console.cloud.google.com/apis/library/calendar-json.googleapis.com>), if it isn't on already.
4. **Add the scopes** on the Data Access page (<https://console.cloud.google.com/auth/scopes>):
   - needed: `https://www.googleapis.com/auth/gmail.readonly`,
     `https://www.googleapis.com/auth/calendar.readonly`,
     `https://www.googleapis.com/auth/calendar.app.created` (Pisa refuses a connection without them);
   - optional: `https://www.googleapis.com/auth/calendar.events` (change your events) and
     `https://www.googleapis.com/auth/gmail.compose` (drafts, and sending them). Without them Pisa only reads.
5. **Paste the Client ID and Client secret into Pisa**: in the PISA app, Settings, Connections, Google. They go
   only to your own server, which keeps the secret encrypted with `PISA_SECRETS_KEY`.
6. **Connect Google**: tap Connect Google, sign in with the same account and accept the consent screen. Your
   server receives Google's answer at the redirect URI and keeps the refresh token encrypted; the app never
   holds a Google token. Finish the Google sign-in within 10 minutes, or tap Connect Google (Collega Google)
   again: a sign-in older than that is refused (`state_expired`).
7. **Optional: a Google Maps API key** for place search, place details and travel times. In the same project,
   turn on billing (<https://console.cloud.google.com/billing>), enable the **Places API (New)**
   (<https://console.cloud.google.com/apis/library/places.googleapis.com>) and the **Routes API**
   (<https://console.cloud.google.com/apis/library/routes.googleapis.com>), then on the Credentials page
   (<https://console.cloud.google.com/apis/credentials>) create an API key and restrict it to those two APIs.
   It is an API key, not a permission of your Google sign-in: Pisa never asks your Google account for access to
   Google Cloud (no `cloud-platform` scope). Paste the key in the PISA app (Settings, Connections, Google
   Maps), and give your OK to its monthly cap there: Google Maps costs money above its free allowance, so Pisa
   calls it only after that ([Connections](connections.md#the-paid-ok)).

### If Personal mode doesn't work

Personal mode is the best fit: no verification, no user limit, no 7-day expiry, and only the account that
created it can authorize the client. A Personal-mode project stays that way, and serves one Google account: a
second person needs their own. If Personal mode isn't offered, or refuses the scopes or the redirect URI, go
down this ladder:

1. **External, In production, unverified.** Create a Web application client on the Clients page
   (<https://console.cloud.google.com/auth/clients>) with an External audience, and publish the app In
   production without asking for verification. Google may ask for a homepage and a privacy policy URL on the
   Branding page. When you connect, Google first shows a warning that it hasn't verified the app: it is your
   own app, so continue. Google's limit of 100 users for unverified apps doesn't matter for one person.
2. **External, Testing** (last resort): leave the app in Testing with your account as a test user. Google ends
   the authorization 7 days after you consent, so you connect again every week.

Common mistakes: a redirect URI that differs by one character from the one Pisa shows (Google answers
`redirect_uri_mismatch`), and a Client ID or secret pasted with a missing character (`invalid_client`). A new
client can take a few minutes, sometimes longer, before Google accepts it.

On `redirect_uri_mismatch`, tap "dettagli dell'errore" ("error details") on Google's page, or run
`pisa google setup` on the server, to see the exact redirect URI. The OAuth client must be a **Web
application**; paste that URI exactly as an authorized redirect URI, then wait about 5 minutes for Google to
apply it before you connect again ([Troubleshooting](troubleshooting.md#common-problems)).

### Good to know

- Your Google project and OAuth client are yours: you are their developer under Google's
  [API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy).
- PISA (the project) never receives your keys, tokens or data: they stay on your server, encrypted.
- Google's terms apply to your project.
- Don't use the PISA name or logo as the app name on your consent screen.

The iOS client id that an earlier setup asked for is no longer used: Pisa needs only your Web application
client. `DELETE /v1/connectors/google` disconnects and revokes the grant; `DELETE
/v1/connectors/google/credentials` then forgets your web client (refused with `provider_in_use` while Google is
still connected through it).
