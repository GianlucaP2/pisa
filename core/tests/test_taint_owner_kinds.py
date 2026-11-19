"""E1's R2b rulings: the user's own profile is never outside text, wherever the turn records it; and Pisa's
own day context never holds a profile write, this turn's or an earlier one's."""

from pisa.brain.untrusted import current, open_turn, watch_turn
from pisa.domain import decisions


def _turn() -> None:
    open_turn([])
    watch_turn()


def test_a_past_profile_listing_no_longer_refuses_outward_or_spend_tools() -> None:
    with decisions.collecting():
        _turn()
        current().read_past(["profile", "owner-profile", "owner-question"])
        current().read("profile")
        current().read_beside(["owner-profile"])
        assert current().gated() == ([], [])  # what TaintedTurn refuses outward and spend tools on
        assert current().outside() == [] and current().holds_profile() == []


def test_an_earlier_turn_s_email_still_refuses_them() -> None:
    with decisions.collecting():
        _turn()
        current().read_past(["profile", "email"])
        assert current().gated() == ([], ["email"])


def test_pisa_s_day_context_never_holds_a_profile_write() -> None:
    with decisions.collecting():
        _turn()
        current().read("day-context")
        current().read_past(["day-context"])
        assert current().holds_profile() == []
        current().read("email")
        assert current().holds_profile() == ["email"]
