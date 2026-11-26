# Places and web search

Pisa finds places and works out travel times in this order, and uses the first that answers:

1. **Your phone's own maps.** No key and nothing to set up: an app that speaks contract 2.3 and lists the
   `pisa.maps` extension in its hello (the PISA app does) looks the place or the travel time up with the
   phone's maps service, inside the turn, and sends the answer back. If the phone says no (location or maps
   not allowed, offline, nothing found, not supported) or doesn't answer within 4 seconds, Pisa moves on.
2. **Google Maps, with your key** (and Apple Maps, with yours: both in the order you choose). The richest
   answers: ratings, opening hours, reviews, photos, live traffic.
3. **OpenStreetMap, free.** Place search with no key at all; travel times on foot, by bike or by car with
   your own free OpenRouteService key. Its data comes with a credit you see next to it (below).
4. **Nothing set up**: the assistant says plainly that places aren't set up, instead of guessing.

Public transport times come only from the phone or Google: OpenRouteService has none. Web research has its own
provider, Tavily, with your key. Weather needs no key: it comes from Open-Meteo, which is free.

## Which provider does what

| Provider | Travel times | Place search | Place details (hours, phone, website, reviews, photos) | Web research | Key |
|---|---|---|---|---|---|
| Your phone's maps | yes, leaving now (walk, transit, car, bike) | yes | no | no | none |
| Apple Maps | yes | yes | no | no | yours |
| Google Maps | yes | yes | yes | no | yours |
| OpenStreetMap | with OpenRouteService (walk, bike, car) | yes | no | no | none; OpenRouteService: yours, free |
| Tavily | no | no | no | yes | yours |

- **Travel times** feed the day plan, "when do I leave", and `GET /v1/places/{id}/travel`.
- **Place search** is the assistant's map search and `GET /v1/places/search?q=...`. Without `q`, that endpoint
  lists the places Pisa already knows nearest to a point, with no provider call.
- **Place details** are the place card's hours, phone, website, review summary and photos
  (`GET /v1/places/{id}/details`). Only Google gives them; the others give a name, an address, a point and a
  category.
- **Web research** is the assistant's web search and page reading, the research on a place
  (`POST /v1/places/{id}/research`, which also needs a model in the `places.research` slot), and the searches
  Pisa makes on its own at night. When it reads a page, Pisa first tries to fetch it itself, and uses Tavily
  only if that fails.

### Your phone first

The phone is asked only during a conversation turn on the live connection (`/v2/live`) of an app that was
granted `pisa.maps` in the server's welcome; a turn over plain HTTP (`POST /v2/runs`) or Pisa's own background
work never asks it. Each request carries a fresh id, and only that phone's answer for that id, within 4
seconds, counts: anything else is dropped. What the phone sends back is untrusted: checked against the
contract's bounds (at most 10 places, names and addresses without control characters, `https` links only),
handed to the assistant as data and never as instructions, and never written to a log (neither the search nor
a place nor a coordinate). The phone's own place ids stay on the phone. How a client answers:
[The phone's own maps](build-a-client/maps.md). `PISA_PHONE_MAPS_ENABLED=0` turns this step off (it is off in
review mode too); discovery then no longer lists the feature `maps.phone`.

### Your order, and the free fallback

For each job Pisa tries the providers in your order and uses the first one that has a key, can do the request
and is under the cap. The default order is Apple, then Google, for travel times and search; Google for details;
Tavily for the web. The order is the `places` section of the settings (`routes`, `search`, `details`, `web`),
changed with `PATCH /v1/settings`. The phone always comes before it, and OpenStreetMap (search) or
OpenRouteService (travel times) after it; a fallback that isn't set up is skipped silently.

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

- **What you need:** a Google Cloud project with billing turned on, the **Places API (New)**
  (<https://console.cloud.google.com/apis/library/places.googleapis.com>) and the **Routes API**
  (<https://console.cloud.google.com/apis/library/routes.googleapis.com>) enabled, and an API key restricted to
  them (<https://console.cloud.google.com/apis/credentials>). It is an API key, never a permission of your
  Google sign-in. `pisa google setup --gcloud --maps` can enable the two APIs for you
  ([Connectors](connectors.md#google)).
- **Where it goes:** `PUT /v1/places/providers/google/key` with `{"key": "<the API key>"}`.
- **Before Pisa calls it:** your OK of Google Maps and its own monthly cap, in Settings → Connections
  ([the paid OK](connections.md#the-paid-ok)). Until then Google Maps is never called, not even to test the
  key.
- **What it costs:** each kind of call has a free monthly allowance from Google, and is paid per call above it.
  In Pisa's price table (checked in October 2026; Google's own pricing page is what you are billed by), a paid
  search or place detail costs about 2 to 4 US cents, a photo under 1 cent, and each origin-to-destination pair
  of a travel-time request half a cent to 1 cent (traffic-aware car routes cost more). Searches that return only
  ids are free. Testing the key is free.

### Tavily

- **What you need:** a Tavily account and its API key.
- **Where it goes:** `PUT /v1/places/providers/tavily/key` with `{"key": "<the API key>"}`.
- **Before Pisa calls it:** your OK of Tavily and its own monthly cap, in Settings → Connections
  ([the paid OK](connections.md#the-paid-ok)); the night's searches wait for it too.
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

## OpenStreetMap

OpenStreetMap is the free fallback for places, after your phone and Google: no account and no key for search,
and your own free key for travel times. Map data © OpenStreetMap contributors, under the
[ODbL](https://www.openstreetmap.org/copyright). Every place it names carries that credit for your client to
show next to it: `attribution` on a search result (`GET /v1/places/search`) and on the place
(`GET /v1/places/{id}`), and a footnote under the places card the assistant shows. Pisa keeps its element's id
(`node/123`) to recognise the place next time.

| Job | Service | Key | Settings |
|---|---|---|---|
| Place search | [Photon](https://photon.komoot.io) (komoot), or [Nominatim](https://nominatim.org) | none | `PISA_OSM_GEOCODER`, `PISA_OSM_PHOTON_URL`, `PISA_OSM_NOMINATIM_URL` |
| Places of a kind nearby (a pharmacy, fuel, a cash machine) | [Overpass](https://overpass-api.de) | none | `PISA_OSM_OVERPASS_URL` |
| Travel times (on foot, by bike, by car) | [OpenRouteService](https://openrouteservice.org) | yours, free | `PISA_OPENROUTESERVICE_URL` |

`PISA_OSM_ENABLED=0` turns all of it off: Pisa then calls no OpenStreetMap service. The settings are in
[Configuration](configuration.md).

### What is sent where

OpenStreetMap is **on by default** (`PISA_OSM_ENABLED=1`): with nothing set up, Pisa's place searches go to
the public Photon, Nominatim and Overpass services, third parties run by volunteers, unless you point the
`PISA_OSM_*_URL` settings at your own instances ([below](#self-hosting-the-backends)). Connections says the
same in its `data_out` ([Connections](connections.md#what-each-entry-says)).

- **Search**: the words of the search, and a point rounded to 3 decimals (about 100 m) to prefer results near
  you. Nothing else: no account, no name, no id of yours.
- **Nearby**: a kind from a fixed list (`pharmacy`, `fuel`, `atm`, `bank`, `supermarket`, …), a radius from
  100 m to 5 km, and the centre rounded to 3 decimals.
- **Travel times**: the real start and end points, to 5 decimals (about 1 m: a route needs them, and one end
  can be your home), the mode, and your key in the `Authorization` header. OpenRouteService has no public transport: a transit time comes from another
  provider or not at all.
- Every request says what it is in its User-Agent: `PISA/<version>`, and `PISA_OSM_PROJECT_URL` when you set
  it. Never your name or an email address.
- No redirect is followed, so a request never ends up at a host you didn't configure. What comes back is
  treated as untrusted: names and addresses are cleaned of control and invisible characters and cut to
  length, and the number of results is capped (10 for a search, 20 nearby).

### The public instances' rules

The public Photon, Nominatim and Overpass servers are run by volunteers. Pisa keeps to their usage policies on
its own: at most **one request a second** to each, for the whole server, and a cache, so the same search near
the same place (within about 1 km) is answered from memory for 6 hours instead of being asked again. If you
search a lot, run your own.

### Self-hosting the backends

Photon, Nominatim and Overpass can all run on your own machine (each project documents its Docker image and
the regional extract to import), and so can OpenRouteService. Point the matching `PISA_OSM_*_URL` (or
`PISA_OPENROUTESERVICE_URL`) at it, e.g. `PISA_OSM_PHOTON_URL=http://photon.lan:2322`: your searches then
never leave your network. An address on your own network is allowed here, since you set it, not a user.

### The OpenRouteService key

- **What you need:** a free account at openrouteservice.org and its API key (the free plan allows a few
  hundred travel-time requests a day).
- **Where it goes:** `PUT /v1/places/providers/openrouteservice/key` with `{"key": "<the API key>"}`, stored
  encrypted like the Google and Tavily keys.
- **Without it:** OpenStreetMap still searches; travel times from it are "not configured", which Pisa says
  plainly rather than as a failure.

## Spending

Every provider call is counted (`GET /v1/places/usage`: this month's calls, the free ones and the cost, in
euros). Three limits apply:

- **The paid OK of each paid provider** (Google Maps, Tavily): its own monthly cap in cents, given in Settings →
  Connections ([Connections](connections.md#the-paid-ok)). Without it the provider is never called; past it,
  the provider is skipped until the month ends. It counts every call to that provider, the night's included.
- **The monthly cap for maps and the web**, `places.monthly_cap_eur` in the settings: 5 euros unless you change
  it (0 to 100). A call that would cost money and cross it is skipped: Pisa uses only the free services until the
  month ends, and sends you one notice a day about it.
- **The hard limit** over every paid service together, `PISA_HARD_DAILY_EUR` and `PISA_HARD_MONTHLY_EUR`
  ([AI providers](ai-providers.md#spending)).

The searches Pisa makes on its own at night use Tavily only, have their own monthly budget of 10 euros, and do
not count against your cap. The hard limit still counts them.

The month is counted in your own time zone. Google bills by Pacific time, so the first and last hours of a month
may be counted in different months by Pisa and by Google.
