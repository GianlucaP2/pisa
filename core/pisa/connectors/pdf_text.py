"""A PDF's text, read locally with pypdf. Never a third-party service, never the network.

The parse runs in a child process (`pisa.connectors.pdf_worker`) capped by `setrlimit` (address
space, CPU) and pypdf's own decompression limits, and is killed at `READ_SECONDS` of wall clock; whatever goes
wrong inside is `unreadable`, never an exception here. Caps before it starts: the bytes (`MAX_BYTES`); inside:
the pages (`MAX_PAGES`, a longer PDF is not read at all) and the text taken (`MAX_CHARS`). An encrypted PDF is
not opened. Whatever the PDF holds is outside text: the caller's rules copy facts out of it.
"""

import asyncio
import json
import sys
from typing import Literal, cast

from pisa.domain.bill_pdf import MAX_BYTES, MAX_CHARS, MAX_PAGES, READ_SECONDS

Outcome = Literal["read", "too_big", "too_many_pages", "unreadable", "encrypted"]
_OUTCOMES = frozenset({"read", "too_many_pages", "unreadable", "encrypted"})
_OUTPUT_MAX = MAX_CHARS * 8 + 1024  # JSON escapes included


async def pdf_text(data: bytes, *, seconds: float = READ_SECONDS) -> tuple[Outcome, str]:
    """(outcome, text): the text only when `outcome` is `read`. Never raises for what the PDF holds."""
    if len(data) > MAX_BYTES:
        return "too_big", ""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-m", "pisa.connectors.pdf_worker", str(MAX_PAGES), str(MAX_CHARS),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
    except OSError:
        return "unreadable", ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), seconds)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return "unreadable", ""
    if proc.returncode != 0 or len(out) > _OUTPUT_MAX:
        return "unreadable", ""
    try:
        found = json.loads(out)
        outcome, text = str(found["outcome"]), str(found["text"])
    except (ValueError, KeyError, TypeError):
        return "unreadable", ""
    if outcome not in _OUTCOMES:
        return "unreadable", ""
    return cast(Outcome, outcome), text[:MAX_CHARS] if outcome == "read" else ""


# a paper's pages, each on its own (the page-number oracle for citations). A paper is longer than a
# bill: a higher ceiling, more CPU and memory, same isolation.
PAPER_MAX_PAGES = 600
PAPER_MAX_BYTES = 128 * 1024 * 1024
PAPER_MAX_CHARS = 4_000_000
PAPER_CPU_S = 120
PAPER_MEMORY = 2 * 1024 * 1024 * 1024
PAPER_STREAMS = 256 * 1024 * 1024
PAPER_SECONDS = 180.0


async def pdf_pages(data: bytes) -> tuple[Outcome, list[str]]:
    """(outcome, one text a page); never raises for what the PDF holds."""
    if len(data) > PAPER_MAX_BYTES:
        return "too_big", []
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-m", "pisa.connectors.pdf_worker", str(PAPER_MAX_PAGES),
            str(PAPER_MAX_CHARS), "pages", str(PAPER_CPU_S), str(PAPER_MEMORY), str(PAPER_STREAMS),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
    except OSError:
        return "unreadable", []
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), PAPER_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return "unreadable", []
    try:
        found = json.loads(out)
        outcome, pages = str(found["outcome"]), [str(p) for p in found["pages"]]
    except (ValueError, KeyError, TypeError):
        return "unreadable", []
    if outcome not in _OUTCOMES:
        return "unreadable", []
    return cast(Outcome, outcome), pages if outcome == "read" else []
