"""`pisa setup` follows the app's Connections (Collegamenti): its six categories, in its order, by its names
(`domain.connections`), between your place and pairing; each category's section says what is set and what is
next. Synthetic data only."""

import asyncio
import io
from typing import Any

import psycopg
import pytest

from pisa.domain import guide
from pisa.domain.connections import CATEGORIES, CATEGORY_NAMES
from tests.conftest import dsn
from tests.test_pair_cli import setup_args


def test_the_sections_are_connections_categories_in_its_order_and_names() -> None:
    assert guide.STEPS[0] == "place" and guide.STEPS[-1] == "pair"
    assert [guide.CATEGORY_OF[s] for s in guide.STEPS[1:-1]] == list(CATEGORIES)
    for lang in ("en", "it"):
        assert set(guide.CATEGORY_TITLES[lang]) == set(CATEGORIES)
    titles = [guide.title(s, "en") for s in guide.STEPS]
    assert titles[1:-1] == [f"{n}. {CATEGORY_NAMES[c]}" for n, c in enumerate(CATEGORIES, 2)]
    assert titles[0] == "1. Your place in the world" and titles[-1] == "8. Pairing"
    assert guide.title("maps", "it") == "4. Mappe e luoghi"
    # the old names still skip: places is both maps and web
    assert guide.skipped(["places,voice", "notifications"]) == {"maps", "web", "talk", "push"}


@pytest.mark.parametrize("lang", ["en", "it"])
def test_every_category_section_says_what_is_set_and_what_is_next(
    app_settings: Any, db: psycopg.Connection, lang: str
) -> None:
    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup

    out = io.StringIO()
    args = setup_args(lang=lang, skip=["place,pair"], push="none")
    settings = app_settings.model_copy(update={"database_url": dsn(db)})
    assert asyncio.run(run_setup(args, settings, Prompt(False, out=out), http=None)) == 0
    text = out.getvalue()
    at = [text.index(guide.title(step, lang)) for step in guide.STEPS]
    assert at == sorted(at)  # in Connections' order
    sections = {
        step: text[start:end] for step, start, end in zip(guide.STEPS, at, [*at[1:], len(text)], strict=True)
    }
    for step in ("ai", "talk", "maps", "web", "accounts"):  # each ends with what is next
        assert any(line.startswith(guide.text("next.none", lang).split(":")[0] + ":")
                   for line in sections[step].splitlines()), step  # fmt: skip
    assert guide.text("talk.next", lang) in sections["talk"]
    assert guide.text("maps.next", lang) in text and guide.text("web.next", lang) in text
    google = guide.text("accounts.google.no_client", lang)
    assert guide.text("accounts.state", lang, google=google) in text
    assert guide.text("accounts.next.no_client", lang) in text


def test_accounts_names_the_next_step_from_google_s_state(app_settings: Any, db: psycopg.Connection) -> None:
    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup

    db.execute(
        "INSERT INTO connector_credentials (connector, web_client_id, web_client_secret_enc) "
        "VALUES ('google', 'client-0000.apps.example.test', '\\x00')"
    )
    out = io.StringIO()
    only = setup_args(lang="en", skip=["place,ai,talk,maps,web,push,pair"])
    settings = app_settings.model_copy(update={"database_url": dsn(db)})
    assert asyncio.run(run_setup(only, settings, Prompt(False, out=out))) == 0
    google = guide.text("accounts.google.not_connected", "en")
    assert guide.text("accounts.state", "en", google=google) in out.getvalue()
    assert guide.text("accounts.next.not_connected", "en") in out.getvalue()
