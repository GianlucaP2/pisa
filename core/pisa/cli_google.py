"""`pisa google setup`: the Google setup guide (`domain.google_setup`) as plain text, for `pisa.cli`.

The redirect URI comes from this server's PISA_ADDRESSES (its first https address with a name); the scopes are
the connector's own. Numbered steps, each with its page and the text to copy, then the legal notes.

`--gcloud`: when the gcloud CLI is installed and logged in, the Gmail and Calendar APIs (with `--maps` also
the Places API (New) and the Routes API) are enabled in the user's project with `gcloud services enable`, so
only the OAuth client is left to make by hand. The exact command is shown first and runs only on a yes at the
terminal, or with `--yes`; without a terminal and without `--yes` it is shown, never run. Nothing gcloud says
is printed (its messages can name the signed-in account): only whether it worked. Without gcloud, or not
logged in, the guide's steps as before."""

import argparse
import re
import shlex
import shutil
import subprocess
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

_GCLOUD = {
    "en": {
        "missing": "gcloud is not installed here: enable the APIs by hand, as the steps below say.",
        "logged_out": "gcloud is not logged in (`gcloud auth login`): enable the APIs by hand, as the steps "
        "below say, or log in and run this again.",
        "no_project": "No Google Cloud project is chosen: pass --project <project id> (or `gcloud config set "
        "project <project id>`), or enable the APIs by hand as the steps below say.",
        "bad_project": "That is not a Google Cloud project id (6-30 lowercase letters, digits and hyphens).",
        "runs": "This enables the APIs in project {project} with your gcloud login:",
        "ask": "Run it? [y/N]: ",
        "not_run": "Not run. Run it yourself, or pass --yes; then only the OAuth client is left (steps "
        "below).",
        "no_terminal": "Not run (no terminal to ask): run it yourself, or pass --yes.",
        "done": "Enabled in {project}: {apis}. The API steps below are done; make the OAuth client.",
        "failed": "gcloud failed (exit {code}): run the command above yourself to see why, or enable the "
        "APIs by hand as the steps below say.",
    },
    "it": {
        "missing": "gcloud non è installato qui: abilita le API a mano, come dicono i passi qui sotto.",
        "logged_out": "gcloud non ha un accesso (`gcloud auth login`): abilita le API a mano, come dicono i "
        "passi qui sotto, o accedi e rilancia questo comando.",
        "no_project": "Nessun progetto Google Cloud scelto: passa --project <id del progetto> (o `gcloud "
        "config set project <id del progetto>`), o abilita le API a mano come dicono i passi qui sotto.",
        "bad_project": "Questo non è l'id di un progetto Google Cloud (6-30 lettere minuscole, cifre e "
        "trattini).",
        "runs": "Questo abilita le API nel progetto {project} con il tuo accesso gcloud:",
        "ask": "Eseguirlo? [s/N]: ",
        "not_run": "Non eseguito. Eseguilo tu, o passa --yes; poi resta solo il client OAuth (passi qui "
        "sotto).",
        "no_terminal": "Non eseguito (nessun terminale per chiedere): eseguilo tu, o passa --yes.",
        "done": "Abilitate in {project}: {apis}. I passi delle API qui sotto sono fatti; crea il client "
        "OAuth.",
        "failed": "gcloud non è riuscito (uscita {code}): esegui tu il comando qui sopra per vedere "
        "perché, o abilita le API a mano come dicono i passi qui sotto.",
    },
}
# Google Cloud's project id: 6-30 characters, lowercase letters, digits and hyphens, a letter first
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
GCLOUD_TIMEOUT_S = 300
YES = frozenset({"y", "yes", "s", "si", "sì"})

_WRAP: dict[str, Any] = {"break_long_words": False, "break_on_hyphens": False}  # URLs stay whole

Run = Callable[..., subprocess.CompletedProcess[str]]


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


def gcloud_apis(maps: bool) -> list[str]:
    """The APIs `--gcloud` enables: Gmail and Calendar; with `--maps` Places (New) and Routes too."""
    apis = [google_setup.GMAIL_API, google_setup.CALENDAR_API]
    return [*apis, google_setup.PLACES_API, google_setup.ROUTES_API] if maps else apis


def enable_with_gcloud(
    args: argparse.Namespace,
    lang: str,
    say: Callable[[str], None],
    *,
    run: Run = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    ask: Callable[[str], str] = input,
    interactive: bool = False,
) -> str | None:
    """Enable the Google APIs with gcloud when it can (the module's docstring). Returns the project id when
    one is known (the guide's links name it), else None. Never prints what gcloud says."""
    words = locale.pick(_GCLOUD, lang)
    gcloud = which("gcloud")
    if gcloud is None:
        say(words["missing"])
        return None

    def quiet(*argv: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return run([gcloud, *argv], capture_output=True, text=True, timeout=GCLOUD_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError):
            return None

    accounts = quiet("auth", "list", "--filter=status:ACTIVE", "--format=value(account)")
    if accounts is None or accounts.returncode != 0 or not accounts.stdout.strip():
        say(words["logged_out"])  # never the account itself
        return None
    project = (getattr(args, "project", None) or "").strip()
    if not project:
        found = quiet("config", "get-value", "project")
        project = found.stdout.strip() if found is not None and found.returncode == 0 else ""
    if not project or project == "(unset)":
        say(words["no_project"])
        return None
    if not PROJECT_ID.match(project):
        say(words["bad_project"])
        return None
    apis = gcloud_apis(bool(getattr(args, "maps", False)))
    command = ["services", "enable", *apis, f"--project={project}"]
    say(words["runs"].format(project=project))
    say("  " + shlex.join(["gcloud", *command]))
    if not getattr(args, "yes", False):
        if not interactive:
            say(words["no_terminal"])
            return project
        if ask(words["ask"]).strip().lower() not in YES:
            say(words["not_run"])
            return project
    done = quiet(*command)
    if done is not None and done.returncode == 0:
        say(words["done"].format(project=project, apis=", ".join(apis)))
    else:
        say(words["failed"].format(code=done.returncode if done is not None else "-"))
    return project


def cmd_google_setup(
    args: argparse.Namespace,
    settings: Settings,
    out: TextIO | None = None,
    *,
    run: Run = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    ask: Callable[[str], str] = input,
    interactive: bool | None = None,
) -> int:
    lang = args.lang or settings.language
    stream = out or sys.stdout
    project = getattr(args, "project", None)
    if getattr(args, "gcloud", False):

        def say(line: str) -> None:
            print(line, file=stream)

        tty = sys.stdin.isatty() if interactive is None else interactive
        project = enable_with_gcloud(args, lang, say, run=run, which=which, ask=ask, interactive=tty)
        print("", file=stream)
    if project is not None and not PROJECT_ID.match(project):
        project = None  # never an unchecked id in the guide's links
    doc = google_setup.guide(google_setup.redirect_uri(settings.address_list), SCOPES, lang, project=project)
    print("\n".join(render(doc, lang)), file=stream)
    return 0


def add_google_parser(sub: Any, func: Callable[[argparse.Namespace], int]) -> None:
    google = sub.add_parser("google", help="the Google connector (Gmail and Calendar)").add_subparsers(
        dest="google_command", required=True
    )
    setup = google.add_parser("setup", help="how to make your own Google project and OAuth client for Pisa")
    setup.add_argument("--lang", choices=locale.LANGUAGES, help="the guide's language (default: Pisa's)")
    setup.add_argument(
        "--gcloud",
        action="store_true",
        help="enable the Gmail and Calendar APIs with your gcloud login (shows the command and asks first)",
    )
    setup.add_argument("--maps", action="store_true", help="with --gcloud: also the Places and Routes APIs")
    setup.add_argument("--project", help="the Google Cloud project id (default: gcloud's own)")
    setup.add_argument("--yes", action="store_true", help="with --gcloud: run the command without asking")
    setup.set_defaults(func=func)
