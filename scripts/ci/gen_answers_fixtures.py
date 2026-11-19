"""Write protocol/fixtures/proposed-answers/ from CORE's
own builders, so the phone builds against what CORE sends. Synthetic places, events and offers.

    cd core && uv run python ../scripts/ci/gen_answers_fixtures.py
"""

import json
from pathlib import Path
from typing import Any

from pisa.domain import answers, visual
from pisa.domain.sheet import SheetData

ANSWERS = frozenset({"findings", "map", "product"})
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "protocol" / "fixtures" / "proposed-answers"


def message(n: int, data: dict[str, Any], purpose: str) -> dict[str, Any]:
    SheetData.model_validate(data)
    return {
        "v": 1, "id": f"00000000-0000-4000-8000-0000000097{n:02d}", "ts": "2026-11-14T18:00:00+01:00",
        "type": "present", "present_id": f"6e3c9d2f-8b54-4a1f-9b2c-3d4e5f6a7d{n:02d}", "reply_to": None,
        "object": {"object_id": f"sheet:4a2d3c0b-7f6e-4d8c-9ba2-1c3d4e5f6c{n:02d}", "kind": "sheet",
                   "revision": 1763139600000 + n, "data": data},
        "purpose": purpose, "focus": "take", "lang": "it-IT",
    }  # fmt: skip


def places() -> dict[str, Any]:
    rows = [
        {"place_id": "g:fittizia", "name": "Fittizia", "why": "A due passi, tranquillo; la pizza che ami", "walk": "6 min",
         "rating": "4.6 (1203)", "open": "aperto, fino alle 23:30", "price": "€€",
         "cited_by": [{"host": "gamberorosso.it"}, {"host": "puntarellarossa.it"}],
         "recognition": [{"guide": "Gambero Rosso", "award": "Tre Spicchi", "year": 2027,
                          "url": "https://www.gamberorosso.it/pizzerie/fittizia"}],
         "pros": ["Impasto leggero, forno a legna"], "cons": ["Si aspetta senza prenotazione"]},
        {"place_id": "g:nonna-finta", "name": "Nonna Finta", "why": "Cena seduta, cucina d'autore", "walk": "14 min",
         "rating": "4.7 (850)", "open": "aperto, fino alle 23:00", "price": "€€€€",
         "cited_by": [{"host": "guide.michelin.com"}],
         "recognition": [{"guide": "Michelin", "award": "★", "year": 2026,
                          "url": "https://guide.michelin.com/it/it/lazio/roma/ristorante/nonna-finta"}]},
        {"place_id": "g:per-finta", "name": "Per Finta", "why": "Elegante, menu degustazione", "walk": "18 min",
         "rating": "4.5 (640)", "open": "chiuso, apre alle 19:30", "price": "€€€€"},
    ]  # fmt: skip
    facts = {
        "g:fittizia": {"lat": 41.859, "lon": 12.468, "open_now": True},
        "g:nonna-finta": {"lat": 41.863, "lon": 12.467, "open_now": True},
        "g:per-finta": {"lat": 41.852, "lon": 12.468, "open_now": False},
    }
    slots = {"g:nonna-finta": {"day": "2026-11-14", "times": ["20:00", "20:30", "21:15"],
                       "url": "https://www.thefork.it/ristorante/nonna-finta-r123",
                       "source": "https://www.thefork.it/ristorante/nonna-finta-r123"}}  # fmt: skip
    tables = visual.findings_fallback(answers.place_blocks(rows, facts, slots)[-1], "it-IT")[:2]
    return {
        "title": "Cena stasera vicino a te",
        "blocks": [{"type": "section", "title": "Cosa conta"}, {"type": "chips", "items": ["tranquillo", "seduti"]},
                   *answers.place_blocks(rows, facts, slots), *tables],
        "sources": [{"title": "Gambero Rosso: Fittizia", "url": "https://www.gamberorosso.it/pizzerie/fittizia",
                     "host": "gamberorosso.it"}],
        "lang": "it-IT",
        "excluded": [{"name": "Trattoria Inesistente", "why": "chiuso il sabato sera", "source": "map"}],
    }  # fmt: skip


def events() -> dict[str, Any]:
    url = "https://www.auditorium.com/it/eventi"
    rows = answers.event_rows([
        {"name": "Coro dei bambini", "at": "2026-11-14T18:00", "venue": "Sala Petrassi", "price": "8 €",
         "age": "dai 3 anni", "url": url, "lat": 41.824, "lon": 12.444},
        {"name": "Franco Inventato", "at": "2026-11-14T21:00", "venue": "Sala Santa Cecilia",
         "price": "da 35 €", "url": url, "lat": 41.823, "lon": 12.445},
        {"name": "La bottega del caffè", "at": "2026-11-14T20:30", "venue": "Teatro Inventato", "price": "da 18 €",
         "url": "https://www.teatro-inventato.example/programma", "lat": 41.853, "lon": 12.46},
    ])  # fmt: skip
    pins = answers.map_block(rows)
    assert pins is not None
    return {"title": "Sabato a Roma", "blocks": [pins, {"type": "findings", "rows": rows}], "lang": "it-IT"}


def products() -> dict[str, Any]:
    src = "https://www.trovaprezzi.it/cuffie/prezzi-scheda-prodotto/sony_wh_1000xm5"
    offers = [
        {"shop": "Bpm Power", "price": 185.85, "shipping": 4.0, "low_6m": 164.97, "source": src},
        {"shop": "Amazon", "price": 199.0, "shipping": 0.0, "low_6m": 164.97, "source": src,
         "url": "https://www.amazon.it/dp/B09XS7JWHH"},
    ]  # fmt: skip
    blocks = [answers.product_block(n, o, "Sony WH-1000XM5", "https://images.example-cdn.it/xm5.jpg" if n == 1 else None)
              for n, o in enumerate(offers, start=1)]  # fmt: skip
    return {"title": "Cuffie ANC sotto 200 €", "blocks": blocks, "lang": "it-IT"}


def hello() -> dict[str, Any]:
    return {
        "v": 1, "id": "00000000-0000-4000-8000-000000009790", "ts": "2026-11-14T18:00:00+01:00", "type": "hello",
        "device_id": "00000000-0000-4000-8000-000000000100", "app_version": "0.1.0", "platform": "ios",
        "capabilities": {"mic": True, "speaker": True, "display": True, "camera": True, "local_stt": True,
                         "local_tts": True, "location": True, "push": True,
                         "present": {"kinds": ["places", "place", "sheet"], "focus": True,
                                     "blocks": ["diagram", "chart", "timeline", "findings", "map", "product"]}},
    }  # fmt: skip


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    drawn = visual.answers_for_phone(places(), ANSWERS, "it-IT")  # what a phone that lists the blocks gets
    files = {
        "present_sheet_findings": message(1, drawn, "Dove cenare stasera"),
        "present_sheet_fallback": message(
            1, visual.answers_for_phone(drawn, None, "it-IT"), "Dove cenare stasera"
        ),
        "present_sheet_events": message(2, events(), "Cosa c'è sabato"),
        "present_sheet_products": message(3, products(), "Dove al prezzo migliore"),
        "hello_present_blocks": hello(),
    }
    for name, body in files.items():
        (OUT / f"{name}.json").write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
