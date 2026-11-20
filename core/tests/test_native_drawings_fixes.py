# ruff: noqa: E501
"""The reviewer's tests for the native drawings' fixes. Every word synthetic."""

import asyncio
import uuid
from typing import Any

import pytest

from pisa.brain.present_tools import sheet_blocks
from pisa.domain import visual
from pisa.domain.sheet import SheetData, iso
from tests.test_native_drawings import _diagram, _service, _sheet_show, _shown
from tests.virtual_time import run_talk


def _chart(
    ys: list[Any], xs: list[Any] | None = None, kind: str = "category", mark: str = "bar"
) -> dict[str, Any]:
    points = [{"x": xs[i] if xs else f"g{i}", "y": y} for i, y in enumerate(ys)]
    return {"type": "chart", "mark": mark, "x": {"label": "x", "kind": kind}, "y": {"label": "y"},
            "series": [{"name": "S", "points": points}]}  # fmt: skip


def test_a_tap_in_a_talk_is_never_joined_to_his_words() -> None:
    """HIGH: his dangling words ("sposta il dentista a") and a tap within the wait were one turn whose words
    held the label: the tap's rails (empty words, read-only) never applied."""
    from pisa.ports.talker import OwnerSpeaking, OwnerWords
    from pisa.services.talk import Opening, TalkScreen
    from tests.test_live_talk import _run, _until

    run, session, link, seen = _run()
    run.screen = TalkScreen(link, Opening().kinds | {"sheet"}, "it-IT", frozenset({"diagram"}))

    async def hook(object_id: str, element_id: str) -> str | None:
        return visual.tap_words("giovedì alle 17", "it-IT")

    run.hooks.asked = hook

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await run.screen.present(_sheet_show("v1", 1))  # type: ignore[union-attr]
        session.queue.put_nowait(OwnerSpeaking(True))
        session.queue.put_nowait(OwnerSpeaking(False))
        session.queue.put_nowait(OwnerWords("sposta il dentista a", final=True))
        await asyncio.sleep(0.2)
        link.messages.put_nowait({"type": "talk.ask", "id": "v1", "element_id": "n4"})
        await _until(lambda: len([s for s in seen if s[0] == "think"]) >= 2, within=6)
        run.end("owner")
        await task

    run_talk(go())
    thinks = [s[1] for s in seen if s[0] == "think"]
    assert thinks[0].startswith("sposta il dentista a") and "Sul disegno" not in thinks[0]
    assert thinks[1] == "Sul disegno: giovedì alle 17"
    # The tap's thought comes flagged (`origin: drawing`), their words' never
    assert [s for s in seen if s[0] in ("think", "origin")][1:] == [
        ("think", thinks[1]),
        ("origin", "drawing"),
    ]


def test_a_sheet_shown_before_the_talk_is_tapped_by_its_object_id() -> None:
    from pisa.services.talk import Opening, TalkScreen
    from tests.test_live_talk import _run, _until

    run, _, link, seen = _run()
    run.screen = TalkScreen(link, Opening().kinds | {"sheet"}, "it-IT", frozenset({"diagram"}))
    asked: list[tuple[str, str]] = []

    async def hook(object_id: str, element_id: str) -> str | None:
        asked.append((object_id, element_id))
        return visual.tap_words("Postgres", "it-IT")

    run.hooks.asked = hook

    async def go() -> None:
        task = asyncio.create_task(run.run())
        link.messages.put_nowait({"type": "talk.ask", "id": "sheet:before", "element_id": "n5"})
        await _until(lambda: any(s[0] == "think" for s in seen))
        run.end("owner")
        await task

    run_talk(go())
    assert asked == [("sheet:before", "n5")]


def test_two_quick_taps_keep_their_own_labels() -> None:
    service, device = _service(), uuid.uuid4()
    service.objects[device] = {"sheet:a": _shown()}
    first, _ = service.tap(device, "sheet:a", "n4")  # type: ignore[misc]
    second, _ = service.tap(device, "sheet:a", "n5")  # type: ignore[misc]
    assert service._taps[(device, first)].label == "Connection pool"
    assert service._taps[(device, second)].label == "Postgres"


@pytest.mark.parametrize("y", [1e30, 10**40, 1.234e-30, "123456789012345678901234567890"])
def test_a_huge_or_tiny_number_never_raises(y: Any) -> None:
    blocks: list[dict[str, Any]] = [_chart([y, 2, 3]), {"type": "stat", "label": "a", "value": y},
                                    {"type": "table", "columns": ["a"], "rows": [[y]]}]  # fmt: skip
    built, dropped = sheet_blocks(blocks, [], results=["1 2 3 " + "9" * 40], lang="it-IT")
    assert built == [] and len(dropped) >= 3


def test_a_category_s_number_traces_and_nothing_approval_like_on_an_axis() -> None:
    built, dropped = sheet_blocks(
        [_chart([1, 2, 3], xs=[99999, 88888, 77777])], [], results=["1 2 3"], lang="it-IT"
    )
    assert built == [] and "not in this turn's results" in dropped[0]
    approval = _chart([1, 2, 3], xs=["a", "Approva pagamento", "c"]) | {
        "y": {"label": "Conferma", "unit": "Paga"}
    }
    built, _ = sheet_blocks([approval], [], results=["1 2 3"], lang="it-IT")
    assert built == []
    plain = _chart([1, 2, 3]) | {"y": {"label": "Conferma", "unit": "Paga"}}
    (chart,), _ = sheet_blocks([plain], [], results=["1 2 3"], lang="it-IT")
    assert chart["y"] == {"label": "y"}


def test_a_drawing_from_what_pisa_knows_shows_no_quantity() -> None:
    block = _diagram()
    block["nodes"][0]["label"] = "App: 10.000 query/s"
    block["nodes"][1]["label"] = "Modello HTTP/2"
    built, dropped = sheet_blocks([block], [], results=[], lang="it-IT")
    labels = [n["label"] for n in built[0]["nodes"]]
    assert "App: 10.000 query/s" not in labels and "Modello HTTP/2" in labels
    assert any("number" in d for d in dropped)


def test_dates_are_the_one_iso_form_the_phone_reads_end_after_at_sorted_by_moment() -> None:
    assert iso("2026-11-10T09:30+01:00") == "2026-11-10T09:30:00+01:00"
    assert iso("2026-11-10T09:30:00.123456") == "2026-11-10T09:30:00"
    for odd in ("2026-W45-1", "20261110", "2026-314", "2026-11-10T09", "2026-13-01"):
        assert iso(odd) is None, odd
    items = [{"at": "2026-01-01T10:00+05:00", "label": "a"}, {"at": "2026-01-01T06:00Z", "label": "b"},
             {"at": "2026-01-02", "end": "2026-01-01", "label": "c"}, {"at": "2026-W01-1", "label": "d"}]  # fmt: skip
    (tl,), dropped = sheet_blocks([{"type": "timeline", "items": items}], [], results=[], lang="it-IT")
    assert [i["label"] for i in tl["items"]] == ["a", "b"] and len(dropped) == 2
    SheetData.model_validate({"title": "t", "blocks": [tl]})


def test_a_pie_never_has_a_negative_slice() -> None:
    built, dropped = sheet_blocks([_chart([5, -2, 3], mark="pie")], [], results=["5 -2 3"], lang="it-IT")
    assert built == [] and "negative" in dropped[-1]


def test_in_a_talk_his_own_words_drop_a_waiting_tap() -> None:
    from pisa.ports.talker import OwnerSpeaking, OwnerWords
    from tests.test_live_talk import _run, _until

    run, session, _, seen = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        run._pending_tap = ("v1", "n4")  # a tap that waited for Pisa's answer
        session.queue.put_nowait(OwnerSpeaking(True))
        session.queue.put_nowait(OwnerSpeaking(False))
        session.queue.put_nowait(OwnerWords("e domani che tempo fa?", final=True))
        await _until(lambda: any(s[0] == "think" for s in seen))
        await asyncio.sleep(0.2)
        run.end("owner")
        await task

    run_talk(go())
    assert not [w for w in session.whispers if w.id == "tap"]  # the stale tap was never answered
