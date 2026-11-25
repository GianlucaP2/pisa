# The phone's own maps (contract 2.3)

Pisa can answer place questions ("a pharmacy open near me?", "how long to walk there?") with the phone's own
maps service, so the server needs no maps key of its own. It asks over `/v2/live`; the phone looks it up and
answers. This is contract 2.3, the extension `pisa.maps` (major 1) and the feature `maps.phone`.

## Turning it on

List `pisa.maps` among the extensions in the `hello`:

```json
{"op": "hello", "contract": {"versions": ["2.0", "2.1", "2.2", "2.3"]}, "agui": {"protocolVersions": ["1.0"]},
 "rings": [0, 1], "extensions": {"pisa.notice": 1, "pisa.surface": 1, "pisa.approval": 1, "pisa.maps": 1},
 "device": {"deviceId": "5b0e4c1e-8a7d-4c4b-9a39-2f4f1c9d7e10", "appVersion": "1.4.0", "platform": "ios"}}
```

A server that grants it in the `welcome` (and lists the feature `maps.phone` in `GET /v1/server`) may send the
two requests below in a turn's run. A client that did not list it never gets them.

## What the server asks

| Event | Value | |
|---|---|---|
| `pisa.maps.search` | `{requestId, query, near?, kind?, limit?}` | look places up. `query`: 1 to 200 characters. `near`: `"here"` or `{lat, lon}`; absent, wherever the phone's maps search by default. `kind`: `restaurant`, `cafe`, `pharmacy`, `fuel`, `supermarket`, `atm`, `parking` or `other`. `limit`: 1 to 10 (default 5) |
| `pisa.maps.eta` | `{requestId, from, to, mode}` | a travel time. `from` and `to`: `"here"`, `{lat, lon}`, or `{placeId}` from one of this phone's earlier results. `mode`: `walk`, `transit`, `drive` or `bike` (the same as `pisa.directions`) |

`"here"` means the phone's own location: the phone reads it and does not send it back.

## What the phone answers

`{"op": "custom", "name": "pisa.maps.result", "value": ...}`, at once, with the same `requestId`:

| Field | |
|---|---|
| `requestId` | the request's |
| `ok` | `true` with `places` (for a search) or `eta` (for an ETA); `false` with `reason` |
| `places` | 1 to 10 `{id, name, lat, lon, address?, kind?, distanceMeters?, openNow?, phone?, url?}` (`$defs/MapsPlace`). `id`: the maps service's own id, 1 to 200 of `A-Z a-z 0-9 . _ : ~ -`; `name` up to 120 characters, `address` up to 200, no control or bidi characters; `phone` E.164; `url` `https://` only, up to 500 |
| `eta` | `{seconds, distanceMeters, mode, departAt?}` (`$defs/MapsEta`) |
| `reason` | `denied` (location or maps not allowed), `unavailable` (the maps service failed or is offline), `no_results`, `timeout`, `unsupported` |

No other field is allowed, anywhere in it. Past a few seconds without an answer Pisa says it couldn't look.

```json
{"op": "custom", "name": "pisa.maps.result", "value": {"requestId": "3f2a1b0c-9d8e-4f7a-8b6c-5d4e3f2a1b0c", "ok": true,
 "places": [{"id": "I5E0A1B2C3D4F5061", "name": "Farmacia Esempio", "address": "Via Esempio 1, Testville",
             "lat": 45.001, "lon": 9.003, "kind": "pharmacy", "distanceMeters": 420, "openNow": true}]}}
```

## Privacy and trust

- The answer is untrusted input to the server: a place's name, address and url are outside text, read as data
  and never as instructions, and the server never fetches the url. A result out of these bounds is refused
  (`invalid_frame`).
- Neither side writes a coordinate to a log, the request's or the result's.
- Send only what was asked: no more than `limit` places, nothing about the user beyond the answer.

The fixtures are in `protocol/v2/fixtures/gaps/{valid,invalid}/maps_*.json`; the kit checks them
([Testing your client](testing.md)).
