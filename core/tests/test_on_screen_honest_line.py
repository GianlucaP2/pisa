"""§31 g7: a reminder confirmation that says the user will see it when they open the app is true, and is
never taken for an "it's on your phone" claim: "…quando apri l'app, perché sul telefono le notifiche…" has
its verb and the phone in two clauses."""

import pytest

from pisa.domain import claims

HONEST = (
    "Promemoria impostato per le 04:40: lo vedrai quando apri l'app, perché sul telefono le notifiche non "
    "arrivano a app chiusa."
)


def test_the_honest_reminder_line_is_no_screen_claim() -> None:
    assert claims.ON_SCREEN.search(HONEST) is None
    assert claims.split_screen_claim(HONEST) == (HONEST, "")


@pytest.mark.parametrize(
    "claim",
    ["Trovi tutto sul telefono.", "Apri la scheda sul telefono.", "Ecco, guarda sul telefono.",
     "Ti ho mandato l'indirizzo sul telefono."],
)  # fmt: skip
def test_a_real_screen_claim_still_is_one(claim: str) -> None:
    assert claims.ON_SCREEN.search(claim) is not None
