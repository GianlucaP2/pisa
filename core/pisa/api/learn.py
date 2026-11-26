"""Pisa Learn over REST, for the phone. Add sources (arXiv ids, DOIs, URLs, titles, or a PDF or Markdown
file; the ingestion runs in the background), list them with their state, delete one, and ask a question in a
course's context (the cited answer, its sentences, tiers and citations)."""

import base64
import binascii
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from pisa.api import learn_schemas as schemas
from pisa.api.deps import DeviceDep, LangDep, RuntimeDep, require_ai_consent
from pisa.api.rest import ERROR_RESPONSES
from pisa.connectors.pdf_text import PAPER_MAX_BYTES
from pisa.domain.errors import PisaError
from pisa.logging import get_logger

router = APIRouter(prefix="/v1/learn", tags=["learn"], responses=ERROR_RESPONSES)
_log = get_logger("pisa.learn.api")


@router.post("/sources", response_model=list[schemas.LearnSource], status_code=202)
async def add_sources(
    body: schemas.LearnSourcesAdd, device: DeviceDep, runtime: RuntimeDep, lang: LangDep
) -> list[dict[str, Any]]:
    """Each item recorded and answered at once (`queued`, or the source it already is); Pisa reads them in the
    background. An ambiguous title comes back `candidates`: send the chosen `item` back."""
    # the background ingestion embeds their text: 409 `ai_consent_required` until the user's AI consent
    # covers it, before anything is queued (FL-1); the ingestion itself runs in this process, queued here
    await require_ai_consent(runtime, device, lang)
    upload = None
    if body.file is not None:
        try:
            data = base64.b64decode(body.file.data, validate=True)
        except (binascii.Error, ValueError):
            raise PisaError(
                "validation_failed", "The file is not valid base64.", "Send the file again"
            ) from None
        if len(data) > PAPER_MAX_BYTES:
            raise PisaError("validation_failed", "The file is over 128 MB.", "Send a smaller copy")
        upload = (body.file.name, body.file.media_type.lower(), data)
    rows = await runtime.learn.add(body.items, course=body.course, upload=upload, device_id=device.id)
    _log.info("learn_sources_post", device_id=str(device.id), count=len(rows))
    return rows


@router.get("/sources", response_model=schemas.LearnSources)
async def list_sources(
    _: DeviceDep,
    runtime: RuntimeDep,
    course: Annotated[str | None, Query(description="One course's sources only")] = None,
) -> dict[str, Any]:
    return {"sources": await runtime.learn.sources(course)}


@router.delete("/sources/{source_id}", response_model=schemas.LearnDeleted)
async def delete_source(
    source_id: Annotated[uuid.UUID, Path(description="The source")], device: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    """Their delete: the source, its structure and its index (its answers in the log stay, until they delete
    them by family)."""
    gone = await runtime.learn.delete(source_id)
    _log.info("learn_source_delete", device_id=str(device.id), source_id=str(source_id))
    return {"deleted": gone}


@router.post("/ask", response_model=schemas.LearnAnswer)
async def ask(
    body: schemas.LearnAsk, device: DeviceDep, runtime: RuntimeDep, lang: LangDep
) -> dict[str, Any]:
    """A question in a course's context: the cited, checked answer."""
    # 409 `ai_consent_required` until the user's AI consent covers it (health too, when the question touches
    # it), before the question is embedded or any model called (FL-1)
    await require_ai_consent(runtime, device, lang, body.question)
    found = await runtime.learn_ask.ask(body.question, body.course)
    _log.info("learn_ask_post", device_id=str(device.id), answer_id=str(found["answer_id"]))
    return found
