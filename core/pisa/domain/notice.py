"""`pisa.notice` (`$defs/s_pisa_notice`): one shape for the live socket and the relay's sealed push
(CONTRACT-2 §5.2, §9, §9.1). Pure: no I/O.

Built from the fields of a v1 `notify`, plus what the lock screen needs:
- `privacy`, what the notice touches: `finance` (the week's money card), `health` (the user's words name a
  health place or care), `people` (an offer about someone they love), `place` (a place, or a sensitive one),
  else `none`. The app, its notification extension and the watch show `genericText` for health and finance
  unless the user allowed full previews for that class; a `sensitive` notice is always generic;
- `genericText`: Pisa's own templated line for the kind, with no outside text ("A reminder from Pisa").
"""

from datetime import datetime
from typing import Any

from pisa.domain import locale
from pisa.domain.disclosure import touches_health
from pisa.domain.health import health_place

GENERIC: dict[str, dict[str, str]] = {
    "en": {
        "reminder": "A reminder from Pisa", "briefing": "Your briefing is ready",
        "replan": "Your plan changed",
        "alert": "An alert from Pisa", "info": "A note from Pisa", "check_in": "Pisa has a question",
        "done": "Pisa closed something for you", "turn_answer": "Pisa answered you",
        "weekly": "Your weekly check is ready", "ledger_week": "Your week in review is ready",
        "approval": "Pisa needs your answer",
    },
    "it": {
        "reminder": "Un promemoria da Pisa", "briefing": "Il briefing è pronto",
        "replan": "Il piano è cambiato",
        "alert": "Un avviso da Pisa", "info": "Una nota da Pisa", "check_in": "Pisa ha una domanda",
        "done": "Pisa ha chiuso una cosa per te", "turn_answer": "Pisa ti ha risposto",
        "weekly": "Il controllo della settimana è pronto",
        "ledger_week": "Il riepilogo della settimana è pronto",
        "approval": "Pisa aspetta una tua risposta",
    },
}  # fmt: skip


def generic_text(kind: str, lang: str) -> str:
    table = locale.pick(GENERIC, lang)
    return table.get(kind, table["info"])


def privacy(fields: dict[str, Any]) -> str:
    """The notice's privacy class from its v1 fields (the most private class it touches first)."""
    if fields.get("ledger_week"):
        return "finance"
    words = " ".join(str(fields.get(k) or "") for k in ("title", "text"))
    place = fields.get("place")
    if touches_health(words) or (place and health_place(str(place), None)):
        return "health"
    if fields.get("ritual") == "people":
        return "people"
    if place or fields.get("sensitive"):
        return "place"
    return "none"


def value(fields: dict[str, Any], default_lang: str) -> dict[str, Any]:
    """A v1 `notify`'s fields → `pisa.notice`."""
    kind: str = fields["kind"]
    refs: list[str] = []
    if fields.get("weekly"):
        kind = "weekly"
    if fields.get("ledger_week"):
        kind = "ledger_week"
        refs.append(f"ledger_week:{fields['ledger_week']}")
    if fields.get("turn_id"):
        kind = "turn_answer"
        refs.append(f"turn:{str(fields['turn_id']).lower()}")
    out: dict[str, Any] = {
        "noticeId": str(fields["notification_id"]).lower(),
        "kind": kind,
        "priority": fields["priority"],
        "lang": fields["lang"],
        "text": fields.get("text") or "-",
    }
    if fields.get("title"):
        out["title"] = fields["title"]
    if fields.get("place"):
        out["place"] = fields["place"]
    for key in ("urgency", "voice", "wrist", "sensitive"):
        if fields.get(key) is not None:
            out[key] = fields[key]
    if fields.get("check_in_id") and fields.get("choices"):
        out["checkIn"] = {
            "checkInId": str(fields["check_in_id"]).lower(),
            "choices": [{"id": c["id"], "label": c["label"]} for c in fields["choices"]],
        }
        if fields.get("expects_reply") is not None:
            out["checkIn"]["expectsReply"] = fields["expects_reply"]
    undo_until = fields.get("undo_until")
    if undo_until is not None:
        task_id, event_id = fields.get("task_id"), fields.get("event_id")
        target = f"task:{task_id}" if task_id else f"event:{event_id}"
        until = undo_until.isoformat() if isinstance(undo_until, datetime) else str(undo_until)
        out["undo"] = {"undoId": target[:200], "until": until}
    if fields.get("task_id"):
        refs.append(f"task:{str(fields['task_id']).lower()}")
    if fields.get("sheet_id"):
        refs.append(str(fields["sheet_id"]))
    if refs:
        out["refs"] = refs[:4]
    out["privacy"] = privacy(fields)
    out["genericText"] = generic_text(kind, fields.get("lang") or default_lang)
    return out
