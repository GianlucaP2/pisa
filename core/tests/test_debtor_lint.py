"""The debtor lint: a task that someone else owes must not read as the owner's to-do. Synthetic names only."""

import pytest

from pisa.domain.debtor_lint import OTHER_AS_OWNER_TODO, OWNER_AS_COUNTERPART, lint, refused

OWNER = ("Dana Test", "dana.test")


@pytest.mark.parametrize(
    "title",
    [
        "Reply to the shop about the refund",
        "Rispondere al negozio sul rimborso",
        "Send the invoice to the courier",
        "Paga la fattura al fornitore",
    ],
)
def test_debtor_lint(title: str) -> None:
    # someone else owes it, and it reads as the owner's to-do: refused
    assert [f.code for f in lint(title, debtor="other")] == [OTHER_AS_OWNER_TODO]
    assert refused(title, debtor="other")
    # the same words are fine when the owner owes the action, or nobody does
    assert not refused(title, debtor="owner")
    assert not refused(title, debtor="none")


@pytest.mark.parametrize(
    "title",
    [
        "Waiting for the shop to answer the refund",
        "In attesa del negozio per il rimborso",
        "Awaiting the courier's pickup of AB-1042",
        "Aspettare la risposta del negozio",
        "The refund for AB-1042",
    ],
)
def test_debtor_lint_waits_pass(title: str) -> None:
    assert not refused(title, debtor="other")


def test_debtor_lint_owner_as_counterpart() -> None:
    assert [
        f.code for f in lint("Reply to Dana Test about the refund", debtor="owner", owner_names=OWNER)
    ] == [OWNER_AS_COUNTERPART]
    assert refused("Rispondere a dana.test per il rimborso", debtor="owner", owner_names=OWNER)
    # another person with another name is fine; so is the name away from a talking verb
    assert not refused("Reply to Marco Example about the refund", debtor="owner", owner_names=OWNER)
    assert not refused("Pick up the parcel for Dana Test", debtor="owner", owner_names=OWNER)
    # both rules at once
    codes = {f.code for f in lint("Reply to Dana Test", debtor="other", owner_names=OWNER)}
    assert codes == {OTHER_AS_OWNER_TODO, OWNER_AS_COUNTERPART}
    assert lint("", debtor="other") == ()
