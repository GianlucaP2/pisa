"""Fixes at the merge (the contract rows): each fails
without its fix."""

from datetime import UTC, datetime, timedelta

from pisa.domain import approvals as rules

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "00000000-0000-4000-8000-000000000001", "kind": "email_send", "summary": "s", "details": [],
        "created_at": NOW, "expires_at": NOW + timedelta(minutes=10), "status": "pending", "result": None,
        "undo_until": None, "undone_at": None, "requires_biometric": False, "origin": "proposal",
    }  # fmt: skip
    return row | changes


def test_a_replaced_email_question_reads_expired_for_the_app() -> None:
    seen = rules.view(_row(status="superseded"), NOW, "Europe/Rome")
    assert seen["status"] == "expired" and seen["result"] == rules.SUPERSEDED_RESULT


def test_undo_minutes_on_the_wire_stay_within_what_builds_up_to_21_accept() -> None:
    assert rules.WIRE_UNDO_MINUTES_MAX == 60
