"""Where the contract files are: inside the installed wheel (``_data/``), or the source tree ``protocol/v2/``.

``PISA_PROTOCOL_V2`` overrides both (a checkout of ``protocol/v2`` to test against)."""
from __future__ import annotations

import os
from pathlib import Path

_PKG = Path(__file__).resolve().parent
_SOURCE = _PKG.parents[2]  # src/pisa_conformance -> src -> conformance -> protocol/v2


def _is_root(p: Path) -> bool:
    return (p / "schema" / "pisa-contract-2.schema.json").is_file()


def data_root() -> Path:
    env = os.environ.get("PISA_PROTOCOL_V2")
    if env:
        p = Path(env).resolve()
        if not _is_root(p):
            raise FileNotFoundError(f"PISA_PROTOCOL_V2={env} holds no schema/pisa-contract-2.schema.json")
        return p
    for p in (_SOURCE, _PKG / "_data"):
        if _is_root(p):
            return p
    raise FileNotFoundError("protocol/v2 data not found: reinstall pisa-conformance or set PISA_PROTOCOL_V2")


def source_root() -> Path | None:
    """protocol/v2 in a git checkout (None when running from an installed wheel only)."""
    return _SOURCE if _is_root(_SOURCE) and (_SOURCE / "tools").is_dir() else None


ROOT = data_root()
SCHEMA = ROOT / "schema" / "pisa-contract-2.schema.json"
CATALOG = ROOT / "catalog" / "pisa-core.catalog.v1.json"
VENDOR = ROOT / "vendor"
FIXTURES = ROOT / "fixtures"
EXAMPLES = ROOT / "examples"
