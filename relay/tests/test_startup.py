# SPDX-License-Identifier: AGPL-3.0-only
"""The relay refuses to start when its windows do not fit together, and its sweep outlives a
failure."""

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pisa_relay import app as relay_app
from pisa_relay.apns import Apns
from pisa_relay.storage import ENDED_KEEP_S
from tests.conftest import BUNDLE, NOW, Clock, FakeApns, p8_key, settings_for
from tests.pki import make_pki


def _app(tmp_path: Path, **changes: Any) -> FastAPI:
    clock, fake, pki = Clock(), FakeApns(), make_pki(NOW)
    settings = settings_for(tmp_path, **changes)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    apns = Apns(http, p8_key(), settings.apns_key_id, settings.apns_team_id, BUNDLE, clock=clock)
    return relay_app.create_app(settings, apns, root=pki.root, clock=clock)


def test_a_transaction_max_age_not_below_how_long_an_ended_one_is_kept_refuses_to_start(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="refunded transaction could register again"):
        _app(tmp_path, transaction_max_age_s=ENDED_KEEP_S)
    _app(tmp_path, transaction_max_age_s=ENDED_KEEP_S - 1)  # below it: starts


def test_accepted_signatures_kept_under_two_replay_windows_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(relay_app, "ACCEPTED_KEEP_S", 2 * relay_app.REPLAY_WINDOW_S - 1)
    with pytest.raises(RuntimeError, match="two replay windows"):
        _app(tmp_path)


def test_the_sweep_logs_a_failure_and_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(relay_app, "SWEEP_EVERY_S", 0.01)
    app = _app(tmp_path)
    calls: list[float] = []

    def sweep(now: float, grace_s: int) -> None:
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("a synthetic storage failure")

    app.state.storage.sweep = sweep
    with TestClient(app):
        deadline = time.monotonic() + 5
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    assert len(calls) >= 3  # it swept again after the failure
    assert any("the sweep failed" in r.getMessage() for r in caplog.records)
