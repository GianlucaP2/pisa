"""The attend eval: does `attend.decide` say the right thing at the right moment?

A synthetic week (Monday 26 October to Sunday 1 November 2026, Rome) of what the senses find: reminders and
leave-now, stays, check-ins, emails the user would want, re-plans, rain against a bike ride, Pisa's own
promises; some alone, some arriving together, some in a meeting or at night. Each candidate is labelled with
what a good friend would do: `now`, `breakpoint` (at the next pause), `later` (a time, or the next talk) or
`drop` (not said aloud: dropped or shown on the phone only).

Each case goes to the model as the service sends it (`pisa.brain.attend`), then through code's rails
(`pisa.domain.attend.validate`, with the case's quiet, meeting and the week's running voice count), and what
would reach the user is scored:
- **missed floors** (gate 0): a floor dropped, folded or put after its deadline;
- **unprompted voice per day** within the rate (3 an hour, 12 a day; floors aside);
- **agreement** with the labels (gate >= 75%), and the model's own before the rails.

Through Pisa's own gateway (metered on the evals' own budget), estimated first and refused above €0.50
or on an unknown price; `part="k/n"` runs the k-th of n parts. Numbers only, never the words.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pisa.brain import attend as brain
from pisa.domain import attend as rules
from pisa.domain import talk
from pisa.domain.ai import CHARS_PER_TOKEN
from pisa.infra import ledger

MAX_EUR = 0.50
GATE = {"missed_floors": 0, "agreement": 0.75}
RUNS = 2  # runs differ by ±10 points on 40 cases: the gate is judged on the mean of two
TZ = "Europe/Rome"
Label = Literal["now", "breakpoint", "later", "drop"]
_ROME = ZoneInfo(TZ)


@dataclass(frozen=True)
class Signal:
    source: rules.Source
    text: str
    label: Label
    kind: str = "info"
    urgency: talk.Urgency = "breakpoint"
    floor: bool = False
    deadline_min: int | None = None  # minutes after the case's moment
    alone: bool = False
    must_show: bool = False
    outside: bool = False


@dataclass(frozen=True)
class Case:
    name: str
    at: datetime
    signals: tuple[Signal, ...]
    doing: str | None = None
    quiet: bool = False
    meeting: bool = False
    lang: str = "it-IT"


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return (
        datetime(2026, 10, day, hour, minute, tzinfo=_ROME)
        if day >= 26
        else datetime(2026, 11, day, hour, minute, tzinfo=_ROME)
    )


def _leave(text: str, minutes: int = 0) -> Signal:
    return Signal("leave", text, "now", "reminder", "now", True, minutes or 1)


def _event(text: str, minutes_left: int) -> Signal:
    urgency: talk.Urgency = "now" if minutes_left <= 10 else "breakpoint"
    label: Label = "now" if minutes_left <= 10 else "breakpoint"
    return Signal("reminder", text, label, "reminder", urgency, True, minutes_left - 5)


def _done(text: str, label: Label = "drop") -> Signal:
    return Signal("stay", text, label, "done", alone=True)


def _check(text: str) -> Signal:
    return Signal("check_in", text, "breakpoint", "check_in", alone=True)


def _mail(text: str, label: Label, high: bool = False) -> Signal:
    return Signal("intake", text, label, must_show=high, outside=True)


def _say(source: rules.Source, text: str, label: Label, deadline_min: int | None = None) -> Signal:
    return Signal(source, text, label, deadline_min=deadline_min)


# The week. Floors in every day; batches of two or three; meetings and quiet hours.
# fmt: off
CASES: tuple[Case, ...] = (
    # Monday 26
    Case("mon_dentist", _at(26, 8, 45), (_event("Tra 15 minuti: Dentista (Via Roma 1).", 15),)),
    Case("mon_leave_office", _at(26, 9, 40), (_leave("Esci ora: tram 13 alle 9:52, 25 minuti."),)),
    Case("mon_done_dentist", _at(26, 10, 5), (_done("Fatto: Dentista ✓"),), doing="travel: Ufficio"),
    Case(
        "mon_marco_cover",
        _at(26, 11, 20),
        (_mail("Marco ti chiede: la cover entro stasera.", "breakpoint", high=True),),
        doing="task: Report",
    ),
    Case("mon_newsletter_ask", _at(26, 14, 10), (_mail("Email da Lidl: offerte della settimana.", "drop"),)),
    Case("mon_lunch_missed", _at(26, 15, 0), (_check("Saltato il pranzo?"),)),
    Case(
        "mon_rain_bike",
        _at(26, 17, 10),
        (_say("weather", "Pioggia alle 18:00, hai la bici: un ombrello?", "breakpoint", 45),),
    ),
    # Tuesday 27
    Case(
        "tue_meeting_mail",
        _at(27, 10, 15),
        (_mail("Sara ti chiede: confermi giovedì?", "breakpoint"),),
        doing="event: Riunione con il team",
        meeting=True,
    ),
    Case(
        "tue_meeting_leave",
        _at(27, 11, 40),
        (_leave("Esci ora: metro M1 alle 11:50, 15 minuti."),),
        meeting=True,
    ),
    Case(
        "tue_three",
        _at(27, 13, 5),
        (
            _say("promise", "Ti avevo detto che te lo ricordavo: il pacco.", "breakpoint"),
            _say("replan", "Il piano è cambiato: Palestra oggi non ci sta più.", "breakpoint", 300),
            _say("weather", "Pioggia alle 16:00, vai a piedi: un ombrello?", "breakpoint", 150),
        ),
    ),
    Case("tue_done_bobby", _at(27, 13, 50), (_done("Fatto: BOBBY'S ✓"),)),
    Case("tue_call_in_5", _at(27, 15, 55), (_event("Tra 5 minuti: Chiamata con Nino.", 5),)),
    Case(
        "tue_night_rain",
        _at(27, 23, 30),
        (_say("weather", "Domani pioggia alle 8:00, hai la bici: un ombrello?", "later"),),
        quiet=True,
    ),
    # Wednesday 28
    Case("wed_leave_gym", _at(28, 7, 10), (_leave("Esci ora: bici, 12 minuti."),)),
    Case(
        "wed_bill",
        _at(28, 9, 30),
        (_mail("Enel: la bolletta scade oggi, 84 euro.", "breakpoint", high=True),),
    ),
    Case(
        "wed_promise_next",
        _at(28, 11, 0),
        (_say("promise", "Ti avevo detto che te lo ricordavo: il libro.", "later"),),
    ),
    Case(
        "wed_two",
        _at(28, 12, 40),
        (
            _done("Fatto: Farmacia ✓"),
            _say("replan", "Il piano è cambiato: Spesa oggi non ci sta più.", "breakpoint", 240),
        ),
    ),
    Case(
        "wed_doctor",
        _at(28, 16, 45),
        (_event("Tra 15 minuti: Medico (Via Po 3).", 15),),
        doing="event: Call clienti",
        meeting=True,
    ),
    Case("wed_dinner_check", _at(28, 21, 30), (_check("Sei andato a cena da Paola?"),)),
    # Thursday 29
    Case("thu_leave_train", _at(29, 6, 50), (_leave("Esci ora: treno delle 7:20, 30 minuti."),)),
    Case(
        "thu_train_mail",
        _at(29, 8, 0),
        (_mail("Trenitalia: il tuo treno ha 20 minuti di ritardo.", "breakpoint", high=True),),
    ),
    Case("thu_sara_again", _at(29, 10, 30), (_mail("Sara ti chiede: confermi giovedì?", "breakpoint"),)),
    Case("thu_done_station", _at(29, 12, 0), (_done("Fatto: Stazione ✓"),)),
    Case(
        "thu_three",
        _at(29, 14, 20),
        (
            _say("promise", "Ti avevo detto che te lo ricordavo: chiamare la mamma.", "breakpoint"),
            _done("Fatto: Poste ✓"),
            _mail("Email da Amazon: il pacco arriva domani.", "drop"),
        ),
    ),
    Case("thu_event_5", _at(29, 17, 55), (_event("Tra 5 minuti: Riunione condominio.", 5),)),
    # Friday 30
    Case("fri_dentist_quiet", _at(30, 7, 45), (_event("Tra 15 minuti: Fisioterapia.", 15),), quiet=True),
    Case("fri_leave", _at(30, 8, 40), (_leave("Esci ora: tram 9 alle 8:52, 20 minuti."),)),
    Case("fri_newsletter", _at(30, 11, 0), (_mail("Email da Medium: articoli per te.", "drop"),)),
    Case(
        "fri_marco_urgent",
        _at(30, 12, 15),
        (_mail("Marco ti chiede: richiamalo subito, è urgente.", "breakpoint", high=True),),
    ),
    Case(
        "fri_rain_aperitivo",
        _at(30, 18, 0),
        (_say("weather", "Pioggia alle 19:00, vai a piedi: un ombrello?", "breakpoint", 50),),
    ),
    Case("fri_done_bar", _at(30, 21, 0), (_done("Fatto: Bar Bislacco ✓"),)),
    # Saturday 31
    Case(
        "sat_market",
        _at(31, 9, 30),
        (_say("promise", "Ti avevo detto che te lo ricordavo: le uova.", "breakpoint"),),
    ),
    Case("sat_leave_lunch", _at(31, 12, 30), (_leave("Esci ora: a piedi, 15 minuti."),)),
    Case(
        "sat_two_mails",
        _at(31, 15, 0),
        (
            _mail("Email da Booking: conferma per domenica.", "drop"),
            _mail("Nino ti chiede: vieni stasera?", "breakpoint"),
        ),
    ),
    Case("sat_cinema", _at(31, 20, 45), (_event("Tra 15 minuti: Cinema.", 15),)),
    # Sunday 1
    Case("sun_quiet_mail", _at(1, 7, 30), (_mail("Email da Netflix: novità.", "drop"),), quiet=True),
    Case(
        "sun_promise_time",
        _at(1, 11, 0),
        (_say("promise", "Ti avevo detto che te lo ricordavo: le piante.", "breakpoint"),),
    ),
    Case("sun_leave", _at(1, 12, 45), (_leave("Esci ora: bici, 18 minuti."),)),
    Case(
        "sun_replan",
        _at(1, 16, 0),
        (_say("replan", "Il piano è cambiato: Corsa oggi non ci sta più.", "later", 120),),
    ),
    Case("sun_lunch_check", _at(1, 15, 0), (_check("Sei andato a pranzo da Nino?"),)),
)
# fmt: on


def select(part: str | None) -> list[Case]:
    """`"k/n"`: the k-th of n parts of the week (in order: the voice count runs within a part)."""
    if not part:
        return list(CASES)
    parsed = re.fullmatch(r"([1-9][0-9]?)/([1-9][0-9]?)", part.strip())
    if parsed is None or int(parsed[1]) > int(parsed[2]):
        raise ValueError(f"part is k/n with 1 <= k <= n < 100, not {part[:20]!r}")
    k, n = int(parsed[1]), int(parsed[2])
    return [c for i, c in enumerate(CASES) if i % n == k - 1]


def candidates(case: Case) -> list[rules.Candidate]:
    return [
        rules.Candidate(
            f"{case.name}.{n}", s.source, s.kind, s.text, case.lang, case.at, s.urgency,
            case.at + timedelta(minutes=s.deadline_min) if s.deadline_min is not None else None,
            s.floor, s.outside, s.alone, s.must_show,
        )
        for n, s in enumerate(case.signals)
    ]  # fmt: skip


def moment(case: Case, said: Sequence[str]) -> brain.Moment:
    body = "app background, glasses off, earbuds connected, companion on"
    return brain.Moment(
        now=case.at, timezone=TZ, lang=case.lang, body=body, doing=case.doing, said=list(said)
    )


def quiet_at(moment: datetime) -> bool:
    """The week's quiet hours (the default setting, 22:00-07:00), for an `at` Pisa picks."""
    local = moment.astimezone(_ROME)
    return local.hour >= 22 or local.hour < 7


def label_of(decision: rules.Decision) -> Label:
    """What reached the user, in the labels' terms (a phone-only show is no interruption: drop)."""
    if decision.choice in ("at", "fold_next"):
        return "later"
    if decision.choice in ("drop", "show_silently"):
        return "drop"
    return decision.choice  # now, breakpoint


def prompt_chars(case: Case) -> int:
    return brain.prompt_chars(candidates(case), lang="it-IT")


Price = Callable[[int, int], Awaitable[Decimal | None]]
Decide = Callable[[Case, list[rules.Candidate], brain.Moment], Awaitable[list[rules.Decision]]]


async def estimate(price: Price, cases: Sequence[Case]) -> Decimal | None:
    total = Decimal(0)
    for case in cases:
        one = await price(prompt_chars(case) // CHARS_PER_TOKEN, brain.MAX_OUTPUT_TOKENS)
        if one is None:
            return None
        total += one
    return total


@dataclass
class Score:
    candidates: int = 0
    agreed: int = 0
    agreed_raw: int = 0
    floors: int = 0
    missed_floors: int = 0
    floor_drops_attempted: int = 0
    by_rules: int = 0
    merged: int = 0
    rails: dict[str, int] = field(default_factory=dict)
    voice_by_day: dict[str, int] = field(default_factory=dict)
    max_voice_hour: int = 0
    confusion: dict[str, int] = field(default_factory=dict)
    timeouts: int = 0
    seconds: list[float] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        agreement = round(self.agreed / self.candidates, 3) if self.candidates else 0.0
        within = max(self.voice_by_day.values(), default=0) <= talk.VOICE_PER_DAY and (
            self.max_voice_hour <= talk.VOICE_PER_HOUR
        )
        return {
            "cases": len(self.results), "candidates": self.candidates, "agreement": agreement,
            "agreement_model_raw": round(self.agreed_raw / self.candidates, 3) if self.candidates else 0.0,
            "floors": self.floors, "missed_floors": self.missed_floors,
            "floor_drops_attempted": self.floor_drops_attempted,
            "unprompted_voice_per_day": dict(sorted(self.voice_by_day.items())),
            "unprompted_voice_max_hour": self.max_voice_hour, "voice_within_rate": within,
            "decided_by_rules": self.by_rules, "merged_decisions": self.merged,
            "rails": dict(sorted(self.rails.items())),
            "confusion": dict(sorted(self.confusion.items())), "timeouts": self.timeouts,
            "decide_s_p50": round(sorted(self.seconds)[len(self.seconds) // 2], 2) if self.seconds else None,
            "gate": {"missed_floors": self.missed_floors == 0, "agreement": agreement >= GATE["agreement"]},
        }  # fmt: skip


async def evaluate(decide: Decide, cases: Sequence[Case]) -> dict[str, Any]:
    """Each case decided, validated with the week's running voice count, and scored; a failed call leaves the
    case to today's rules (as in the service) and is counted."""
    score = Score()
    spoken: list[datetime] = []  # unprompted voice so far this run (floors aside)
    said: list[tuple[datetime, str]] = []
    for case in sorted(cases, key=lambda c: c.at):
        members = candidates(case)
        by_id = {c.id: c for c in members}
        labels = {f"{case.name}.{n}": s.label for n, s in enumerate(case.signals)}
        day_start = case.at.replace(hour=0, minute=0)
        hour = sum(1 for t in spoken if case.at - t < timedelta(hours=1))
        today = sum(1 for t in spoken if t >= day_start)
        recent = [f"{k} at {t:%H:%M}" for t, k in said if case.at - t < timedelta(hours=2)]
        # As the service decides: floors by today's rules at once; Pisa's own items and an
        # email's in separate calls, side by side, each within the service's budget.
        made: list[rules.Decision] = [rules.rules(c) for c in members if c.floor]
        rest = [c for c in members if not c.floor]
        groups = [g for g in ([c for c in rest if not c.outside], [c for c in rest if c.outside]) if g]
        seen = moment(case, recent)

        async def one(
            group: list[rules.Candidate], case: Case = case, seen: brain.Moment = seen
        ) -> tuple[list[rules.Decision], str | None]:
            started = time.monotonic()
            try:  # the service's own timeout: a slow answer is today's rules there, so here
                return await asyncio.wait_for(
                    decide(case, group, seen), rules.DECIDE_TIMEOUT_NO_FLOOR_S
                ), None
            except TimeoutError:
                score.timeouts += 1
                return [], "timeout"
            except Exception as err:
                return [], getattr(err, "code", type(err).__name__)
            finally:
                score.seconds.append(time.monotonic() - started)

        failed = None
        for answer, problem in await asyncio.gather(*(one(g) for g in groups)):
            made += answer
            failed = failed or problem
        raw = {i: d for d in rules.cover(made, members) for i in d.ids}
        ctx = rules.Context(now=case.at, quiet=case.quiet, meeting=case.meeting,
                            voice_ok=talk.voice_allowed(False, hour, today), quiet_at=quiet_at)  # fmt: skip
        result: dict[str, Any] = {"failed": failed} if failed else {}
        for decision in rules.cover(made, members):
            group = [by_id[i] for i in decision.ids]
            final = rules.validate(decision, group, ctx)
            score.by_rules += final.by == "rules"
            score.merged += len(group) > 1
            for rail in final.rails:
                score.rails[rail] = score.rails.get(rail, 0) + 1
            voice_floor = any(talk.is_floor(c.kind, c.urgency) for c in group)
            reaches = final.choice in ("now", "breakpoint", "at") and rules.spoken(final.channel)
            if reaches and not voice_floor and not case.quiet:
                spoken.append(final.at or case.at)
            if final.choice != "drop":
                said.append((case.at, group[0].source))
            for c in group:
                got, want = label_of(final), labels[c.id]
                score.candidates += 1
                score.agreed += got == want
                score.agreed_raw += label_of(raw[c.id]) == want
                score.confusion[f"{want}→{got}"] = score.confusion.get(f"{want}→{got}", 0) + 1
                if c.floor:
                    score.floors += 1
                    score.floor_drops_attempted += raw[c.id].choice in ("drop", "fold_next")
                    late = final.at is not None and c.deadline is not None and final.at > c.deadline
                    # a now-floor downgraded (a pause, a silent channel) is missed too
                    downgraded = c.urgency == "now" and (
                        final.choice != "now" or not rules.spoken(final.channel)
                    )
                    score.missed_floors += (
                        final.choice in ("drop", "fold_next", "show_silently") or late or downgraded
                    )
                result[c.id.split(".", 1)[1]] = f"{want}→{got}"
        score.results[case.name] = result
    for t in spoken:
        key = t.astimezone(_ROME).date().isoformat()
        score.voice_by_day[key] = score.voice_by_day.get(key, 0) + 1
        in_hour = sum(1 for u in spoken if timedelta(0) <= u - t < timedelta(hours=1))
        score.max_voice_hour = max(score.max_voice_hour, in_hour)
    return score.summary() | {"results": score.results}


@ledger.on_eval_budget  # the evals' own budget, never the user's caps or guard
async def run(
    runtime: Any,
    price: Price,
    *,
    part: str | None = None,
    max_eur: float = MAX_EUR,
    call: bool = True,
    runs: int = RUNS,
) -> dict[str, Any]:
    """Estimate, then (when `call` and within `max_eur`) ask the slot's model for each case, `runs` times; the
    gate on the mean agreement and on every run's missed floors."""
    cases = select(part)
    one_run = await estimate(price, cases)
    planned = one_run * runs if one_run is not None else None
    eur = float(round(planned, 4)) if planned is not None else None
    out: dict[str, Any] = {"cases": len(cases), "part": part, "runs": runs, "estimate_eur": eur}
    if planned is None:
        return out | {"refused": "a price is unknown: nothing was called"}
    if planned > Decimal(str(max_eur)):
        return out | {"refused": f"estimated €{planned:.3f} is above €{max_eur:.2f}: nothing was called"}
    if not call:
        return out
    measured: list[str] = []

    async def decide(case: Case, members: list[rules.Candidate], seen: brain.Moment) -> list[rules.Decision]:
        async def one(ctx: Any) -> list[rules.Decision]:
            if measured and str(ctx.ref) != measured[0]:
                raise OtherModel(str(ctx.ref))  # never scored on another model
            measured[:1] = [str(ctx.ref)]
            made, usage = await brain.decide(ctx.model, members, seen, lang="it-IT")
            ctx.usage.update(usage)
            return made

        return await runtime.gateway.run(
            brain.SLOT, one, prompt_chars=prompt_chars(case), max_output_tokens=brain.MAX_OUTPUT_TOKENS,
            can_fallback=lambda: False,
        )  # fmt: skip

    each = [await evaluate(decide, cases) for _ in range(runs)]
    return out | {"model": measured[0] if measured else None} | combined(each)


def combined(each: list[dict[str, Any]]) -> dict[str, Any]:
    """Several runs as one result: each run's numbers, the mean agreement, every missed floor; the gate on the
    mean."""
    mean = round(sum(r["agreement"] for r in each) / len(each), 3)
    missed = sum(r["missed_floors"] for r in each)
    return {
        "agreement_mean": mean,
        "missed_floors_total": missed,
        "each": each,
        "gate": {"missed_floors": missed == 0, "agreement": mean >= GATE["agreement"]},
    }


class OtherModel(Exception):
    code = "other_model"
