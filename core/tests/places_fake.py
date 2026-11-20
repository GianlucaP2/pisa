"""A fake of the place providers' HTTP APIs (Apple Maps Server API, Google Places (New) + Routes, Tavily),
served through httpx.MockTransport from the recorded responses in tests/data/places.

Like the real services it honours the request: Google returns only the fields in the field mask, Apple and
Google answer travel times for the coordinates asked (from a simple speed per mode), and Apple's token is
checked on every call. `fail[route] = (status, body)` makes a route fail once (or `always`).
"""

import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

DATA = Path(__file__).parent / "data" / "places"
SPEED_M_PER_MIN = {"drive": 500, "transit": 300, "walk": 80, "bike": 250}
APPLE_MODE = {"Automobile": "drive", "Transit": "transit", "Walking": "walk", "Cycling": "bike"}
GOOGLE_MODE = {"DRIVE": "drive", "TRANSIT": "transit", "WALK": "walk", "BICYCLE": "bike"}
TRAFFIC_FACTOR = 1.25  # driving with traffic


def load(name: str) -> Any:
    return json.loads((DATA / name).read_text())


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def _matches(query: str, *texts: str) -> bool:
    """Every word of the query is in the name or the address (case and accents don't matter)."""
    from pisa.domain.places import text_key

    haystack = " ".join(text_key(t) for t in texts).split()
    return all(word in haystack for word in text_key(query).split() if len(word) > 2)


def _mask(data: dict[str, Any], mask: str, prefix: str = "") -> dict[str, Any]:
    """Keep only the masked fields (top level), like Google does."""
    fields = {f.removeprefix(prefix) for f in mask.split(",")}
    return {k: v for k, v in data.items() if k in fields}


class FakePlaces:
    def __init__(self) -> None:
        self.requests: list[tuple[str, httpx.Request]] = []
        self.fail: dict[str, tuple[int, Any]] = {}
        self.always: set[str] = set()
        self.apple_tokens = 0
        self.apple_search = load("apple_search.json")
        self.google_search = load("google_text_search.json")
        self.tavily_search = load("tavily_search.json")
        self.tavily_extract = load("tavily_extract.json")
        self.no_route: set[tuple[float, float]] = set()
        self.etas_snap = 0.0

    def routes(self, name: str | None = None) -> list[str]:
        return [r for r, _ in self.requests if name is None or r == name]

    def last(self, name: str) -> httpx.Request:
        return [req for r, req in self.requests if r == name][-1]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = self._route(request)
        self.requests.append((route, request))
        if route in self.fail:
            status, body = self.fail[route] if route in self.always else self.fail.pop(route)
            return httpx.Response(status, json=body)
        return getattr(self, "_" + route.replace(".", "_"))(request)

    @staticmethod
    def _route(request: httpx.Request) -> str:
        host, path = request.url.host, request.url.path
        if host == "maps-api.apple.com":
            return {"/v1/token": "apple.token", "/v1/etas": "apple.etas", "/v1/search": "apple.search",
                    "/v1/reverseGeocode": "apple.reverse", "/v1/geocode": "apple.geocode"}[path]  # fmt: skip
        if host == "places.googleapis.com":
            if path.endswith(":searchText"):
                return "google.search"
            if path.endswith("/media"):
                return "google.media"
            return "google.details"
        if host == "routes.googleapis.com":
            return "google.matrix"
        if host == "api.tavily.com":
            return "tavily." + path.strip("/")
        raise AssertionError(f"unexpected request to {request.url}")

    # --- Apple ---------------------------------------------------------------------------------------------

    def _apple_token(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ey")  # the signed Maps JWT
        self.apple_tokens += 1
        return httpx.Response(
            200, json={"accessToken": f"apple-access-{self.apple_tokens}", "expiresInSeconds": 1800}
        )

    def _apple_auth(self, request: httpx.Request) -> httpx.Response | None:
        if request.headers.get("authorization") != f"Bearer apple-access-{self.apple_tokens}":
            return httpx.Response(401, json={"error": {"message": "Not Authorized", "details": []}})
        return None

    def _apple_etas(self, request: httpx.Request) -> httpx.Response:
        if (denied := self._apple_auth(request)) is not None:
            return denied
        q = parse_qs(urlparse(str(request.url)).query)
        origin = tuple(float(x) for x in q["origin"][0].split(","))
        mode = APPLE_MODE[q.get("transportType", ["Automobile"])[0]]
        etas = []
        for dest_text in q["destinations"][0].split("|"):
            dest = tuple(float(x) for x in dest_text.split(","))
            if dest in self.no_route:
                continue
            metres = _metres(origin, dest)  # type: ignore[arg-type]
            static = round(metres / SPEED_M_PER_MIN[mode] * 60)
            expected = round(static * TRAFFIC_FACTOR) if mode == "drive" else static
            etas.append({
                # Apple may echo the destination snapped to the street: `etas_snap` moves it (degrees).
                "destination": {"latitude": dest[0] + self.etas_snap, "longitude": dest[1]},
                "transportType": q.get("transportType", ["Automobile"])[0].upper(),
                "distanceMeters": round(metres),
                "expectedTravelTimeSeconds": expected,
                "staticTravelTimeSeconds": static,
            })  # fmt: skip
        return httpx.Response(200, json={"etas": etas})

    def _apple_search(self, request: httpx.Request) -> httpx.Response:
        if (denied := self._apple_auth(request)) is not None:
            return denied
        q = parse_qs(urlparse(str(request.url)).query)["q"][0]
        results = [
            r
            for r in self.apple_search["results"]
            if _matches(
                q, r["name"], *r["formattedAddressLines"], r.get("poiCategory", "")
            )  # as Apple: a category
        ]
        return httpx.Response(200, json={**self.apple_search, "results": results})

    def _apple_reverse(self, request: httpx.Request) -> httpx.Response:
        return self._apple_auth(request) or httpx.Response(200, json=load("apple_reverse_geocode.json"))

    def _apple_geocode(self, request: httpx.Request) -> httpx.Response:
        """The addresses for a query, keyed by its normalised text; none for anything else."""
        from pisa.domain.places import text_key

        if (denied := self._apple_auth(request)) is not None:
            return denied
        q = parse_qs(urlparse(str(request.url)).query)["q"][0]
        return httpx.Response(200, json={"results": load("apple_geocode.json").get(text_key(q), [])})

    # --- Google --------------------------------------------------------------------------------------------

    def _google_search(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"]
        mask = request.headers["x-goog-fieldmask"]
        body = json.loads(request.content)
        found = [p for p in self.google_search["places"]
                 if _matches(body["textQuery"], p["displayName"]["text"], p["formattedAddress"])]  # fmt: skip
        places = [_mask(p, mask, "places.") for p in found]
        size = body.get("pageSize", 20)
        return httpx.Response(200, json={"places": places[:size]})

    def _google_media(self, request: httpx.Request) -> httpx.Response:
        """Place Photos getMedia with skipHttpRedirect: the photo's short-lived URL."""
        q = parse_qs(urlparse(str(request.url)).query)
        assert q.get("skipHttpRedirect") == ["true"] and "x-goog-fieldmask" not in request.headers
        name = request.url.path.removeprefix("/v1/").removesuffix("/media")
        width = q["maxWidthPx"][0]
        return httpx.Response(
            200, json={"name": name, "photoUri": f"https://lh3.googleusercontent.com/{name}=w{width}"}
        )

    def _google_details(self, request: httpx.Request) -> httpx.Response:
        mask = request.headers["x-goog-fieldmask"]
        place_id = request.url.path.rsplit("/", 1)[-1]
        if place_id == "ChIJmarioTrattoria0001":
            data = {**load("google_place_details.json"), **load("google_place_reviews.json")}
        elif place_id == "ChIJposteItaliane0002":
            data = load("google_place_details_poste.json")
        else:
            match = next((p for p in self.google_search["places"] if p["id"] == place_id), None)
            if match is None:
                return httpx.Response(
                    404, json={"error": {"code": 404, "message": "Not found", "status": "NOT_FOUND"}}
                )
            data = dict(match)
        return httpx.Response(200, json=_mask(data, mask))

    def _google_matrix(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mode = GOOGLE_MODE[body["travelMode"]]
        origin = body["origins"][0]["waypoint"]["location"]["latLng"]
        out = []
        for i, d in enumerate(body["destinations"]):
            dest = d["waypoint"]["location"]["latLng"]
            if (dest["latitude"], dest["longitude"]) in self.no_route:
                out.append(
                    {"originIndex": 0, "destinationIndex": i, "status": {}, "condition": "ROUTE_NOT_FOUND"}
                )
                continue
            metres = _metres((origin["latitude"], origin["longitude"]), (dest["latitude"], dest["longitude"]))
            static = round(metres / SPEED_M_PER_MIN[mode] * 60)
            traffic = body.get("routingPreference") == "TRAFFIC_AWARE"
            element = {"originIndex": 0, "destinationIndex": i, "status": {}, "condition": "ROUTE_EXISTS",
                       "distanceMeters": round(metres),
                       "duration": f"{round(static * TRAFFIC_FACTOR) if traffic else static}s",
                       "staticDuration": f"{static}s"}  # fmt: skip
            out.append(element)
        return httpx.Response(200, json=out)

    # --- Tavily --------------------------------------------------------------------------------------------

    def _tavily_search(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data = dict(self.tavily_search)
        data["results"] = data["results"][: body.get("max_results", 5)]
        return httpx.Response(200, json=data)

    def _tavily_extract(self, request: httpx.Request) -> httpx.Response:
        urls = json.loads(request.content)["urls"]
        data = dict(self.tavily_extract)
        data["results"] = [r for r in data["results"] if r["url"] in urls]
        return httpx.Response(200, json=data)
