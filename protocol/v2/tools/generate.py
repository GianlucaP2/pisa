"""Writes (or, with --check, verifies) every generated file of protocol/v2.

    python3 protocol/v2/tools/generate.py           # regenerate
    python3 protocol/v2/tools/generate.py --check   # exit 1 if a committed file differs from what the generators make

The JSON files are never edited by hand: edit the generator, run this, commit both. A frozen file
(protocol/v2/FROZEN.json) must not change at all; see the README there.
"""
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
V2 = TOOLS.parent


def dumps(doc: Any) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def outputs() -> dict[Path, str]:
    schema = runpy.run_path(str(TOOLS / "gen_schema.py"))["SCHEMA"]
    catalog = runpy.run_path(str(TOOLS / "gen_catalog.py"))["CATALOG"]
    asyncapi = runpy.run_path(str(TOOLS / "gen_asyncapi.py"))["build"](schema)
    openapi = runpy.run_path(str(TOOLS / "gen_openapi_delta.py"))["build"](schema)
    return {
        V2 / "schema" / "pisa-contract-2.schema.json": dumps(schema),
        V2 / "catalog" / "pisa-core.catalog.v1.json": dumps(catalog),
        V2 / "asyncapi-live.json": dumps(asyncapi),
        V2 / "openapi-v2-delta.json": dumps(openapi),
    }


def drift() -> list[str]:
    return [str(p.relative_to(V2)) for p, text in outputs().items()
            if not p.is_file() or p.read_text(encoding="utf-8") != text]


def main(argv: list[str]) -> int:
    if "--check" in argv:
        bad = drift()
        for b in bad:
            print(f"drift: {b} differs from its generator (run tools/generate.py)")
        return 1 if bad else 0
    for p, text in outputs().items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print("wrote", p.relative_to(V2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
