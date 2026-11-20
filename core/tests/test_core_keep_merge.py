"""The keep review's findings, fixed at the merge."""

import asyncio

import psycopg
import pytest

from pisa.domain.episodes import asks_past
from pisa.domain.health import visit_kinds
from pisa.domain.reasons import said_reason
from pisa.domain.tongue import speaks
from pisa.services import erase, maintenance

HIS_VISITS = [
    "sono passato dall'ospedale ieri?",
    "sono andato in pronto soccorso ieri?",
    "sono andato dal mio medico ieri?",
    "ieri sono andato a fare le analisi?",
    "a che ora sono uscito dall'ospedale?",
    "sono stato dall'oculista?",
    "sono andata dall'ortopedico?",
    "sono rimasto tanto dal dentista?",
    "sono arrivato in ritardo dal medico?",
    "did I go see the doctor?",
    "ik ben gisteren bij de dokter geweest?",
]
NOT_A_VISIT = [
    "did I get the medicine yesterday?",
    "ho visto il dottor House in tv ieri?",
    "ho visto le medicine nuove?",
    "did I see a doctor on TV last night?",
    "ho preso le medicine ieri?",
]


@pytest.mark.parametrize("words", HIS_VISITS)
def test_his_visit_with_elisions_possessives_and_multiword_places_is_heard(words: str) -> None:
    assert visit_kinds(words)


@pytest.mark.parametrize("words", NOT_A_VISIT)
def test_medicine_and_a_screen_are_no_visit(words: str) -> None:
    assert not visit_kinds(words)


def test_pasta_is_no_past_question_and_rimasto_still_is() -> None:
    assert not asks_past("abbiamo pasta in casa?")
    assert not asks_past("hai pasta per stasera?")
    assert asks_past("quanto sono rimasto dal medico?")


@pytest.mark.parametrize(
    "line", ["Invoice due soon.", "Deadline approaching.", "Una deadline: payment due il 20."]
)
def test_a_short_english_reason_is_not_his_italian(line: str) -> None:
    assert not speaks(line, "it-IT")


@pytest.mark.parametrize("line", ["Scadenza imminente.", "Rinnovo del dominio entro venerdì", "F24 16/11"])
def test_a_short_italian_reason_stays(line: str) -> None:
    assert speaks(line, "it-IT")


def test_a_task_reason_never_says_there_is_nothing_to_do() -> None:
    breach = said_reason("Someone signed in from a new device", "info", "account_breach", "it-IT", task=True)
    assert breach and "account" in breach and "informazioni" not in breach.lower()
    assert said_reason("Something unusual", "notification", None, "it-IT", task=True) == "Da fare"
    assert said_reason("Something unusual", "notification", None, "it-IT") != "Da fare"  # an email's detail


def test_a_transient_database_error_leaves_the_job_for_the_resume() -> None:
    class Gone(psycopg.OperationalError):
        sqlstate = "57P01"

    class Deadlock(psycopg.errors.DeadlockDetected):
        pass

    assert erase._transient(Gone())
    assert erase._transient(Deadlock())
    assert erase._transient(ConnectionResetError())
    assert not erase._transient(ValueError("x"))
    assert not erase._transient(psycopg.errors.UndefinedTable())


def test_the_resume_runs_when_the_places_prune_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    async def pending(conninfo: str) -> int:
        ran.append(conninfo)
        return 0

    class Settings:
        database_url = "postgresql://nowhere.invalid/none"

    monkeypatch.setattr(erase, "run_pending", pending)
    with pytest.raises(psycopg.OperationalError):
        asyncio.run(maintenance.prune_places(Settings()))  # type: ignore[arg-type]
    assert ran == ["postgresql://nowhere.invalid/none"]
