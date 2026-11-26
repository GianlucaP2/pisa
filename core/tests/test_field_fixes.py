"""Field fixes: no errand outside the planner day, email-task places only where the user must go,
the first sync's tasks re-triaged, and the queued items."""

from datetime import timedelta

import pytest

from tests.assistant_env import add_task, rome, set_settings
from tests.places_env import HERE_MILAN, at_location
from tests.test_planner import OFFICE, place, run, with_place

# The user asks Pisa to plan every task (Pisa never times one by itself)
pytestmark = pytest.mark.usefixtures("owner_plans_every_task")

# --- no errand outside the planner day --------------------------------------------------------------------


def errand_plan(db, secrets_key, end: str, now):  # type: ignore[no-untyped-def]
    set_settings(db, {"quiet": {"hours": None}, "planner": {"day": {"start": "08:00", "end": end}}})
    task = add_task(db, "Ritira il pacco al PuntoPoste", rome(2026, 10, 22).date())
    with_place(db, "tasks", task, place(db, "PuntoPoste", OFFICE))
    at_location(db, HERE_MILAN, now - timedelta(minutes=1))
    return task, run(db, secrets_key, now, lambda p, env: p.plan(), keys=())


def test_a_plan_made_after_midnight_places_no_errand_in_the_night(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The day ends at 02:00, a plan made at 00:05 is still yesterday's. The errand isn't put
    at 00:24 (nor is "leave now" sent for it): it waits for the next day's plan."""
    task, plan = errand_plan(db, secrets_key, "02:00", rome(2026, 10, 22, 0, 5))
    assert plan["date"] == "2026-10-21"
    assert not any(i.get("task_id") == task for i in plan["items"])
    (unplanned,) = plan["unplanned"]
    assert unplanned["reason"] == "Niente commissioni dopo mezzanotte: va nel piano del giorno dopo."


def test_with_the_usual_day_a_plan_at_00_05_places_it_from_the_days_start(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    task, plan = errand_plan(db, secrets_key, "21:00", rome(2026, 10, 22, 0, 5))
    errand = next(i for i in plan["items"] if i.get("task_id") == task)
    assert errand["start"] >= "2026-10-22T08:00:00+02:00" and errand["end"] <= "2026-10-22T21:00:00+02:00"


def test_before_midnight_an_errand_ends_by_midnight(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    task, plan = errand_plan(db, secrets_key, "02:00", rome(2026, 10, 21, 23, 0))
    errand = next(i for i in plan["items"] if i.get("task_id") == task)
    assert errand["end"] <= "2026-10-22T00:00:00+02:00"


def test_too_little_left_before_midnight_waits(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """At 23:40 with the day ending at 02:00, 20 minutes are left before midnight: not enough for 30."""
    task, plan = errand_plan(db, secrets_key, "02:00", rome(2026, 10, 21, 23, 40))
    assert not any(i.get("task_id") == task for i in plan["items"])
    assert (
        plan["unplanned"][0]["reason"] == "Niente commissioni dopo mezzanotte: va nel piano del giorno dopo."
    )


def test_an_ordinary_day_ending_soon_is_not_about_midnight(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """M15: at 20:45 on an 08:00-21:00 day, the errand doesn't fit before the day's end: not "midnight"."""
    task, plan = errand_plan(db, secrets_key, "21:00", rome(2026, 10, 22, 20, 45))
    assert not any(i.get("task_id") == task for i in plan["items"])
    assert "midnight" not in plan["unplanned"][0]["reason"]


# --- email tasks get a place only where the user must go -------------------------------------------------

AMSTERDAM = (52.420, 4.868)


def test_a_website_or_the_senders_company_is_no_place() -> None:
    from pisa.domain.places import physical_place

    assert physical_place("GitHub", "notifications@github.example") is None
    assert physical_place("https://github.com/example/pisa/actions", None) is None
    assert physical_place("www.ovh.it", "noreply@ovh.example") is None
    assert (
        physical_place("Poste Italiane, Via Immaginaria 1", "noreply@poste.example")
        == "Poste Italiane, Via Immaginaria 1"
    )
    assert physical_place("  ", None) is None


class Amsterdam:
    """The user is in Amsterdam; Apple knows a tobacconist there and the Rome parcel point's street."""

    @staticmethod
    def fake():  # type: ignore[no-untyped-def]
        import httpx

        from tests.places_fake import FakePlaces

        class Fake(FakePlaces):
            def _apple_reverse(self, request: httpx.Request) -> httpx.Response:
                return self._apple_auth(request) or httpx.Response(200, json={"results": [{
                    "name": "Damrak 1", "coordinate": {"latitude": AMSTERDAM[0], "longitude": AMSTERDAM[1]},
                    "formattedAddressLines": ["Damrak 1", "1012 LG Amsterdam", "Nederland"]}]})  # fmt: skip

            def _apple_search(self, request: httpx.Request) -> httpx.Response:  # every shop nearby
                return self._apple_auth(request) or httpx.Response(200, json=self.apple_search)

            def _apple_geocode(self, request: httpx.Request) -> httpx.Response:
                q = request.url.params["q"]
                rome = {
                    "name": "Via Finta 10",
                    "coordinate": {"latitude": 41.943, "longitude": 12.531},
                    "formattedAddressLines": ["Via Finta 10", "00176 Roma RM", "Italia"],
                    "structuredAddress": {"locality": "Roma", "thoroughfare": "Via Finta"},
                }
                return self._apple_auth(request) or httpx.Response(
                    200, json={"results": [rome] if "Finta" in q or "PuntoPoste" in q else []}
                )

        fake = Fake()

        def shop(pid: str, name: str, at: tuple[float, float], street: str, city: str) -> dict[str, object]:
            return {"id": pid, "name": name, "coordinate": {"latitude": at[0], "longitude": at[1]},
                    "formattedAddressLines": [street, city, "Nederland"], "poiCategory": "Store"}  # fmt: skip

        fake.apple_search = {
            "results": [
                shop("I1", "Tabak 104", (52.413, 4.869), "Verzonnenstraat 1", "1012 NX Amsterdam"),
                shop("I2", "Albert Heijn", (52.405, 4.889), "Fantasieplein 5", "1071 DJ Amsterdam"),
            ]
        }
        return fake


def resolve_tasks(db, secrets_key, *texts: str):  # type: ignore[no-untyped-def]
    from tests.places_env import run_places

    ids = [add_task(db, f"Task {i}", rome(2026, 10, 22).date(), place=t) for i, t in enumerate(texts)]
    at_location(db, AMSTERDAM, rome(2026, 10, 22, 9, 59))
    run_places(
        db, secrets_key, Amsterdam.fake(), rome(2026, 10, 22, 10), lambda env: env.places.resolve_pending()
    )
    return [
        db.execute("SELECT place_id IS NOT NULL, place_elsewhere FROM tasks WHERE id = %s", (i,)).fetchone()
        for i in ids
    ]


def test_a_parcel_point_in_rome_stays_unresolved_while_in_amsterdam(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """A parcel point's text, "PuntoPoste TABACCHERIA n. 104", was matched to a shop in Amsterdam. With its
    city it is "Elsewhere: Roma"; without, the Amsterdam tobacconist doesn't match the text, so no place."""
    named, bare, here = resolve_tasks(
        db,
        secrets_key,
        "PuntoPoste TABACCHERIA n. 104, Via Finta 10, Roma",
        "Tabaccheria PuntoPoste 104",
        "Albert Heijn Fantasieplein",
    )
    assert named == (False, "Roma")
    assert bare == (False, None)
    assert here == (True, None)  # a place in the city the user is in still resolves


def test_elsewhere_is_the_unplanned_reason(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    resolve_tasks(db, secrets_key, "PuntoPoste TABACCHERIA n. 104, Via Finta 10, Roma")
    plan = run(db, secrets_key, rome(2026, 10, 22, 10), lambda p, env: p.plan(), keys=())
    assert [u["reason"] for u in plan["unplanned"]] == ["Altrove: Roma"]


# --- re-triage of the first sync's tasks ------------------------------------------------------------------


def email_task(db, title: str, mid: str, thread: str, subject: str, created) -> str:  # type: ignore[no-untyped-def]
    import uuid

    from pisa.domain.tasks import title_key

    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
        "snippet, body) VALUES (%s, %s, 'Sender', 'x@example.com', %s, %s, '{INBOX}', 'x', 'x') "
        "ON CONFLICT DO NOTHING",
        (mid, thread, subject, created),
    )
    task_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO tasks (id, title, status, due_kind, priority, source_kind, source_ref, title_key, "
        "created_at, updated_at) VALUES (%s, %s, 'open', 'none', 2, 'email', %s, %s, %s, %s)",
        (task_id, title, mid, title_key(title), created, created),
    )
    return task_id


# --- the queued items -----------------------------------------------------------------------------------


def test_a_skip_makes_earlier_options_stale(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """A check-in "skip" and an option apply for the same lunch: the skip takes the plan lock too, so the
    options made before it no longer apply (409), and two at once land one after the other."""
    import asyncio

    from pisa.domain.errors import PisaError
    from tests.test_location_precision import with_options
    from tests.test_plan_options import make_trip_day

    day = make_trip_day(db)

    async def skip_then_apply(service, clock):  # type: ignore[no-untyped-def]
        got = await service.options({"event_id": day["lunch"]})
        await service.skip({"event_id": day["lunch"]}, "check_in")
        try:
            await service.apply(got["options_id"], got["options"][1]["id"])
        except PisaError as err:
            return err.code
        return "applied"

    assert with_options(db, secrets_key, skip_then_apply) == "plan_changed"
    db.execute("DELETE FROM plan_adjustments")

    async def together(service, clock):  # type: ignore[no-untyped-def]
        got = await service.options({"event_id": day["lunch"]})
        return await asyncio.gather(
            service.apply(got["options_id"], got["options"][1]["id"]),
            service.skip({"event_id": day["lunch"]}, "check_in"),
            return_exceptions=True,
        )

    applied, skipped = with_options(db, secrets_key, together)
    assert not isinstance(skipped, BaseException)
    rows = [r[0] for r in db.execute("SELECT source FROM plan_adjustments ORDER BY seq").fetchall()]
    if isinstance(applied, PisaError):  # the skip came first
        assert applied.code == "plan_changed" and rows == ["check_in"]
    else:  # the apply came first, whole; the skip after it, the newest choice
        assert rows[-1] == "check_in" and set(rows[:-1]) == {"app"}


def test_the_options_budget_holds_after_an_infeasible_skip(db, secrets_key, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An infeasible skip used to leave the next variant "first" (solved past the deadline), and the M17
    alternatives went on after it: ~3.5 s. Now only the first variant is solved past the deadline."""
    from pisa.services import plan_options
    from pisa.services.planner import PlannerService
    from tests.test_location_precision import sambalaya, with_options
    from tests.test_plan_options import make_trip_day

    day = make_trip_day(db)
    real = PlannerService.solve_day
    calls: list[int] = []

    async def solve_day(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        built = await real(self, *args, **kwargs)
        calls.append(1)
        if len(calls) == 2:  # the skip variant
            built.solution.status = "infeasible"
        return built

    monkeypatch.setattr(PlannerService, "solve_day", solve_day)
    monkeypatch.setattr(plan_options, "OPTIONS_SOLVE_S", 0.0)
    fake = sambalaya()

    async def go(service, clock):  # type: ignore[no-untyped-def]
        return await service.options({"event_id": day["lunch"]}, "fit")

    got = with_options(db, secrets_key, go, fake=fake, keys=("apple",))
    assert len(calls) == 2 and got["options"] == []  # the day as it is, and the skip: nothing more
    assert fake.routes("apple.search") == []  # no similar-place search once the time is up


# --- M14 ---------------------------------------------------------------------------------

ROME = (41.927, 12.512)


class Travels:
    """Apple answers by place and language: Amsterdam is Amsterdam in both; Rome is "Roma" in Italian and
    "Rome" in English, in the reverse geocode and in the parcel point's address."""

    @staticmethod
    def fake():  # type: ignore[no-untyped-def]
        import httpx

        fake = Amsterdam.fake()

        def city(lang: str) -> str:
            return "Roma" if lang.startswith("it") else "Rome"

        def reverse(request: httpx.Request) -> httpx.Response:
            lat = float(request.url.params["loc"].split(",")[0])
            name = "Amsterdam" if lat > 50 else city(request.url.params["lang"])
            line = "1012 LG Amsterdam" if lat > 50 else f"00184 {name} RM"
            return fake._apple_auth(request) or httpx.Response(200, json={"results": [{
                "name": "x", "coordinate": {"latitude": lat, "longitude": 5.0},
                "formattedAddressLines": ["Via 1", line, "Italia"]}]})  # fmt: skip

        def geocode(request: httpx.Request) -> httpx.Response:
            q, name = request.url.params["q"], city(request.url.params["lang"])
            point = {"name": "Via Finta 10", "coordinate": {"latitude": 41.943, "longitude": 12.531},
                     "formattedAddressLines": ["Via Finta 10", f"00182 {name} RM", "Italia"],
                     "structuredAddress": {"locality": name, "thoroughfare": "Via Finta"}}  # fmt: skip
            return fake._apple_auth(request) or httpx.Response(
                200, json={"results": [point] if "Finta" in q else []}
            )

        fake._apple_reverse = reverse  # type: ignore[method-assign]
        fake._apple_geocode = geocode  # type: ignore[method-assign]
        return fake


def resolve_now(db, secrets_key, fake, where, at) -> None:  # type: ignore[no-untyped-def]
    from tests.places_env import run_places

    at_location(db, where, at - timedelta(minutes=1))
    run_places(db, secrets_key, fake, at, lambda env: env.places.resolve_pending())


def test_elsewhere_is_checked_again_back_in_that_city(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """M14: "…, Roma" is "Elsewhere: Roma" in Amsterdam; back in Rome it is checked again (the memo holds
    the city it was checked in), whatever the user's language: Rome is Roma."""
    set_settings(db, {"language": "en-US"})
    task = add_task(db, "Pacco", rome(2026, 10, 22).date(), place="PuntoPoste 104, Via Finta 10, Roma")
    fake = Travels.fake()
    resolve_now(db, secrets_key, fake, AMSTERDAM, rome(2026, 10, 22, 10))
    status = "SELECT place_elsewhere, place_checked_in FROM tasks WHERE id = %s"
    assert db.execute(status, (task,)).fetchone() == ("Roma", "amsterdam")
    resolve_now(db, secrets_key, fake, ROME, rome(2026, 10, 24, 10))
    assert db.execute(status, (task,)).fetchone() == (None, "roma|rome")
