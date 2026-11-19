"""Contract X: MetricKit diagnostics from the phone, call stacks and exception types only, kept 30 days."""

import json
from pathlib import Path

from pisa.domain.diagnostics import sanitize, summary
from tests.conftest import pair

CRASH = json.loads((Path(__file__).parent / "data" / "metrickit" / "crash.json").read_text())


def body(payload: object = CRASH, kind: str = "crash") -> dict[str, object]:
    return {"kind": kind, "app_version": "0.1.0 (4)", "os_version": "26.3", "payload": payload}


def test_only_call_stacks_and_exception_types_are_kept() -> None:
    kept = sanitize(CRASH)
    text = json.dumps(kept)
    for personal in (
        "Pranzo con Dario",
        "chiama Marta",
        "020 7946 0123",
        "regionFormat",
        "signpostData",
        "pid",
    ):
        assert personal not in text, personal
    meta = kept["crashDiagnostics"][0]["diagnosticMetaData"]
    assert meta["exceptionReason"] == {
        "exceptionName": "NSInternalInconsistencyException",
        "className": "AVAudioEngine",
    }
    assert (meta["exceptionType"], meta["signal"], meta["appVersion"]) == (6, 5, "0.1.0")
    assert summary(kept) == (
        "EXC_BREAKPOINT (SIGTRAP) NSInternalInconsistencyException",
        ["Pisa +0x1a2b3c", "Pisa +0x1a2750", "AVFAudio +0x140"],  # the attributed thread, callee first
    )


def test_the_endpoint_stores_it(client, db) -> None:  # type: ignore[no-untyped-def]
    device, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/v1/devices/me/diagnostics", headers=auth, json=body()).status_code == 204
    row = db.execute(
        "SELECT device_id::text, kind, app_version, os_version, exception_type, top_frames, payload::text "
        "FROM device_diagnostics"
    ).fetchone()
    assert row is not None and row[:6] == (
        device, "crash", "0.1.0 (4)", "26.3", "EXC_BREAKPOINT (SIGTRAP) NSInternalInconsistencyException",
        ["Pisa +0x1a2b3c", "Pisa +0x1a2750", "AVFAudio +0x140"],
    )  # fmt: skip
    assert "chiama Marta" not in row[6]
    assert client.post("/v1/devices/me/diagnostics", json=body()).status_code == 401
    wrong = client.post("/v1/devices/me/diagnostics", headers=auth, json=body(kind="memory"))
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "validation_failed"


def test_a_payload_over_1_mb_is_refused(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    big = {"crashDiagnostics": [], "padding": "x" * 1_000_001}
    response = client.post("/v1/devices/me/diagnostics", headers=auth, json=body(big))
    assert response.status_code == 400
    assert (
        response.json()["error"]["message"] == "The diagnostic payload is 1.0 MB; at most 1 MB is accepted."
    )
    assert db.execute("SELECT count(*) FROM device_diagnostics").fetchone() == (0,)


# --- review fixes ------------------------------------------------------------------------------------------


def frames(*names: str) -> list[dict[str, object]]:
    """A call stack, callee first: each caller nested under its callee, as MetricKit sends it."""
    root: list[dict[str, object]] = []
    level = root
    for i, name in enumerate(names):
        frame: dict[str, object] = {"binaryName": name, "offsetIntoBinaryTextSegment": 0x100 + i,
                                    "binaryUUID": "0B0C", "subFrames": []}  # fmt: skip
        level.append(frame)
        sub: list[dict[str, object]] = []
        frame["subFrames"] = sub
        level = sub
    return root


def bare(stack: list[dict[str, object]]) -> dict[str, object]:
    """One MXDiagnostic, as the phone sends it (not wrapped in a payload)."""
    return {
        "version": "1.0.0",
        "diagnosticMetaData": {"exceptionType": 10, "signal": 6, "appVersion": "0.1.0",
                               "exceptionReason": {"exceptionName": "NSRangeException",
                                                   "composedMessage": "index 3 beyond chiama Marta"}},
        "callStackTree": {"callStackPerThread": True,
                          "callStacks": [{"threadAttributed": True, "callStackRootFrames": stack}]},
    }  # fmt: skip


def test_a_bare_diagnostic_is_kept_with_the_apps_frames(client, db) -> None:  # type: ignore[no-untyped-def]
    """H5: the phone sends one MXDiagnostic; the top 5 frames of an NSException are all system frames, so the
    first 3 frames of Pisa's own binary come too."""
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    stack = frames("libsystem_kernel.dylib", "libsystem_c.dylib", "libc++abi.dylib", "libobjc.A.dylib",
                   "CoreFoundation", "CoreFoundation", "Pisa", "SwiftUI", "Pisa", "Pisa", "Pisa")  # fmt: skip
    assert client.post("/v1/devices/me/diagnostics", headers=auth, json=body(bare(stack))).status_code == 204
    exception, top, payload = db.execute(
        "SELECT exception_type, top_frames, payload::text FROM device_diagnostics"
    ).fetchone()  # type: ignore[misc]
    assert exception == "EXC_CRASH (SIGABRT) NSRangeException"
    assert top == ["libsystem_kernel.dylib +0x100", "libsystem_c.dylib +0x101", "libc++abi.dylib +0x102",
                   "libobjc.A.dylib +0x103", "CoreFoundation +0x104", "Pisa +0x106", "Pisa +0x108",
                   "Pisa +0x109"]  # fmt: skip
    assert "chiama Marta" not in payload and json.loads(payload)["crashDiagnostics"][0]["callStackTree"]


def test_a_deep_stack_does_not_recurse() -> None:
    """LOW: a runaway recursion's stack (thousands of frames) is cut at FRAME_DEPTH_MAX, without recursion."""
    from pisa.domain.diagnostics import FRAME_DEPTH_MAX

    kept = sanitize(bare(frames(*(["Pisa"] * 5000))), "crash")
    depth, level = 0, kept["crashDiagnostics"][0]["callStackTree"]["callStacks"][0]["callStackRootFrames"]
    while level:
        depth += 1
        level = level[0].get("subFrames") or []
    assert depth == FRAME_DEPTH_MAX


def test_the_summary_is_the_crash_not_the_first_key() -> None:
    """LOW: a payload with a launch diagnostic and a crash reports the crash."""
    launch = bare(frames("dyld")) | {"diagnosticMetaData": {"launchDuration": 3.2}}
    payload = {"appLaunchDiagnostics": [launch], "crashDiagnostics": [bare(frames("Pisa"))]}
    assert summary(sanitize(payload)) == ("EXC_CRASH (SIGABRT) NSRangeException", ["Pisa +0x100"])
    assert summary(sanitize(payload), "launch")[1] == ["dyld +0x100"]


def test_an_upload_over_the_limit_is_refused_before_parsing(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"} | {"Content-Type": "application/json"}
    response = client.post("/v1/devices/me/diagnostics", headers=auth, content=b"[" * 2_000_000)
    assert response.status_code == 400
    assert response.json()["error"]["message"] == (
        "The diagnostic upload is over 1 MB; at most 1 MB is accepted."
    )
