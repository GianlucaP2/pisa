"""A stop runs the hooks the runtime registered, once, and a failing one never blocks it."""

from tests.test_ledger_and_autonomy import rt


def test_a_stop_runs_its_hooks_once_and_a_failing_hook_never_blocks_it(client) -> None:  # type: ignore[no-untyped-def]
    stops = rt(client).autopilot.stops
    assert stops.on_stop == []  # Runtime.start registers none today
    called: list[str] = []

    async def broken() -> None:
        raise RuntimeError("runner down")

    async def fake() -> None:
        called.append("stop_all")

    stops.on_stop[:] = [broken, fake]
    assert client.portal.call(stops.stop, "app")["stopped"] is True
    client.portal.call(stops.stop, "voice")  # already stopped: the hooks don't run again
    assert called == ["stop_all"]
