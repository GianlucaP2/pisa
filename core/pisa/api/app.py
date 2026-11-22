"""FastAPI application factory."""

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pisa import __version__
from pisa.api import (
    approvals,
    briefings,
    connectors,
    events,
    follow,
    live,
    loopback,
    places,
    plan,
    profile,
    rest,
    runs,
    speech,
    talk,
    tasks,
    voice,
    weather,
)
from pisa.api import autopilot as autopilot_api
from pisa.api import dossier as dossier_api
from pisa.api import erase as erase_api
from pisa.api import intentions as intentions_api
from pisa.api import learn as learn_api
from pisa.api import memory as memory_api
from pisa.api import money as money_api
from pisa.api import note as note_api
from pisa.api import pairing as pairing_api
from pisa.api import people as people_api
from pisa.api import public as public_api
from pisa.api import rituals as rituals_api
from pisa.api import server as server_api
from pisa.api import settings as settings_api
from pisa.api import trips as trips_api
from pisa.api import views as views_api
from pisa.api import voice_id as voice_id_api
from pisa.api import world as world_api
from pisa.api.deps import feature
from pisa.brain import understand
from pisa.db.errors import database_unreachable
from pisa.db.migrate import applied_versions, latest_version
from pisa.domain import experimental
from pisa.domain.errors import PisaError
from pisa.infra import locks
from pisa.logging import get_logger, safe_path
from pisa.services import disclosure, session_registry
from pisa.services.notifications import Dispatcher, listen_outbox
from pisa.services.push import push_configured, push_key_stored
from pisa.services.runtime import Runtime
from pisa.settings import Settings, get_settings

RuntimeFactory = Callable[[Settings], Awaitable[Runtime]]
_log = get_logger("pisa.api")


def error_response(err: PisaError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": err.code, "message": err.message, "action": err.action, **err.extra}},
        status_code=err.http_status,
    )


# U+0000 in a JSON string (`\u0000` after an even run of backslashes) or in the query string (`%00`)
_JSON_NUL = re.compile(rb"(?<!\\)(?:\\\\)*\\u0000")
_QUERY_NUL = re.compile(rb"%00")
NUL_REFUSED = PisaError(
    "validation_failed", "Text can't contain the NUL character (U+0000).", "Remove it and try again"
)


class NulGuard:
    """No text with U+0000 gets in (400 validation_failed): Postgres text can't hold it, so a request carrying
    one in its path, query or JSON body would fail half-way in the database (a 500). Checked at the door,
    before any route runs; other bodies (files) pass untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if "\x00" in scope["path"] or _QUERY_NUL.search(scope.get("query_string", b"")):
            await error_response(NUL_REFUSED)(scope, receive, send)
            return
        content_type = dict(scope["headers"]).get(b"content-type", b"").split(b";")[0].strip().lower()
        if not (content_type == b"application/json" or content_type.endswith(b"+json")):
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, receive, send)  # the client left: the app sees the disconnect
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        if _JSON_NUL.search(body):
            await error_response(NUL_REFUSED)(scope, receive, send)
            return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _validation_message(exc: RequestValidationError) -> str:
    # Never echo the input: it may be an API key.
    first = exc.errors()[0] if exc.errors() else {}
    loc = [str(p) for p in first.get("loc", ()) if p not in ("body", "query", "path")]
    field = ".".join(loc) or "body"
    detail = str(first.get("msg", "invalid value"))
    return f"The field '{field}' is invalid: {detail[:1].lower()}{detail[1:].rstrip('.')}."


def create_app(settings: Settings | None = None, *, runtime_factory: RuntimeFactory | None = None) -> FastAPI:
    settings = settings or get_settings()
    factory: RuntimeFactory = runtime_factory or Runtime.start

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # RAIL: one API process per database (locks.API_PROCESS): held for the process's life.
        guard = await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True)
        got = await (await guard.execute("SELECT pg_try_advisory_lock(%s)", (locks.API_PROCESS,))).fetchone()
        if not (got and got[0]):
            await guard.close()
            raise RuntimeError(
                "Another Pisa API process is running on this database: Pisa's open questions live in one "
                "process's memory, so run one API worker (uvicorn --workers 1)."
            )
        runtime = await factory(settings)
        if not push_configured(settings.apns_team_id, settings.apns_topic):  # optional: Pisa works without
            _log.info("push_off", why="PISA_APNS_TEAM_ID and PISA_APNS_TOPIC are not set")
        app.state.runtime = runtime
        app.state.sessions = session_registry.SessionRegistry(runtime.voice)
        runtime.live = app.state.sessions  # proactive messages go to live sockets first
        dispatcher = Dispatcher(
            runtime.pool,
            app.state.sessions,
            runtime.push,
            runtime.clock,
            unasked=runtime.conversations.unasked,
            breakpoints=app.state.sessions,
            owner=runtime.owner,
        )
        app.state.sessions.outbox = dispatcher  # what the sockets release at a breakpoint or put off
        dispatcher.ask = runtime.talk.may_ask  # Pisa speaks first with Pisa closed (TALK_ASK)

        async def tell_disclosure() -> None:  # a slot or model server changed, in this process or another
            async with runtime.pool.connection() as conn:
                await disclosure.tell(conn, settings.ollama_base_url, app.state.sessions)

        listeners = [
            asyncio.create_task(
                session_registry.listen_for_revocations(
                    settings.database_url, app.state.sessions, on_disclosure=tell_disclosure
                )
            ),
            outbox := asyncio.create_task(listen_outbox(settings.database_url, dispatcher)),
            asyncio.create_task(asyncio.to_thread(understand.warm)),  # wordfreq's lists, off the loop
        ]
        app.state.outbox_listener = outbox  # tests pause it to drive one dispatcher only
        await runtime.erase.resume()  # a deletion a restart interrupted runs again
        await runtime.learn.resume()  # a source a restart interrupted is ingested again
        try:
            yield
        finally:
            for listener in listeners:
                listener.cancel()
            await asyncio.gather(*listeners, return_exceptions=True)
            await runtime.close()
            # Released before the close: a closed connection's lock is freed only once its server process
            # has exited, a moment later, so a restarting API process could find it still held.
            with contextlib.suppress(psycopg.Error):  # a connection that can't answer: it goes with it
                await guard.execute("SELECT pg_advisory_unlock(%s)", (locks.API_PROCESS,))
            await guard.close()

    # in production no API map without a token (/openapi.json, /docs, /redoc); CI writes the
    # schema with `app.openapi()` (scripts/ci/gen_openapi.py), which works either way.
    hidden: dict[str, Any] = (
        {"docs_url": None, "redoc_url": None, "openapi_url": None} if settings.env == "prod" else {}
    )
    app = FastAPI(title="Pisa Core", version=__version__, lifespan=lifespan, **hidden)
    app.add_middleware(NulGuard)
    if settings.insecure_loopback_pairing:  # plain http on a developer's machine: this machine only
        app.add_middleware(loopback.LoopbackOnly, extra=loopback.allowed_peers(settings))
    app.include_router(pairing_api.router)  # contract 2: before /v1/devices/{id} (`me` is no id)
    app.include_router(rest.router)
    app.include_router(connectors.router)
    app.include_router(connectors.callback)  # the user's own server: Google comes back here
    app.include_router(connectors.health)
    app.include_router(connectors.obligations)  # the admin radar
    app.include_router(tasks.router)
    app.include_router(intentions_api.router)
    app.include_router(dossier_api.router)
    app.include_router(settings_api.router)
    app.include_router(erase_api.router)
    app.include_router(briefings.router)
    app.include_router(
        rituals_api.router, dependencies=[feature("rituals")]
    )  # before places: /v1/places/notes isn't a place id
    app.include_router(places.router, dependencies=[feature("places")])
    app.include_router(voice.router)
    app.include_router(voice_id_api.router)
    app.include_router(learn_api.router, dependencies=[feature("learn")])
    app.include_router(weather.router)
    app.include_router(plan.router)
    app.include_router(events.router)
    app.include_router(follow.router)
    app.include_router(approvals.router)
    app.include_router(
        note_api.router, dependencies=[feature("note")]
    )  # their note, before /v1/profile's own paths
    app.include_router(profile.router, dependencies=[feature("profile")])
    app.include_router(views_api.router, dependencies=[feature("views")])
    app.include_router(trips_api.router, dependencies=[feature("trips")])
    app.include_router(public_api.router, dependencies=[feature("public")])
    app.include_router(people_api.router, dependencies=[feature("people")])
    app.include_router(autopilot_api.router)
    app.include_router(money_api.router, dependencies=[feature("money")])
    app.include_router(memory_api.router, dependencies=[feature("memory")])
    app.include_router(speech.router)
    app.include_router(talk.router)
    app.include_router(live.router)
    app.include_router(runs.router)
    app.include_router(server_api.router)  # discovery (no auth)
    app.include_router(world_api.public)  # Pisa's card and keys (public, no owner data)
    app.include_router(world_api.router)

    @app.exception_handler(PisaError)
    async def _pisa_error(_: Request, exc: PisaError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            PisaError("validation_failed", _validation_message(exc), "Fix the request and try again")
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            err = PisaError(
                "not_found",
                f"There is no endpoint {request.method} {safe_path(request.url.path)}.",
                "Check the API path",
            )
        else:
            err = PisaError(
                "validation_failed",
                f"The request {request.method} {safe_path(request.url.path)} was refused: {exc.detail}.",
                "Check the request",
            )
        response = error_response(err)
        response.status_code = exc.status_code
        return response

    @app.exception_handler(psycopg.OperationalError)
    async def _database(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
        _log.error("database_unreachable", path=safe_path(request.url.path), cause=exc.__class__.__name__)
        return error_response(database_unreachable())

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        _log.exception("request_failed", path=safe_path(request.url.path))
        return error_response(
            PisaError(
                "internal",
                f"Pisa failed on {request.method} {safe_path(request.url.path)} ({exc.__class__.__name__}).",
                "Try again",
            )
        )

    def openapi() -> dict[str, Any]:
        """FastAPI documents request-validation errors as 422; Pisa answers them with 400
        validation_failed."""
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            for path in schema.get("paths", {}).values():
                for operation in path.values():
                    responses = operation.get("responses", {})
                    if responses.pop("422", None) is not None:
                        responses.setdefault(
                            "400",
                            {
                                "description": "validation_failed",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                    }
                                },
                            },
                        )
            for name in ("HTTPValidationError", "ValidationError"):
                schema.get("components", {}).get("schemas", {}).pop(name, None)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]

    @app.get("/health")
    def health() -> dict[str, str]:
        """Alive: the process runs. Never touches dependencies."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Ready: the database answers and the schema is up to date. Names the failing check. `push` says
        whether Pisa can send pushes (APNs is optional: "off" until its settings and key are there); it never
        makes Pisa not ready. `relay` says whether the push relay is set (PISA_PUSH_RELAY_URL: relay mode is
        offered to the app; the experimental `push_relay` on too)."""
        checks: dict[str, str] = {}
        push = "off"
        # the relay: "on" only when it is set and on (review mode keeps it off: `experimental.REVIEW_OFF`)
        relayed = experimental.on(
            "push_relay", experimental.enabled(settings.experimental, review_mode=settings.review_mode)
        )
        relay = "on" if settings.push_relay_url and relayed else "off"
        try:
            with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
                applied = max(applied_versions(conn), default=0)
                if applied and push_configured(settings.apns_team_id, settings.apns_topic):
                    push = "on" if push_key_stored(conn) else "off"
            expected = latest_version()
            checks["database"] = "ok"
            checks["schema"] = (
                "ok" if applied == expected else f"at {applied}, expected {expected}: run pisa migrate"
            )
        except psycopg.OperationalError as exc:
            checks["database"] = f"unreachable: {exc.__class__.__name__}"
        ok = all(v == "ok" for v in checks.values()) and "schema" in checks
        return JSONResponse(
            {"status": "ok" if ok else "not_ready", "checks": checks, "push": push, "relay": relay},
            status_code=200 if ok else 503,
        )

    return app
