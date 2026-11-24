"""Generate protocol/openapi.json from the FastAPI app (REST contract B).

    cd core && uv run python ../scripts/ci/gen_openapi.py          # write
    cd core && uv run python ../scripts/ci/gen_openapi.py --check  # CI: fail when the committed file is stale
"""

import json
import sys
from pathlib import Path

from pisa.api.app import create_app
from pisa.settings import Settings

OUT = Path(__file__).resolve().parents[2] / "protocol" / "openapi.json"


def render() -> str:
    app = create_app(Settings(timezone="UTC", database_url="postgresql://unused@127.0.0.1:1/unused"))
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT} is out of date: run `uv run python ../scripts/ci/gen_openapi.py` in core/", file=sys.stderr)
            return 1
        print(f"{OUT.name} is up to date")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
