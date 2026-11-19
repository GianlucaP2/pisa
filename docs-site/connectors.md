# Connectors

Every source Pisa reads from is a connector, switched on or off from a client (`PATCH /v1/connectors/{id}`:
your own client, or the PISA app's Settings → Connectors). The list is the code's own (`core/pisa/domain/connectors.py`); each connector has its own settings, and some take a
credential or a consent.

| Connector | What it reads | What it needs | On by default |
|---|---|---|---|
| Google (Gmail + Calendar) | your mail and calendar | your own Google OAuth clients, and a client that implements Google Sign-In ([below](#google)) | yes |
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

## Google

Pisa reads Gmail and Google Calendar with a Google account's permission, through Google's own sign-in. Pisa has
no Google client of its own and no sign-in page: you create the OAuth clients in your own Google Cloud project,
and a client app signs in to Google and hands Pisa a one-time code. Without a client that does this, the Google
connector can't be connected, and mail and calendar tools answer "Google is not connected".

### In Google Cloud (once)

1. A project with the **Gmail API** and the **Google Calendar API** enabled.
2. An OAuth consent screen (Google Auth Platform) that asks for these scopes:
   - needed: `gmail.readonly`, `calendar.readonly`, `calendar.app.created` (Pisa refuses a connection without
     them);
   - optional: `calendar.events` (change your events) and `gmail.compose` (drafts, and sending them). Without
     them Pisa only reads.

   While the consent screen is in testing, add your Google account as a test user. Google's own rules apply:
   in testing, Google expires the refresh token after 7 days, and the client must connect again; Gmail's read
   scope is a restricted scope, so publishing the consent screen for general use needs Google's verification. A
   Google Workspace account can use an Internal consent screen instead. Check Google's current documentation.
3. Two OAuth clients:
   - a **Web application** client: its client id and client secret are what the server exchanges the code
     with;
   - the client your sign-in runs as, for example an **iOS** client. The API takes its id as `ios_client_id`
     (it must differ from the web client's id). The server only stores it and hands it back in
     `GET /v1/connectors/google`, for a client to configure its sign-in.

### On the server (once)

`PUT /v1/connectors/google/credentials` with `{"web_client_id", "web_client_secret", "ios_client_id"}`. Both ids
end in `.apps.googleusercontent.com`. The secret is stored encrypted with `PISA_SECRETS_KEY`.
`GET /v1/connectors/google` then says `"configured": true`.

### What a client must do to connect

1. Run Google Sign-In with **server-side access**: the server client id set to the web client's id, asking for
   the scopes above with offline access. Google's sign-in SDKs for iOS and Android do this and return a
   one-time **server auth code**.
2. Send it at once to `POST /v1/connectors/google/code` with `{"server_auth_code": "<the code>"}`. Pisa exchanges
   it with Google (with an empty redirect URI, as the iOS and Android SDKs' codes need), keeps the refresh token
   encrypted, and starts the first sync. Once the server holds a Face ID key, the request is signed (op
   `google_connect`, target the SHA-256 hex of the code).
3. Show the answer: the account, the scopes granted, and `missing_scopes` (a client can offer to sign in again
   to grant them).

A code is single-use and expires within minutes. A code made for a redirect URI (a web page's sign-in flow)
is not accepted today. `DELETE /v1/connectors/google` disconnects and revokes the grant.

So: mail and calendar **require a client that implements Google Sign-In** as above. The PISA app does; the
command-line client does not.
