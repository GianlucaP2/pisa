"""Their health and sport days on Pisa's server, derived numbers only. Pure rules, no I/O.

- **What arrives** (`DaySummary`): the phone's daily summary from Apple Health (sleep, steps, workouts,
resting
  HR and HRV, VO2 max) and Garmin's daily pull map to the same shape. Every model forbids extra fields, so a
  raw sample, a medication's name, cycle data or a place is refused at the door (400), never stored.
  Medications are only "taken / missed" counts.
- **What Pisa says** (`describe`): describe and compare with their own recent days, never interpret:
  no condition named, no threshold, no "abnormal", no advice on doses.
  `FORBIDDEN` is the list a test holds every line against.
- **Places**: nothing here carries one; a workout is a kind, minutes and numbers.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, time
from statistics import mean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WorkoutKind = Literal[
    "running",
    "walking",
    "hiking",
    "cycling",
    "swimming",
    "strength",
    "hiit",
    "yoga",
    "pilates",
    "rowing",
    "elliptical",
    "skiing",
    "tennis",
    "padel",
    "football",
    "basketball",
    "climbing",
    "dance",
    "martial_arts",
    "other",
]

# The words Pisa never says about their body (it, en): a condition's name, a verdict, a threshold's word,
# advice on a dose. A test holds every line `describe` can make against it (synthetic extremes included).
FORBIDDEN = (
    "apnea",
    "apnee",
    "ipertension",
    "hypertension",
    "ipotension",
    "hypotension",
    "aritmi",
    "arrhythm",
    "fibrillazion",
    "fibrillation",
    "tachicardi",
    "tachycardi",
    "bradicardi",
    "bradycardi",
    "insonni",
    "insomni",
    "diabet",
    "depress",
    "ansia",
    "anxiety",
    "obes",
    "malatti",
    "disease",
    "diagnos",
    "patologi",
    "patholog",
    "anormal",
    "abnormal",
    "sintom",
    "symptom",
    "malato",
    "illness",
    "sick",
    "disturbo",
    "disorder",
    "rischio cardi",
    "cardiac risk",
    "dose",
    "dosaggio",
    "dosage",
    "raddoppia",
    "double the",
    "salta la",
    "skip the",
    "terapia",
    "therapy",
    "infezion",
    "infection",
    "febbre",
    "fever",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Sleep(_Strict):
    asleep_min: int = Field(ge=0, le=1440, description="Minutes asleep (the phone sums the stages)")
    in_bed_min: int | None = Field(default=None, ge=0, le=1440)
    bedtime: time | None = Field(default=None, description="Local time they fell asleep")
    wake: time | None = Field(default=None, description="Local time they woke up")


class Workout(_Strict):
    kind: WorkoutKind
    minutes: int = Field(ge=1, le=1440)
    kcal: int | None = Field(default=None, ge=0, le=20000)
    distance_m: int | None = Field(default=None, ge=0, le=1_000_000)


class Doses(_Strict):
    """Only whether scheduled doses were taken or missed; never a medication's name."""

    taken: int = Field(ge=0, le=100)
    missed: int = Field(ge=0, le=100)


class DaySummary(_Strict):
    day: date = Field(description="Their local day the summary is for")
    sleep: Sleep | None = None
    steps: int | None = Field(default=None, ge=0, le=200000)
    active_kcal: int | None = Field(default=None, ge=0, le=20000)
    exercise_min: int | None = Field(default=None, ge=0, le=1440)
    workouts: list[Workout] = Field(default_factory=list, max_length=20)
    resting_hr: int | None = Field(default=None, ge=20, le=250)
    hrv_sdnn_ms: int | None = Field(default=None, ge=1, le=400, description="Apple's HRV is SDNN, in ms")
    vo2max: float | None = Field(default=None, ge=5, le=99)
    doses: Doses | None = None


@dataclass(frozen=True)
class Day:
    day: date
    source: str
    sleep_min: int | None
    bedtime: time | None
    wake_time: time | None
    steps: int | None
    exercise_min: int | None
    resting_hr: int | None
    hrv_sdnn_ms: int | None
    vo2max: float | None
    workouts: list[dict[str, object]]
    doses_taken: int | None
    doses_missed: int | None


def _hm(minutes: float) -> str:
    m = round(minutes)
    return f"{m // 60}h{m % 60:02d}"


def _avg(values: Sequence[float | None]) -> float | None:
    got = [float(v) for v in values if v is not None]
    return mean(got) if got else None


def _versus(now: float | None, before: float | None, unit: str, it: bool) -> str:
    """A plain comparison with their own earlier days (a percentage), or nothing."""
    if now is None or before is None or before == 0:
        return ""
    pct = round((now - before) / before * 100)
    if abs(pct) < 3:
        return " (come nei giorni prima)" if it else " (as on the days before)"
    if it:
        return f" ({abs(pct)}% {'sopra' if pct > 0 else 'sotto'} la tua media{unit})"
    return f" ({abs(pct)}% {'above' if pct > 0 else 'below'} your average{unit})"


def describe(days: Sequence[Day], earlier: Sequence[Day], lang: str) -> list[str]:
    """The days asked (newest first), each compared with their own earlier days: numbers, never a verdict."""
    it = not lang.startswith("en")
    if not days:
        return []
    out: list[str] = []
    base_sleep = _avg([d.sleep_min for d in earlier])
    base_steps = _avg([d.steps for d in earlier])
    base_rhr = _avg([d.resting_hr for d in earlier])
    base_hrv = _avg([d.hrv_sdnn_ms for d in earlier])
    for d in days:
        parts: list[str] = []
        if d.sleep_min is not None:
            when = ""
            if d.bedtime and d.wake_time:
                when = (
                    f", dalle {d.bedtime:%H:%M} alle {d.wake_time:%H:%M}"
                    if it
                    else f", {d.bedtime:%H:%M} to {d.wake_time:%H:%M}"
                )
            label = "sonno" if it else "sleep"
            parts.append(f"{label} {_hm(d.sleep_min)}{when}{_versus(d.sleep_min, base_sleep, '', it)}")
        if d.steps is not None:
            label = "passi" if it else "steps"
            parts.append(f"{label} {d.steps}{_versus(d.steps, base_steps, '', it)}")
        if d.exercise_min is not None:
            parts.append(f"{'minuti di esercizio' if it else 'exercise minutes'} {d.exercise_min}")
        for w in d.workouts[:5]:
            extra = f", {round(int(str(w['distance_m'])) / 1000, 1)} km" if w.get("distance_m") else ""
            parts.append(f"{'allenamento' if it else 'workout'} {w['kind']} {w['minutes']} min{extra}")
        if d.resting_hr is not None:
            label = "battito a riposo" if it else "resting heart rate"
            parts.append(f"{label} {d.resting_hr} bpm{_versus(d.resting_hr, base_rhr, '', it)}")
        if d.hrv_sdnn_ms is not None:
            parts.append(f"HRV (SDNN) {d.hrv_sdnn_ms} ms{_versus(d.hrv_sdnn_ms, base_hrv, '', it)}")
        if d.vo2max is not None:
            parts.append(f"VO2 max {d.vo2max:.1f}")
        if d.doses_taken is not None or d.doses_missed is not None:
            taken, missed = d.doses_taken or 0, d.doses_missed or 0
            parts.append(
                f"{'promemoria farmaci' if it else 'medication reminders'}: "
                f"{taken} {'presi' if it else 'taken'}, {missed} {'non segnati' if it else 'not logged'}"
            )
        source = {"apple_health": "Apple Health", "garmin": "Garmin"}.get(d.source, d.source)
        out.append(f"{d.day.isoformat()} ({source}): " + ("; ".join(parts) if parts else "—"))
    return out


def clean(line: str) -> bool:
    """True when a line names none of the forbidden words."""
    low = line.lower()
    return not any(word in low for word in FORBIDDEN)
