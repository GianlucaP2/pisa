"""Pisa's skills: wish to skill and the skill garden (`pisa_skills`, `pisa_skill_uses`).

A wish (their own words this turn, never outside text) becomes a skill: 2-5 checks from their sentence, a view
recipe over the read-only allowlist (`domain.views.validate`), a trial on their data now (reads
only: no write, no push), its sheet shown with "Lo accendo?". It is turned on only with the phone's signature
after Face ID (the key store), and only when the trial passed every check.

The garden: uses are counted; an `on` skill unused for 30 days withers (no schedule; a call brings it back);
schedules they gave run it as a notify (an initiative step: a stop pauses it, quiet hours hold it).
Logs carry counts and statuses, never their sentence or a sheet.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from rapidfuzz import fuzz

from pisa.domain import views as view_rules
from pisa.domain import wishes as rules
from pisa.domain.decisions import Decision
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services import decisions as decision_log
from pisa.services.notifications import enqueue
from pisa.services.phone_keys import PhoneKeys

_log = get_logger("pisa.wishes")
FIND_MIN = 70  # a name said is found by whole words or a close spelling (the rule)
SCHEDULED_PER_HOUR = 4
UNDONE_WITHIN = timedelta(minutes=10)  # turned off this soon after a use: that use was undone


class Sheets(Protocol):
    """The recipe runner (`ViewService.sheet_of`)."""

    async def sheet_of(
        self, title: str, stored: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]: ...

    async def trial(
        self, title: str, stored: dict[str, Any], again: str
    ) -> tuple[dict[str, Any] | None, list[str], str | None]: ...


def _not_found() -> PisaError:
    return PisaError("not_found", "There is no such skill.", "Refresh Pisa's skills")


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Check the skill")


def _iso(moment: datetime | None, tz: ZoneInfo) -> str | None:
    return moment.astimezone(tz).isoformat() if moment else None


class WishService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        sheets: Sheets,
        keys: PhoneKeys,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
    ) -> None:
        self._pool = pool
        self._sheets = sheets
        self._keys = keys
        self._owner = owner
        self._clock = clock

    # --- wish to skill ------------------------------------------------------------------------------------

    async def wish(
        self,
        name: str,
        words: str,
        checks: Any,
        recipe: Any,
        *,
        hosts: set[str],
        outside: bool,
        turn_id: str | None = None,
        device_id: uuid.UUID | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """(the sentence for the model, the trial's sheet to show). RAIL: only on their own wish this turn."""
        if outside or not rules.asks_wish(words):
            return (
                "Not made: a skill comes only from the user's own wish this turn "
                '("vorrei che tu potessi…"), never from outside text.'
            ), None
        name = " ".join(name.split())[: rules.NAME_MAX]
        key = view_rules.name_key(name)
        if len(key) < view_rules.NAME_MIN:
            return f"Not made: a skill needs a name of {view_rules.NAME_MIN} letters or more.", None
        checked = rules.checks_of(checks)
        if isinstance(checked, str):
            return f"Not made: {checked}.", None
        valid = view_rules.validate(recipe, hosts, words)
        if isinstance(valid, str):
            return f"Not made: {valid}.", None
        # the trial's placeholders checked as save_view's are, with the fields
        stored = valid.as_json()
        # the trial: reads only, now, once
        sheet, _, wrong = await self._sheets.trial(name, stored, "Call wish_skill again")
        if wrong is not None:
            return wrong.replace("Not saved:", "Not made:", 1), None
        passed = [rules.passes(c, sheet) for c in checked]
        kind, status = "view", "trial"
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa_skills'))")
            old = await (
                await conn.execute("SELECT id, status FROM pisa_skills WHERE name_key = %s", (key,))
            ).fetchone()
            if old is not None and old[1] not in ("proposed", "trial"):
                return f'Not made: Pisa already has a skill called "{name}". Ask for another name.', None
            count = (await (await conn.execute("SELECT count(*) FROM pisa_skills")).fetchone() or (0,))[0]
            if old is None and count >= rules.SKILLS_MAX:
                return f"Not made: Pisa keeps {rules.SKILLS_MAX} of these at most. Ask which to delete.", None
            trial = {"at": now.isoformat(), "sheet": sheet, "passed": passed}
            await conn.execute(
                "INSERT INTO pisa_skills (id, name, name_key, kind, status, made_from, checks, recipe, job, "
                "tests_passed, trial, turn_id, device_id, created_at, updated_at, digest) VALUES (%s, %s, "
                "%s, %s, "
                "%s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (name_key) DO UPDATE SET name = "
                "EXCLUDED.name, "
                "kind = EXCLUDED.kind, status = EXCLUDED.status, made_from = EXCLUDED.made_from, "
                "checks = EXCLUDED.checks, recipe = EXCLUDED.recipe, job = EXCLUDED.job, "
                "tests_passed = EXCLUDED.tests_passed, trial = EXCLUDED.trial, digest = EXCLUDED.digest, "
                "updated_at = EXCLUDED.updated_at",
                (
                    uuid.uuid4(),
                    name,
                    key,
                    kind,
                    status,
                    " ".join(words.split())[: rules.MADE_FROM_MAX],
                    Jsonb(checked),
                    Jsonb(stored),
                    None,
                    sum(passed),
                    Jsonb(trial),
                    turn_id,
                    device_id,
                    now,
                    now,
                    rules.digest(kind, stored, checked),
                ),
            )
        _log.info("skill_wished", kind=kind, checks=len(checked), passed=sum(passed))
        if sheet is None:
            return (
                f'Tried: "{name}" has nothing to show today ({len(checked)} checks, none passed). Say so.',
                None,
            )
        if sum(passed) < len(checked):
            return (
                f'Tried: "{name}" passed {sum(passed)} of {len(checked)} checks; it can\'t be turned on like '
                "this. Show it, say what is missing in one line."
            ), sheet
        return (
            f'Tried on their data: "{name}" passed all {len(checked)} checks. Show it and ask "Lo accendo?": '
            "their yes, then Face ID on the phone, turns it on."
        ), sheet

    # --- the app ------------------------------------------------------------------------------------------

    async def _wire(
        self, conn: AsyncConnection, row: dict[str, Any], *, full: bool = False
    ) -> dict[str, Any]:
        tz = self._owner.zone
        uses = await (
            await conn.execute(
                "SELECT count(*) FROM pisa_skill_uses WHERE skill_id = %s AND used_at >= %s AND how <> "
                "'schedule'",
                (row["id"], self._clock() - rules.WITHER_AFTER),
            )
        ).fetchone()
        out: dict[str, Any] = {
            "id": str(row["id"]),
            "name": row["name"],
            "kind": row["kind"],
            "status": row["status"],
            "made_from": row["made_from"],
            "tests_passed": row["tests_passed"],
            "tests_total": len(row["checks"]),
            "last_used_at": _iso(row["last_used_at"], tz),
            "uses_30d": uses[0] if uses else 0,
            "created_at": _iso(row["created_at"], tz),
            "enabled_at": _iso(row["enabled_at"], tz),
        }
        if row["schedule"]:
            out["schedule"] = row["schedule"]
        if full:
            passed = (row["trial"] or {}).get("passed") or []
            out["checks"] = [
                c | {"passed": bool(passed[i]) if i < len(passed) else False}
                for i, c in enumerate(row["checks"])
            ]
            trial = row["trial"]
            if trial and trial.get("sheet"):
                at = datetime.fromisoformat(trial["at"])
                out["trial"] = {"at": _iso(at, tz), "sheet": trial["sheet"]}
        return out

    async def listed(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            rows = await (await cur.execute("SELECT * FROM pisa_skills ORDER BY created_at DESC")).fetchall()
            return [await self._wire(conn, r) for r in rows]

    async def _row(self, conn: AsyncConnection, skill_id: str) -> dict[str, Any]:
        try:
            key = uuid.UUID(skill_id)
        except ValueError:
            raise _not_found() from None
        cur = conn.cursor(row_factory=dict_row)
        row = await (await cur.execute("SELECT * FROM pisa_skills WHERE id = %s", (key,))).fetchone()
        if row is None:
            raise _not_found()
        return row

    async def get(self, skill_id: str) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            return await self._wire(conn, await self._row(conn, skill_id), full=True)

    def subject(self, row: dict[str, Any]) -> dict[str, str]:
        """What the signature turns on: this id and name, this exact recipe and checks (their
        digest), and what it reads, as the phone shows it."""
        reads = (row["recipe"] or {}).get("reads") if row["kind"] == "view" else None
        return {
            "skill_id": str(row["id"]),
            "name": row["name"],
            "digest": row["digest"],
            "reads": rules.reads_text(reads),
        }

    async def challenge(self, skill_id: str) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            row = await self._row(conn, skill_id)
        return await self._keys.challenge("skill", self.subject(row))

    async def enable(
        self, skill_id: str, nonce: str, signature: str, key_id: str, *, device_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        """RAIL: on only with a signature over this skill (a Face ID key's, or the asking device's own key,
        `device_id`), and only a view whose trial passed every check."""
        async with self._pool.connection() as conn:
            row = await self._row(conn, skill_id)
            lang = await self._owner.lang(conn)
        if row["kind"] != "view" or row["status"] not in ("trial", "off", "withered"):
            raise _invalid("Only a view skill that was tried (or turned off) can be turned on.")
        if row["tests_passed"] < len(row["checks"]):
            raise _invalid("Its trial didn't pass every check: Pisa can't turn it on like this.")
        await self._keys.verify("skill", self.subject(row), nonce, signature, key_id, device_id)
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            # only the skill that was signed: the same digest, still tried or off, still
            # passing
            done = await conn.execute(
                "UPDATE pisa_skills SET status = 'on', enabled_at = %s, updated_at = %s WHERE id = %s "
                "AND digest = %s AND kind = 'view' AND status IN ('trial', 'off', 'withered') "
                "AND tests_passed >= jsonb_array_length(checks)",
                (now, now, row["id"], row["digest"]),
            )
            if not done.rowcount:
                raise _invalid("The skill changed since it was signed: ask for Face ID again.")
            what = f"Ho imparato: {row['name']}." if lang == "it-IT" else f"I learned: {row['name']}."
            await decision_log.store(
                conn,
                [
                    Decision(
                        "act",
                        "skill_on",
                        "owner_words",
                        ref=str(row["id"]),
                        purpose=what[:120],
                        outcome="done",
                    )
                ],
                at=now,
                trace_id=None,
                device_id=None,
            )
        _log.info("skill_on")
        return await self.get(skill_id)

    async def disable(self, skill_id: str) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            row = await self._row(conn, skill_id)
            if row["status"] in ("on", "withered"):
                now = self._clock()
                await conn.execute(
                    "UPDATE pisa_skills SET status = 'off', updated_at = %s WHERE id = %s", (now, row["id"])
                )
                # off within minutes of a use of theirs: that use was undone (the ledger's,
                # shadow)
                await conn.execute(
                    "UPDATE pisa_skill_uses SET outcome = 'undone' WHERE skill_id = %s AND how <> 'schedule' "
                    "AND outcome IN ('kept', 'corrected') AND used_at >= %s",
                    (row["id"], now - UNDONE_WITHIN),
                )
        return await self.get(skill_id)

    async def delete(self, skill_id: str) -> None:
        async with self._pool.connection() as conn:
            row = await self._row(conn, skill_id)
            await conn.execute("DELETE FROM pisa_skills WHERE id = %s", (row["id"],))

    # --- by voice -----------------------------------------------------------------------------------------

    async def _find(self, name: str) -> dict[str, Any] | None:
        wanted = view_rules.name_key(name)
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            rows = list(await (await cur.execute("SELECT * FROM pisa_skills")).fetchall())

        def score(key: str) -> float:
            if len(key) >= view_rules.NAME_MIN and f" {key} " in f" {wanted} ":
                return 100.0 + 10 * len(key.split())
            return fuzz.ratio(wanted, key)

        ranked = sorted(((score(r["name_key"]), r) for r in rows), key=lambda p: -p[0])
        return ranked[0][1] if ranked and ranked[0][0] >= FIND_MIN else None

    async def names(self) -> list[str]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT name FROM pisa_skills WHERE status IN ('on', 'withered') ORDER BY name"
                )
            ).fetchall()
        return [str(r[0]) for r in rows]

    async def run(self, name: str, how: str = "voice") -> tuple[str, dict[str, Any] | None, list[str]]:
        """A skill by name, now: (its name or why not, its sheet, what was left out). A withered one comes
        back."""
        skill = await self._find(name)
        if skill is None:
            return f"No skill called {name[:40]!r}.", None, []
        if skill["status"] not in ("on", "withered"):
            return f'"{skill["name"]}" isn\'t on ({skill["status"]}): only the user turns it on.', None, []
        sheet, dropped = await self._sheets.sheet_of(skill["name"], skill["recipe"])
        now = self._clock()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pisa_skill_uses (skill_id, used_at, how, outcome) VALUES (%s, %s, %s, %s)",
                (skill["id"], now, how, "failed" if sheet is None else "kept"),  # the ledger
            )
            if how != "schedule":  # only their own use keeps a skill alive (or brings it back)
                await conn.execute(
                    "UPDATE pisa_skills SET last_used_at = %s, status = CASE WHEN status = 'withered' THEN "
                    "'on' ELSE status END, updated_at = %s WHERE id = %s",
                    (now, now, skill["id"]),
                )
        return skill["name"], sheet, dropped

    async def kind_of(self, name: str) -> str:
        """The data block kind of what this skill reads (email, web, map: outside text)."""
        skill = await self._find(name)
        return rules.read_kind((skill["recipe"] or {}).get("reads") if skill else None)

    async def rename(self, old: str, new: str, words: str, *, outside: bool) -> str:
        if outside or not rules.asks_rename(words):
            return "Not renamed: only the user's own words this turn rename a skill."
        skill = await self._find(old)
        if skill is None:
            return f"No skill called {old[:40]!r}."
        name = " ".join(new.split())[: rules.NAME_MAX]
        key = view_rules.name_key(name)
        if len(key) < view_rules.NAME_MIN:
            return f"Not renamed: a name of {view_rules.NAME_MIN} letters or more."
        async with self._pool.connection() as conn:
            taken = await (
                await conn.execute(
                    "SELECT 1 FROM pisa_skills WHERE name_key = %s AND id <> %s", (key, skill["id"])
                )
            ).fetchone()
            if taken is not None:
                return f'Not renamed: Pisa already has a skill called "{name}".'
            await conn.execute(
                "UPDATE pisa_skills SET name = %s, name_key = %s, updated_at = %s WHERE id = %s",
                (name, key, self._clock(), skill["id"]),
            )
        return f'Renamed: "{skill["name"]}" is now "{name}". Say it in a few words.'

    async def set_schedule(self, name: str, at: str, days: str, words: str, *, outside: bool) -> str:
        if outside or not rules.asks_schedule(words):
            return "Not scheduled: only the user's own words this turn give a skill its time."
        skill = await self._find(name)
        if skill is None or skill["status"] not in ("on", "withered"):
            return "Not scheduled: no skill of that name is on."
        if rules.reads_outside((skill["recipe"] or {}).get("reads")):
            # never Pisa's words on the lock screen out of an email or a page, every day
            return (
                "Not scheduled: it reads email or the web; Pisa runs it when they ask, "
                "never on a timer. Say so."
            )
        schedule = rules.schedule_of(at, days)
        if isinstance(schedule, str):
            return f"Not scheduled: {schedule}."
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pisa_skills SET schedule = %s, status = 'on', updated_at = %s WHERE id = %s",
                (Jsonb(schedule), self._clock(), skill["id"]),
            )
        return f'Scheduled: "{skill["name"]}" at {schedule["at"]}, {schedule["days"]}.'

    # --- the garden ---------------------------------------------------------------------------------------

    async def tick(self) -> int:
        """Every minute (an initiative step): skills unused for 30 days wither; the due schedules run as a
        notify (quiet hours hold it)."""
        now, local = self._clock(), self._owner.local(self._clock())
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            rows = list(await (await cur.execute("SELECT * FROM pisa_skills WHERE status = 'on'")).fetchall())
            lang = await self._owner.lang(conn)
            for row in rows:
                if rules.withers(row["status"], row["last_used_at"], row["enabled_at"], now):
                    await conn.execute(
                        "UPDATE pisa_skills SET status = 'withered', updated_at = %s WHERE id = %s",
                        (now, row["id"]),
                    )
                    row["status"] = "withered"
        async with self._pool.connection() as conn:  # a few an hour, the rest the next minutes
            recent = await (
                await conn.execute(
                    "SELECT count(*) FROM pisa_skill_uses WHERE how = 'schedule' AND used_at > %s",
                    (now - timedelta(hours=1),),
                )
            ).fetchone()
        room = SCHEDULED_PER_HOUR - (recent[0] if recent else 0)
        ran = 0
        for row in rows:
            if row["status"] != "on" or not rules.due(row["schedule"], local, row["last_run_on"]):
                continue
            if rules.reads_outside((row["recipe"] or {}).get("reads")):
                continue  # never on a timer
            if ran >= room:
                break
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE pisa_skills SET last_run_on = %s WHERE id = %s", (local.date(), row["id"])
                )
            _, sheet, _ = await self.run(row["name"], "schedule")
            if sheet is None:
                continue
            async with self._pool.connection() as conn:
                text = rules.scheduled_text(row["name"], sheet, (row["recipe"] or {}).get("reads"))
                await enqueue(
                    conn,
                    kind="info",
                    text=text,
                    lang=lang,
                    now=now,
                    timezone=self._owner.tz,
                    path="wishes.scheduled",
                )
            ran += 1
        return ran

    async def week(self, start: datetime, end: datetime, lang: str) -> dict[str, list[dict[str, str]]]:
        """The Sunday card's part: what Pisa learned this week, and the withered ones ("la tengo?")."""
        async with self._pool.connection() as conn:
            learned = await (
                await conn.execute(
                    "SELECT id::text, name FROM pisa_skills WHERE enabled_at >= %s AND enabled_at < %s "
                    "AND status = 'on' ORDER BY enabled_at",
                    (start, end),
                )
            ).fetchall()
            withered = await (
                await conn.execute(
                    "SELECT id::text, name FROM pisa_skills WHERE status = 'withered' ORDER BY name"
                )
            ).fetchall()
        keep = "la tengo?" if lang == "it-IT" else "shall I keep it?"
        return {
            "learned": [{"id": i, "name": n} for i, n in learned],
            "withered": [{"id": i, "name": n, "text": f"{n}: {keep}"} for i, n in withered],
        }
