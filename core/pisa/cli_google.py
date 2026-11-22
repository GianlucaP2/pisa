"""`pisa google setup`: the Google setup guide (`domain.google_setup`) as plain text, for `pisa.cli`.

The redirect URI comes from this server's PISA_ADDRESSES (its first https address with a name); the scopes are
the connector's own. Numbered steps, each with its page and the text to copy, then the legal notes."""

import argparse
import sys
import textwrap
from collections.abc import Callable
from typing import Any, TextIO

from pisa.connectors.google.oauth import SCOPES
from pisa.domain import google_setup, locale
from pisa.settings import Settings

_HEADINGS = {
    "en": {"title": "Connect Google: your own Google project", "open": "Open", "copy": "Copy",
           "redirect": "Redirect URI", "none": "none yet", "legal": "Good to know"},
    "it": {"title": "Collega Google: il tuo progetto Google", "open": "Apri", "copy": "Copia",
           "redirect": "URI di reindirizzamento", "none": "non ancora", "legal": "Da sapere"},
}  # fmt: skip

_WRAP: dict[str, Any] = {"break_long_words": False, "break_on_hyphens": False}  # URLs stay whole


def render(doc: dict[str, Any], lang: str) -> list[str]:
    """The guide as plain-text lines."""
    words = locale.pick(_HEADINGS, lang)
    lines = [words["title"], "", f"{words['redirect']}: {doc['redirect_uri'] or words['none']}", ""]
    for n, step in enumerate(doc["steps"], 1):
        lines.append(f"{n}. {step['title']}")
        lines += textwrap.wrap(step["body"], 100, initial_indent="   ", subsequent_indent="   ", **_WRAP)
        if step["url"]:
            lines.append(f"   {words['open']}: {step['url']}")
        if step["copy"]:
            lines.append(f"   {words['copy']}:")
            lines += [f"     {line}" for line in step["copy"].splitlines()]
        lines.append("")
    lines.append(words["legal"] + ":")
    for line in doc["legal"]:
        lines += textwrap.wrap(line, 100, initial_indent="- ", subsequent_indent="  ", **_WRAP)
    return lines


def cmd_google_setup(args: argparse.Namespace, settings: Settings, out: TextIO | None = None) -> int:
    lang = args.lang or settings.language
    doc = google_setup.guide(google_setup.redirect_uri(settings.address_list), SCOPES, lang)
    print("\n".join(render(doc, lang)), file=out or sys.stdout)
    return 0


def add_google_parser(sub: Any, func: Callable[[argparse.Namespace], int]) -> None:
    google = sub.add_parser("google", help="the Google connector (Gmail and Calendar)").add_subparsers(
        dest="google_command", required=True
    )
    setup = google.add_parser("setup", help="how to make your own Google project and OAuth client for Pisa")
    setup.add_argument("--lang", choices=locale.LANGUAGES, help="the guide's language (default: Pisa's)")
    setup.set_defaults(func=func)
