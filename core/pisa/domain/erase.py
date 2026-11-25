"""They delete, Pisa never does. Their deletion by family and age (Settings → "Cancella", the phone's
signature). Pure rules, no I/O.

Each family names its tables and the clause that makes a row "older than the date": `%s` is the cutoff,
a moment (`AT`: the date's first instant in their zone) or the date itself (`DAY`, for a `date` column). Every
clause reads the table as `m`. A row's children go with it (ON DELETE CASCADE); a row of their that points at
one (ON DELETE SET NULL) keeps its own text. Conversation messages follow the safe rule: one a live
row cites stays (`retention.GOING`, added by the service).

erasing never resets a safety counter or a dedupe guard. A `guard` part (the push caps'
rows, the reminders' and attend's dedupe keys, the walk recap's and the morning thought's once-a-day reads)
never loses a row newer than its own window (each named after the read it protects);
`llm_calls` keeps the current month (the spend caps); a stuck-task morning thought stays while its task is
open.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, LiteralString, get_args
from zoneinfo import ZoneInfo

from pisa.domain import errands

Family = Literal["messages", "places", "mail", "money", "records", "memory", "health", "voice", "learn"]
FAMILY_NAMES: tuple[Family, ...] = get_args(Family)
Kind = Literal["at", "day"]
AT: Kind = "at"
DAY: Kind = "day"


# each guard is named after the read it protects, at least as long as that read's window
PUSH_CAPS = timedelta(days=2)  # notifications: the daily push caps count today's (their zone)
REMINDER_KEYS = timedelta(days=2)  # reminders_sent: one reminder per key (today's and tomorrow's early ones)
PRESENCE_TODAY = timedelta(days=2)  # done_by_presence: today's, never closed twice
ATTEND_CAP = timedelta(days=2)  # attend_candidates: the keys and the 2-a-day cap
IDEA_WEEK = timedelta(days=7)  # ritual_asks: `kind='idea'` asks are counted per week (rituals' per_week)
CUES_TODAY = timedelta(days=2)  # pisa_cue_asks: today's cues
WALK_RECAP_DAY = timedelta(days=2)  # walk_recaps: once a day
MORNING_THOUGHT_DAY = timedelta(days=2)  # morning_thoughts: once a day
ERRANDS_WEEK = timedelta(days=errands.REPEAT_DAYS)  # errands: a combination isn't suggested twice in this


VOICE_SINCE: LiteralString = "coalesce((SELECT min(c.created_at) FROM voice_consents c), m.created_at) < %s"


@dataclass(frozen=True)
class Part:
    table: LiteralString
    older: LiteralString  # the clause, `m` the table, `%s` the cutoff
    kind: Kind = AT
    guard: timedelta | None = None  # a cap's or a dedupe's row: the cutoff is never newer than now - guard
    month: bool = False  # the spend caps: the current month (their zone) is never erased


FAMILIES: dict[Family, tuple[Part, ...]] = {
    # their conversations: the messages (the safe rule is the service's) and the talk sessions (turns cascade)
    "messages": (
        Part("messages", "m.created_at < %s"),
        Part("talk_sessions", "m.started_at < %s"),
        Part("outside_values", "m.noted_at < %s"),  # the values their talks read from outside
        Part("turns", "m.created_at < %s"),  # REST turns (Pisa's words); their sheets cascade
        Part("research_findings", "m.created_at < %s"),  # what their talks' research found
        Part("drawings", "m.created_at < %s"),  # the drawings Pisa showed in their talks
    ),
    # places and GPS: the phone's points and visits, the stays, their notes on places, the residency and trips
    "places": (
        Part("device_locations", "m.measured_at < %s"),
        Part("device_visits", "m.arrived_at < %s"),
        Part("stays", "m.arrived_at < %s"),
        Part("place_notes", "m.created_at < %s"),
        Part("residency_days", "m.day < %s", DAY),
        # a trip goes once it ended (back home, or STAY_MAX after its arrival): never one in progress
        Part("trips", "coalesce(m.back_at, m.arrive_at + interval '90 days') < %s"),
    ),
    # their mail: Pisa's copies (attachments and reads cascade), who they write to, the rules' log, Pisa's
    # drafts and sent mail; an email-sourced gift idea goes with its email (the note, by the service)
    "mail": (
        Part("email_messages", "m.sent_at < %s"),
        Part("email_correspondents", "m.last_at < %s"),
        Part("email_rule_log", "m.made_at < %s"),
        Part("email_drafts", "m.created_at < %s"),
        Part("pisa_sent_mail", "m.sent_at < %s"),
        Part("outside_values", "m.noted_at < %s"),  # what Pisa read from their mail
        Part("mailed_invites", "m.seen_at < %s"),  # the UIDs of the invitations in it
    ),
    # their money and what Pisa recognised in their mail
    "money": (
        Part("flight_claims", "m.created_at < %s"),
        Part("refunds_promised", "m.created_at < %s"),
        Part("bill_checks", "m.created_at < %s"),
        Part("owner_promises", "m.created_at < %s"),
        Part("supply_tasks", "m.created_at < %s"),
        Part("purchases", "m.created_at < %s"),
        Part("money_seen", "m.seen_at < %s"),
        Part("things", "m.created_at < %s"),
        Part("bank_transactions", "m.booked_on < %s", DAY),  # their bank, read-only
        Part(
            "bank_accounts",
            "m.seen_at < %s AND NOT EXISTS (SELECT 1 FROM bank_transactions t WHERE "
            "t.connector_id = m.connector_id AND t.account_uid = m.uid)",
        ),
    ),
    # Pisa's records about them: what it said, did, proposed and measured
    "records": (
        Part("briefings", "m.created_at < %s"),
        Part("notifications", "m.created_at < %s", guard=PUSH_CAPS),  # the push caps count them
        Part("reminders_sent", "m.created_at < %s", guard=REMINDER_KEYS),  # one reminder per key
        Part("plans", "m.generated_at < %s"),
        Part("plan_adjustments", "m.created_at < %s"),
        Part("check_ins", "m.created_at < %s"),
        Part("handoff_preps", "m.created_at < %s"),
        Part("understand_corrections", "m.created_at < %s"),
        Part("done_by_presence", "m.done_at < %s", guard=PRESENCE_TODAY),  # today's: never closed twice
        Part("approvals", "m.created_at < %s"),
        Part("pisa_decisions", "m.at < %s"),
        Part("personal_evals", "m.made_at < %s"),
        Part("attend_candidates", "m.created_at < %s", guard=ATTEND_CAP),  # keys; the 2-a-day cap
        Part("guardian_items", "m.decided_at < %s"),  # every hold and release
        Part("pisa_promises", "m.created_at < %s"),
        Part("ritual_asks", "m.created_at < %s", guard=IDEA_WEEK),
        Part("ritual_sheets", "m.created_at < %s"),
        Part("pisa_cue_asks", "m.asked_at < %s", guard=CUES_TODAY),  # today's cues
        Part("walk_recaps", "m.created_at < %s", guard=WALK_RECAP_DAY),  # once a day
        Part(  # once a day; a stuck task is asked about once: its row stays while the task is open
            "morning_thoughts",
            "m.created_at < %s AND NOT (m.kind = 'stuck_task' AND EXISTS "
            "(SELECT 1 FROM tasks t WHERE t.id = m.ref AND t.status = 'open'))",
            guard=MORNING_THOUGHT_DAY,
        ),
        Part("errand_suggestions", "m.created_at < %s", guard=ERRANDS_WEEK),  # not twice a week
        Part("feedback_events", "m.created_at < %s"),
        Part("recommendation_outcomes", "m.created_at < %s"),
        Part("task_bulk", "m.created_at < %s"),
        Part(  # one in flight is the sweep's; this month's are the spend caps
            "llm_calls", "m.created_at < %s AND m.status <> 'reserved'", month=True
        ),
        Part("device_diagnostics", "m.received_at < %s"),
        Part("device_app_events", "m.received_at < %s"),
        Part("device_metrics", "m.received_at < %s"),
        Part("owner_feedback", "m.said_at < %s"),
        Part("pisa_owner_reports", "m.made_at < %s"),
        Part("pisa_replay_runs", "m.at < %s"),
        Part("pisa_skill_uses", "m.used_at < %s"),
        Part("obligations", "m.created_at < %s"),  # the admin radar
        Part("intentions", "m.created_at < %s"),  # archived, never deleted by Pisa
        Part("dossier_items", "m.created_at < %s"),  # what Pisa said each morning, their thumbs
        Part("day_rehearsals", "m.rehearsed_at < %s"),  # "domani, provato"
        Part("plan_actuals", "m.scored_at < %s"),  # planned against actual
        Part("plan_bands", "m.computed_at < %s"),  # their band, made from their days
    ),
    # Pisa's memory: their days as episodes, their +1/-1, Pisa's guesses, the nights (their children cascade),
    # and Memoria (their standing instructions and their notes)
    "memory": (
        Part("episodes", "m.day < %s", DAY),
        Part("pisa_explicit", "m.at < %s"),
        Part("hunches", "m.created_at < %s"),
        Part("pisa_nights", "m.night < %s AND m.status <> 'running'", DAY),  # tonight's run is the night's
        # a month that holds the day stays
        Part("pisa_meter", "m.month < date_trunc('month', %s::date)", DAY),
        Part("pisa_desk_nights", "m.night < %s", DAY),
        Part("pisa_desk_scores", "m.night < %s", DAY),
        Part("pisa_desk_windows", "m.day < %s", DAY),
        Part("pisa_idea_deltas", "m.night < %s", DAY),
        Part("pisa_idea_map", "m.night < %s", DAY),
        Part("owner_instructions", "m.created_at < %s"),  # Memoria: their standing instructions
        Part("owner_notes", "m.created_at < %s"),  # Memoria: their notes
    ),
    # their health and sport days (derived numbers only)
    "health": (Part("health_days", "m.day < %s", DAY),),
    # their voice: the enrolment recordings, the prints per route, their consent.
    # erased as one, by their first consent's date (every row of their voice comes after it): never prints
    # without their recordings or a consent left behind; the consents go last
    "voice": (
        Part("voice_recordings", VOICE_SINCE),
        Part("voice_profiles", VOICE_SINCE),
        Part("voice_consents", VOICE_SINCE),
    ),
    # their sources (their segments and chunks cascade) and the answers Pisa gave from them
    "learn": (
        Part("learn_sources", "m.created_at < %s"),
        Part("learn_answer_log", "m.created_at < %s"),
    ),
}


ASKED = timedelta(microseconds=1)  # "older than" the moment they asked takes a row of that instant too


def cutoff(before: date, tz: str) -> datetime:
    """The first instant of `before` in their zone (UTC): a row older than this goes."""
    return datetime.combine(before, time.min, ZoneInfo(tz)).astimezone(UTC)


def month_start(now: datetime, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return datetime.combine(local.date().replace(day=1), time.min, ZoneInfo(tz)).astimezone(UTC)


def at(part: Part, before: date, tz: str, now: datetime) -> date | datetime:
    """The part's cutoff: the date itself (`DAY`), else its first instant in their zone, never newer than the
    moment they asked (`before` tomorrow: everything up to now, a row of that very instant too) nor than the
    part's guard allows (now - guard; the month's start for the spend caps)."""
    if part.kind == DAY:
        return before
    cut = min(cutoff(before, tz), now + ASKED)
    if part.guard is not None:
        cut = min(cut, now - part.guard)
    if part.month:
        cut = min(cut, month_start(now, tz))
    return cut


def future(before: date, tz: str, now: datetime) -> bool:
    """A date after tomorrow in their zone: refused. Tomorrow is "all of it": every row up to the moment they
    asked (today's too), never one written after it (a resumed job keeps the moment it was asked)."""
    return before > now.astimezone(ZoneInfo(tz)).date() + timedelta(days=1)


def target(family: str, before: date) -> str:
    """What the phone signs (`device` challenge, op `erase`): `<family>:<YYYY-MM-DD>`."""
    return f"{family}:{before.isoformat()}"
