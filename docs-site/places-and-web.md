# Places and web search

Pisa finds places, works out travel times and searches the web through three providers, each with your own key:
Apple Maps, Google Maps and Tavily. None is built in and none is required. Without them the rest of Pisa works,
and the assistant says plainly which provider is missing ("No provider for place search is ready").

Weather needs no key: it comes from Open-Meteo, which is free.

## Which provider does what

| Provider | Travel times | Place search | Place details (hours, phone, website, reviews, photos) | Web research |
|---|---|---|---|---|
| Apple Maps | yes | yes | no | no |
| Google Maps | yes | yes | yes | no |
| Tavily | no | no | no | yes |

- **Travel times** feed the day plan, "when do I leave", and `GET /v1/places/{id}/travel`.
- **Place search** is the assistant's map search and `GET /v1/places/search?q=...`. Without `q`, that endpoint
  lists the places Pisa already knows nearest to a point, with no provider call.
- **Place details** are the place card's hours, phone, website, review summary and photos
  (`GET /v1/places/{id}/details`). Only Google gives them; Apple Maps gives a name, an address, a point and a
  category.
- **Web research** is the assistant's web search and page reading, the research on a place
  (`POST /v1/places/{id}/research`, which also needs a model in the `places.research` slot), and the searches
  Pisa makes on its own at night. When it reads a page, Pisa first tries to fetch it itself, and uses Tavily
  only if that fails.

For each job Pisa tries the providers in your order and uses the first one that has a key, can do the request
and is under the cap. The default order is Apple, then Google, for travel times and search; Google for details;
Tavily for the web. The order is the `places` section of the settings (`routes`, `search`, `details`, `web`),
changed with `PATCH /v1/settings`.

## Keys: where each one goes

A key goes to the server once, from a paired client (`PUT /v1/places/providers/{provider}/key`). Pisa stores it
encrypted with `PISA_SECRETS_KEY`, so that setting must be set first. Your own client or the PISA app can
offer a settings screen for this. Without a client, `pisa client call` can make the request (see below).
`GET /v1/places/providers` shows each provider's state, and `POST /v1/places/providers/{provider}/test` makes
one real call to check the key.

### Apple Maps

- **What you need:** a paid Apple Developer Program membership, and a Maps private key (a `.p8` file) with
  its 10-character key id, made in your Apple developer account. Pisa uses the Apple Maps Server API.
- **Where it goes:** your Apple team id in `PISA_APNS_TEAM_ID` (the same setting as direct push; the Maps key
  is signed with the same team), and the key itself with:

  ```bash
  pisa secrets set-maps --key-id <the key id> < AuthKey_XXXXXXXXXX.p8
  ```

  or `PUT /v1/places/providers/apple/key` with `{"key": "<the .p8 text>", "key_id": "<the key id>"}`. Delete
  the `.p8` file from the server afterwards.
- **What it costs:** nothing beyond the membership. Apple allows 25,000 calls a day, shared with MapKit JS; Pisa
  counts them and never pays for them.

### Google Maps

- **What you need:** a Google Cloud project with billing turned on, the **Places API (New)** and the
  **Routes API** enabled, and an API key allowed to use them.
- **Where it goes:** `PUT /v1/places/providers/google/key` with `{"key": "<the API key>"}`.
- **What it costs:** each kind of call has a free monthly allowance from Google, and is paid per call above it.
  In Pisa's price table (checked in October 2026; Google's own pricing page is what you are billed by), a paid
  search or place detail costs about 2 to 4 US cents, a photo under 1 cent, and each origin-to-destination pair
  of a travel-time request half a cent to 1 cent (traffic-aware car routes cost more). Searches that return only
  ids are free. Testing the key is free.

### Tavily

- **What you need:** a Tavily account and its API key.
- **Where it goes:** `PUT /v1/places/providers/tavily/key` with `{"key": "<the API key>"}`.
- **What it costs:** 1,000 free credits a month, then 0.8 US cents a credit. A search is one credit; reading
  pages is one credit per 5 pages. Testing the key spends one search, so Pisa refuses the test when that credit
  would be paid and cross your cap.

### From the command line

On a headless install, the command-line client can store a Google or Tavily key. Put the JSON body in a file
readable only by you, so the key is not typed into your shell's history:

```bash
pisa client call PUT /v1/places/providers/tavily/key --data @tavily-key.json   # or --data @- to read stdin
pisa client call POST /v1/places/providers/tavily/test
```

`--data @file` (or `@-` for stdin) reads the body itself, so the key never appears in the command's arguments,
where every process on the machine could see it. Once the server holds a Face ID key from the PISA app, storing a key is a signed
operation (op `place_key`, target `<provider>:<SHA-256 hex of the key>`, see
[Pairing](pairing.md#signed-operations)).

## Spending

Every provider call is counted (`GET /v1/places/usage`: this month's calls, the free ones and the cost, in
euros). Two limits apply:

- **The monthly cap for maps and the web**, `places.monthly_cap_eur` in the settings: 5 euros unless you change
  it (0 to 100). A call that would cost money and cross it is skipped: Pisa uses only the free services until the
  month ends, and sends you one notice a day about it.
- **The hard limit** over every paid service together, `PISA_HARD_DAILY_EUR` and `PISA_HARD_MONTHLY_EUR`
  ([AI providers](ai-providers.md#spending)).

The searches Pisa makes on its own at night use Tavily only, have their own monthly budget of 10 euros, and do
not count against your cap. The hard limit still counts them.

The month is counted in your own time zone. Google bills by Pacific time, so the first and last hours of a month
may be counted in different months by Pisa and by Google.
