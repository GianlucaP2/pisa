"""Diagnostics from the phone: MetricKit payloads (`MXDiagnosticPayload.jsonRepresentation()`),
kept so a crash reaches the developers without the device. Pure functions, no I/O.

A payload keeps **call stacks and exception types only**: every key outside the lists below is dropped,
whatever it holds, and so is any text longer than a short system value (an exception's composed message can
quote personal text). Frames are not symbolicated here: binary name, UUID and offset are enough for the
developers, with the build's dSYM.
"""

import json
from typing import Any

PAYLOAD_MAX_BYTES = 1_000_000  # ≤ 1 MB
TOP_FRAMES = 5
APP_FRAMES = 3  # plus the first frames in Pisa's own binary (an NSException's top 5 are all system frames)
APP_BINARY = "Pisa"  # the app's executable
FRAME_DEPTH_MAX = 512  # a deeper stack (runaway recursion) is cut there: the top is what matters
_TEXT_MAX = 120  # longer strings are not a system value: dropped

# The payload's lists, in the order the report looks at them (a crash first).
_DIAGNOSTICS_BY_KIND = {
    "crash": "crashDiagnostics",
    "hang": "hangDiagnostics",
    "cpu": "cpuExceptionDiagnostics",
    "disk": "diskWriteExceptionDiagnostics",
    "launch": "appLaunchDiagnostics",
}
_DIAGNOSTICS = set(_DIAGNOSTICS_BY_KIND.values())
_TOP = _DIAGNOSTICS | {"timeStampBegin", "timeStampEnd"}
_DIAGNOSTIC = {"callStackTree", "diagnosticMetaData", "version"}
_TREE = {"callStacks", "callStackPerThread"}
_STACK = {"threadAttributed", "callStackRootFrames"}
_FRAME = {"binaryUUID", "offsetIntoBinaryTextSegment", "sampleCount", "binaryName", "address", "subFrames"}
_META = {
    "appBuildVersion", "appVersion", "osVersion", "deviceType", "platformArchitecture", "bundleIdentifier",
    "exceptionType", "exceptionCode", "signal", "terminationReason", "hangDuration", "totalCPUTime",
    "totalSampledTime", "writesCaused", "launchDuration", "isTestFlightApp", "lowPowerModeEnabled",
    "exceptionReason",
}  # fmt: skip
_REASON = {"exceptionType", "exceptionName", "className"}  # never composedMessage, formatString, arguments

_EXC = {1: "EXC_BAD_ACCESS", 2: "EXC_BAD_INSTRUCTION", 3: "EXC_ARITHMETIC", 4: "EXC_EMULATION",
        5: "EXC_SOFTWARE", 6: "EXC_BREAKPOINT", 10: "EXC_CRASH", 11: "EXC_RESOURCE",
        12: "EXC_GUARD"}  # fmt: skip
_SIGNAL = {4: "SIGILL", 5: "SIGTRAP", 6: "SIGABRT", 8: "SIGFPE", 9: "SIGKILL", 10: "SIGBUS", 11: "SIGSEGV",
           13: "SIGPIPE"}  # fmt: skip


def payload_size(payload: Any) -> int:
    return len(json.dumps(payload, separators=(",", ":")).encode())


def _scalar(value: Any) -> Any:
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, str) and len(value) <= _TEXT_MAX:
        return value
    return None


def _keep(value: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if k in allowed}


def _frame(root: Any) -> dict[str, Any]:
    """One frame and its callers (MetricKit nests each caller under its callee), without recursion: at most
    FRAME_DEPTH_MAX levels."""

    def one(value: Any) -> dict[str, Any]:
        frame = {k: _scalar(v) for k, v in _keep(value, _FRAME - {"subFrames"}).items()}
        return {k: v for k, v in frame.items() if v is not None}

    top = one(root)
    pending = [(root, top, 1)]
    while pending:
        value, out, depth = pending.pop()
        subs = value.get("subFrames") if isinstance(value, dict) else None
        if not isinstance(subs, list) or depth >= FRAME_DEPTH_MAX:
            continue
        out["subFrames"] = []
        for sub in subs:
            kept = one(sub)
            out["subFrames"].append(kept)
            pending.append((sub, kept, depth + 1))
    return top


def _meta(value: Any) -> dict[str, Any]:
    out = {k: _scalar(v) for k, v in _keep(value, _META - {"exceptionReason"}).items()}
    reason = value.get("exceptionReason") if isinstance(value, dict) else None
    if isinstance(reason, dict):
        out["exceptionReason"] = {k: _scalar(v) for k, v in _keep(reason, _REASON).items()}
    return {k: v for k, v in out.items() if v is not None}


def sanitize(payload: Any, kind: str = "crash") -> dict[str, Any]:
    """The payload with call stacks and exception types only (see the module docstring). A bare diagnostic
    (one `MXDiagnostic`: `callStackTree` and `diagnosticMetaData` at the top) is read as a payload holding
    just it, under its kind's list."""
    if isinstance(payload, dict) and ("callStackTree" in payload or "diagnosticMetaData" in payload):
        payload = {_DIAGNOSTICS_BY_KIND.get(kind, "crashDiagnostics"): [payload]}
    out: dict[str, Any] = {}
    for key, value in _keep(payload, _TOP).items():
        if key not in _DIAGNOSTICS:
            if (kept := _scalar(value)) is not None:
                out[key] = kept
            continue
        items = []
        for diagnostic in value if isinstance(value, list) else []:
            d = _keep(diagnostic, _DIAGNOSTIC)
            item: dict[str, Any] = {"diagnosticMetaData": _meta(d.get("diagnosticMetaData"))}
            tree = _keep(d.get("callStackTree"), _TREE)
            item["callStackTree"] = {
                "callStackPerThread": bool(tree.get("callStackPerThread")),
                "callStacks": [
                    {
                        "threadAttributed": bool(stack.get("threadAttributed")),
                        "callStackRootFrames": [_frame(f) for f in stack.get("callStackRootFrames") or []],
                    }
                    for stack in (_keep(s, _STACK) for s in tree.get("callStacks") or [])
                ],
            }
            items.append(item)
        out[key] = items
    return out


def summary(payload: dict[str, Any], kind: str = "crash") -> tuple[str | None, list[str]]:
    """(the exception type, the frames to show) of the diagnostic that matters, for the report: the upload's
    kind first, then a crash, a hang… The frames are the attributed thread's top TOP_FRAMES, plus its first
    APP_FRAMES in Pisa's own binary when those are deeper: "EXC_BREAKPOINT (SIGTRAP)", ["libswiftCore.dylib
    +0x4d5e", …, "Pisa +0x1a2b3c"]."""
    order = [_DIAGNOSTICS_BY_KIND[kind]] if kind in _DIAGNOSTICS_BY_KIND else []
    order += [key for key in _DIAGNOSTICS_BY_KIND.values() if key not in order]
    first = next((d for key in order for d in payload.get(key) or []), None)
    if first is None:
        return None, []
    meta = first.get("diagnosticMetaData") or {}
    parts = []
    if (exc := meta.get("exceptionType")) is not None:
        parts.append(_EXC.get(exc, f"exception {exc}"))
    if (sig := meta.get("signal")) is not None:
        parts.append(f"({_SIGNAL.get(sig, f'signal {sig}')})")
    if name := (meta.get("exceptionReason") or {}).get("exceptionName"):
        parts.append(str(name))
    stacks = (first.get("callStackTree") or {}).get("callStacks") or []
    stack = next((s for s in stacks if s.get("threadAttributed")), stacks[0] if stacks else None)
    frames: list[str] = []
    app = 0
    level = (stack or {}).get("callStackRootFrames") or []
    while level and (len(frames) < TOP_FRAMES or app < APP_FRAMES):  # each caller is under its callee
        frame = level[0]
        mine = frame.get("binaryName") == APP_BINARY
        if len(frames) < TOP_FRAMES or mine:
            offset = frame.get("offsetIntoBinaryTextSegment")
            where = hex(offset) if isinstance(offset, int) else "?"
            frames.append(f"{frame.get('binaryName', '?')} +{where}")
        app += mine
        level = frame.get("subFrames") or []
    return (" ".join(parts) or None), frames
