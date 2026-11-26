"""Validate frames against the OFFICIAL AG-UI 1.0 and A2UI 1.0 schemas (vendored, unmodified) plus PISA's own."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from .paths import CATALOG, CATALOG_V2, SCHEMA, VENDOR

CORE_1 = "urn:pisa:a2ui:catalog:core:1"
CORE_2 = "urn:pisa:a2ui:catalog:core:2"
CATALOGS = {CORE_1: CATALOG, CORE_2: CATALOG_V2}
EXT_ID = "urn:pisa:contract:2:extensions"
AGUI_ID = "https://ag-ui.com/spec/1.0/schema.json"
A2UI_A2R = "https://a2ui.org/specification/v1_0/agent_to_renderer.json"
A2UI_R2A = "https://a2ui.org/specification/v1_0/renderer_to_agent.json"
CATALOG_PLACEHOLDER = "https://a2ui.org/specification/v1_0/catalog.json"


def _load(p: Path) -> dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def registry(catalog_id: str = CORE_1) -> Registry:
    """The schemas, with `catalog_id`'s catalog in force (core:1 unless a surface names core:2)."""
    docs = {}
    for f in (VENDOR / "a2ui-1.0").glob("*.json"):
        d = _load(f)
        docs[d["$id"]] = d
    docs[AGUI_ID] = _load(VENDOR / "ag-ui-1.0" / "schema.json")
    for path in CATALOGS.values():
        cat = _load(path)
        docs[cat["$id"]] = cat
    catalog = _load(CATALOGS[catalog_id])
    # A2UI's envelope refers to "catalog.json#/$defs/anyComponent": the catalog in force is substituted there.
    docs[CATALOG_PLACEHOLDER] = catalog
    docs[EXT_ID] = _load(SCHEMA)
    return Registry().with_resources([(k, DRAFT202012.create_resource(v)) for k, v in docs.items()])


def _validator(ref: str, catalog_id: str = CORE_1) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator({"$ref": ref}, registry=registry(catalog_id),
                                           format_checker=jsonschema.FormatChecker())


def errors(instance: Any, ref: str, catalog_id: str = CORE_1) -> list[str]:
    """`instance` against `ref`; A2UI components are judged by `catalog_id`'s catalog (an unknown id: core:1)."""
    if catalog_id not in CATALOGS:
        catalog_id = CORE_1
    return [f"{e.json_path}: {e.message[:240]}" for e in _validator(ref, catalog_id).iter_errors(instance)]


def surface_catalog(content: Any) -> str:
    """The catalog an a2ui-surface activity's createSurface names (core:1 when none does)."""
    ops = content.get("a2ui_operations") if isinstance(content, dict) else None
    for op in ops if isinstance(ops, list) else []:
        made = op.get("createSurface") if isinstance(op, dict) else None
        if isinstance(made, dict) and made.get("catalogId") in CATALOGS:
            return made["catalogId"]
    return CORE_1


def surface_errors(content: Any) -> list[str]:
    """An a2ui-surface activity's content, its components judged by the catalog its createSurface names."""
    return errors(content, EXT_ID + "#/$defs/A2UISurfaceActivity", surface_catalog(content))


def server_frame_errors(frame: dict[str, Any]) -> list[str]:
    ev = frame.get("event")
    catalog_id = CORE_1
    if isinstance(ev, dict) and ev.get("activityType") == "a2ui-surface":
        catalog_id = surface_catalog(ev.get("content"))
    errs = errors(frame, EXT_ID + "#/$defs/ServerFrame", catalog_id)
    if ev is None:
        return errs
    errs += errors(ev, AGUI_ID)  # the event alone is plain AG-UI 1.0
    if ev.get("type") == "CUSTOM":
        errs += errors(ev, EXT_ID + "#/$defs/ServerCustomEvent")
    if ev.get("type") == "ACTIVITY_SNAPSHOT":
        ref = {"a2ui-surface": "A2UISurfaceActivity", "mcp-apps": "McpAppActivity"}.get(ev["activityType"])
        if ref is None:
            errs.append(f"unknown activityType {ev['activityType']}")
        else:
            errs += errors(ev["content"], EXT_ID + "#/$defs/" + ref, catalog_id)
    if ev.get("type") == "RUN_ERROR" and str(frame.get("threadId", "")).startswith("pisa-approval:"):
        errs += errors(ev, EXT_ID + "#/$defs/ApprovalRunError")
    if ev.get("type") == "RUN_FINISHED" and isinstance(ev.get("result"), dict) and "talkEnded" in ev["result"]:
        errs += errors(ev["result"], EXT_ID + "#/$defs/TalkEndedResult")  # G4
    if ev.get("type") == "RUN_FINISHED" and ev.get("outcome", {}).get("type") == "interrupt":
        for it in ev["outcome"]["interrupts"]:
            if it.get("reason") == "pisa.approval":
                errs += errors(it, EXT_ID + "#/$defs/ApprovalInterrupt")
    return errs


def client_frame_errors(frame: dict[str, Any]) -> list[str]:
    errs = errors(frame, EXT_ID + "#/$defs/ClientFrame")
    if frame.get("op") == "run":
        fp = frame["input"].get("forwardedProps", {}).get("pisa")
        if fp is not None:
            errs += errors(fp, EXT_ID + "#/$defs/TurnProps")
        for r in frame["input"].get("resume", []):
            if r.get("status") == "resolved" and str(r["interruptId"]).startswith("int-"):
                errs += errors(r["payload"], EXT_ID + "#/$defs/ApprovalResume")
    return errs


def enrol_server_frame_errors(frame: dict[str, Any]) -> list[str]:
    """G5: a server frame on an enrolment data channel (talk session purpose "enrol"). No thread, no run."""
    errs = errors(frame, EXT_ID + "#/$defs/EnrolServerFrame")
    if isinstance(frame.get("event"), dict):
        errs += errors(frame["event"], AGUI_ID)  # still a plain AG-UI 1.0 CUSTOM event
    return errs


def enrol_client_frame_errors(frame: dict[str, Any]) -> list[str]:
    """G5: an app frame on an enrolment data channel."""
    return errors(frame, EXT_ID + "#/$defs/EnrolClientFrame")


def check_seq(frames: list[dict[str, Any]], first: int | None = None) -> list[str]:
    """seq grows by exactly one per frame (control frames included); the first is `first` when given."""
    problems: list[str] = []
    prev = None
    for i, f in enumerate(frames):
        seq = f.get("seq")
        if not isinstance(seq, int):
            problems.append(f"frame {i}: no integer seq")
            continue
        if prev is None:
            if first is not None and seq != first:
                problems.append(f"frame {i}: first seq is {seq}, expected {first}")
        elif seq != prev + 1:
            problems.append(f"frame {i}: seq {seq} after {prev}")
        prev = seq
    return problems


def check_lifecycle(frames: list[dict[str, Any]]) -> list[str]:
    """AG-UI lifecycle per thread: opens with RUN_STARTED, nothing after RUN_FINISHED/RUN_ERROR but RUN_STARTED,
    text/tool triads balanced, steps closed before finishing."""
    problems: list[str] = []
    open_run: dict[str, str | None] = {}
    open_msgs: dict[str, set[str]] = {}
    open_tools: dict[str, set[str]] = {}
    for f in frames:
        if "event" not in f:
            continue
        th, ev = f["threadId"], f["event"]
        t = ev["type"]
        if t == "RUN_STARTED":
            if open_run.get(th):
                problems.append(f"seq {f['seq']}: RUN_STARTED while a run is open on {th}")
            open_run[th] = ev["runId"]; open_msgs[th] = set(); open_tools[th] = set()
            continue
        if not open_run.get(th):
            problems.append(f"seq {f['seq']}: {t} outside a run on {th}")
            continue
        if t == "TEXT_MESSAGE_START":
            open_msgs[th].add(ev["messageId"])
        elif t in ("TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END") and ev["messageId"] not in open_msgs[th]:
            problems.append(f"seq {f['seq']}: {t} for unopened message")
        if t == "TEXT_MESSAGE_END":
            open_msgs[th].discard(ev["messageId"])
        if t == "TOOL_CALL_START":
            open_tools[th].add(ev["toolCallId"])
        elif t in ("TOOL_CALL_ARGS", "TOOL_CALL_END") and ev["toolCallId"] not in open_tools[th]:
            problems.append(f"seq {f['seq']}: {t} for unopened tool call")
        if t == "TOOL_CALL_END":
            open_tools[th].discard(ev["toolCallId"])
        if t in ("RUN_FINISHED", "RUN_ERROR"):
            if open_msgs[th] or open_tools[th]:
                problems.append(f"seq {f['seq']}: run closed with open messages or tool calls")
            open_run[th] = None
    return problems
