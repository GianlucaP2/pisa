"""Garmin's Personal API (Oct 2026): their own data, pulled once a day with
the key they created on Garmin Connect (Garmin shows it once). The key makes a 24-hour access token (no
refresh token); the key itself expires after 30, 90 or 180 days or a year (they enter the date; Settings shows
it). Pull only; Pisa keeps derived numbers (a `DaySummary` a day), never the raw .FIT files.

The Personal API's reference is shown to key holders only. The paths below are the
program's documented shape at the time of writing and are named in one place (`PATHS`) to be checked against
the reference when they create the key.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from pisa.connectors.sources.errors import SourceError
from pisa.failures import redact

BASE = "https://apis.garmin.com"
PATHS = {
    "token": "/personal/oauth/token",
    "dailies": "/personal/wellness-api/rest/dailies",
    "sleeps": "/personal/wellness-api/rest/sleeps",
    "activities": "/personal/wellness-api/rest/activities",
}
TOKEN_TTL = timedelta(hours=24)
_KINDS = {
    "RUNNING": "running",
    "WALKING": "walking",
    "HIKING": "hiking",
    "CYCLING": "cycling",
    "LAP_SWIMMING": "swimming",
    "OPEN_WATER_SWIMMING": "swimming",
    "STRENGTH_TRAINING": "strength",
    "YOGA": "yoga",
    "PILATES": "pilates",
    "ROWING": "rowing",
    "ELLIPTICAL": "elliptical",
    "TENNIS": "tennis",
    "PADEL": "padel",
    "INDOOR_CLIMBING": "climbing",
}


class GarminPersonal:
    def __init__(self, http: httpx.AsyncClient, *, clock: Any = lambda: datetime.now(UTC)) -> None:
        self._http = http
        self._clock = clock

    async def token(self, secret: dict[str, str]) -> tuple[str, datetime]:
        try:
            response = await self._http.post(
                BASE + PATHS["token"],
                data={"grant_type": "personal_api_key", "api_key": secret["api_key"]},
                timeout=30.0,
            )
        except httpx.HTTPError as err:
            raise SourceError(
                f"Garmin is unreachable ({type(err).__name__}): Pisa tries again tomorrow"
            ) from None
        if response.status_code in (400, 401, 403):
            raise SourceError(
                f"Garmin refused the Personal API key (HTTP {response.status_code}): create a new key on "
                "Garmin Connect and enter it in Settings → Connections → Garmin",
                status=response.status_code,
                auth=True,
            )
        if response.status_code >= 400:
            raise SourceError(f"Garmin answered HTTP {response.status_code}", status=response.status_code)
        body = response.json()
        ttl = int(body.get("expires_in") or TOKEN_TTL.total_seconds())
        return str(body["access_token"]), self._clock() + timedelta(seconds=ttl)

    async def _get(self, path: str, token: str, start: date, end: date) -> list[dict[str, Any]]:
        params = {
            "uploadStartTimeInSeconds": str(
                int(datetime.combine(start, datetime.min.time(), UTC).timestamp())
            ),
            "uploadEndTimeInSeconds": str(int(datetime.combine(end, datetime.min.time(), UTC).timestamp())),
        }
        try:
            response = await self._http.get(
                BASE + path, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
            )
        except httpx.HTTPError as err:
            raise SourceError(
                f"Garmin is unreachable ({type(err).__name__}): Pisa tries again tomorrow"
            ) from None
        if response.status_code in (401, 403):
            raise SourceError(
                "Garmin refused the day's token: Pisa asks for a new one next time",
                status=response.status_code,
                auth=True,
            )
        if response.status_code >= 400:
            raise SourceError(
                redact(f"Garmin answered HTTP {response.status_code}", token),
                status=response.status_code,
            )
        out = response.json()
        return out if isinstance(out, list) else []

    async def days(self, token: str, start: date, end: date) -> list[dict[str, Any]]:
        """Derived numbers a day: sleep minutes and times, steps, active kcal, resting HR, workouts."""
        by_day: dict[str, dict[str, Any]] = {}

        def day(key: Any) -> dict[str, Any] | None:
            if not key:
                return None
            return by_day.setdefault(str(key)[:10], {"day": str(key)[:10], "workouts": []})

        for d in await self._get(PATHS["dailies"], token, start, end):
            row = day(d.get("calendarDate"))
            if row is not None:
                row["steps"] = d.get("steps")
                row["active_kcal"] = d.get("activeKilocalories")
                row["resting_hr"] = d.get("restingHeartRateInBeatsPerMinute")
                if d.get("moderateIntensityDurationInSeconds") is not None:
                    row["exercise_min"] = (
                        int(
                            (d.get("moderateIntensityDurationInSeconds") or 0)
                            + (d.get("vigorousIntensityDurationInSeconds") or 0)
                        )
                        // 60
                    )
        for s in await self._get(PATHS["sleeps"], token, start, end):
            row = day(s.get("calendarDate"))
            seconds = s.get("durationInSeconds")
            if row is not None and seconds is not None:
                row["sleep"] = {"asleep_min": min(int(seconds) // 60, 1440)}
        for a in await self._get(PATHS["activities"], token, start, end):
            started = a.get("startTimeInSeconds")
            if started is None or a.get("durationInSeconds") is None:
                continue
            row = day(datetime.fromtimestamp(int(started), UTC).date().isoformat())
            if row is None or len(row["workouts"]) >= 20:
                continue
            row["workouts"].append(
                {
                    "kind": _KINDS.get(str(a.get("activityType") or "").upper(), "other"),
                    "minutes": max(1, int(a["durationInSeconds"]) // 60),
                    "kcal": a.get("activeKilocalories"),
                    "distance_m": int(a["distanceInMeters"])
                    if a.get("distanceInMeters") is not None
                    else None,
                }
            )
        return [{k: v for k, v in row.items() if v is not None} for row in by_day.values()]
