"""The AI disclosure and the user's consent, which every turn needs before it reaches an AI provider.

    python consent.py [--state FILE]

It runs a turn before consent (RUN_ERROR ai_consent_required), shows the disclosure, records the consent, and runs
the turn again.
"""

import asyncio
import sys
import uuid
from typing import Any

from chat_live import turn
from chat_runs import run_collected
from pisa_example import State, arguments, http_client
from render import Conversation
from session import Session


# [guide:disclosure]
async def ask_consent(session: Session, health: bool = False) -> dict[str, Any]:
    """Show which providers get which data, then record the user's yes for this revision."""
    disclosure = await session.request("GET", "/v1/ai/disclosure")
    for p in disclosure["processors"]:  # one per feature slot: show all of them before asking
        provider = p["provider"]  # {id, name, kind: cloud | self_hosted | on_server, region?}
        print(
            f"{p['slot']}: {provider['name']} ({provider['kind']}) {p['model']} gets {', '.join(p['dataCategories'])}"
        )
    # health is asked separately (healthRequiresSeparateConsent is always true): never pre-ticked
    await session.request(
        "POST", "/v1/ai/consent", json={"revision": disclosure["revision"], "health": health}
    )
    return disclosure


def with_consent(body: dict[str, Any], revision: str, health: bool = False) -> dict[str, Any]:
    """Or carry it in each run: forwardedProps.pisa.consent wins over the recorded one."""
    body["forwardedProps"]["pisa"]["consent"] = {"aiRevision": revision, "health": health}
    return body


# [/guide:disclosure]


async def main() -> int:
    args = arguments(__doc__ or "").parse_args()
    state = State.load(args.state)
    thread = f"consent-{uuid.uuid4()}"
    async with http_client(state) as http:
        session = Session(http, state)
        screen = Conversation()
        before = await run_collected(session, turn(thread, "Hello"), screen)
        print(f"before consent: {before and before.get('code')}")
        disclosure = await ask_consent(session)
        after = await run_collected(
            session, with_consent(turn(thread, "Hello"), disclosure["revision"]), screen
        )
        print(f"after consent: {after and after['type']} {after and after.get('code', '')}")
    ok = before is not None and before.get("code") == "ai_consent_required"
    ok = ok and after is not None and after.get("code") != "ai_consent_required"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
