"""The contract-2 OpenAPI delta as one self-contained document, for Schemathesis (CONTRACT-2 §10, conformance item 1).

`openapi-v2-delta.json` refers to other files: the contract's schema, the vendored AG-UI 1.0 and A2UI 1.0 schemas
(by path, by `$id` and by `$anchor`), and v1's `openapi.json`. A tool that reads one document cannot follow those, so
`bundled()` copies each referenced document into `components/schemas` (as `x-bundled-<name>`) and rewrites every
`$ref` to a local JSON pointer. Nothing in the schemas changes but where their references point.

    from pisa_conformance.openapi import bundled
    import schemathesis
    schema = schemathesis.openapi.from_dict(bundled(operations={"getApproval", "resumeApproval"}))

"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin

from .paths import CATALOG, ROOT, SCHEMA, VENDOR
from .validate import AGUI_ID, CATALOG_PLACEHOLDER, EXT_ID

DELTA = ROOT / "openapi-v2-delta.json"
# v1's protocol/openapi.json: beside protocol/v2 in a checkout, under _data/v1 in the wheel
V1 = next((p for p in (ROOT.parent / "openapi.json", ROOT / "v1" / "openapi.json") if p.is_file()), None)


def _load(p: Path) -> dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def _documents() -> dict[str, tuple[str, dict[str, Any]]]:
    """uri -> (its name in the bundle, document)."""
    docs: dict[str, tuple[str, dict[str, Any]]] = {}
    for f in sorted((VENDOR / "a2ui-1.0").glob("*.json")):
        d = _load(f)
        docs[d["$id"]] = ("a2ui_" + f.stem.replace("-", "_"), d)
    docs[AGUI_ID] = ("agui", _load(VENDOR / "ag-ui-1.0" / "schema.json"))
    catalog = _load(CATALOG)
    docs[catalog["$id"]] = ("catalog", catalog)
    docs[CATALOG_PLACEHOLDER] = docs[catalog["$id"]]
    docs[EXT_ID] = ("pisa2", _load(SCHEMA))
    docs[SCHEMA.resolve().as_uri()] = docs[EXT_ID]
    docs[(VENDOR / "ag-ui-1.0" / "schema.json").resolve().as_uri()] = docs[AGUI_ID]
    if V1 is not None:   # the delta names it "../openapi.json", wherever the delta itself is
        docs[urljoin(DELTA.resolve().as_uri(), "../openapi.json")] = ("v1", _load(V1))
    return docs


def _anchors(node: Any, here: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(node, dict):
        if isinstance(node.get("$anchor"), str):
            out[node["$anchor"]] = here
        for k, v in node.items():
            out.update(_anchors(v, f"{here}/{_esc(k)}"))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.update(_anchors(v, f"{here}/{i}"))
    return out


def _esc(k: str) -> str:
    return k.replace("~", "~0").replace("/", "~1")


def bundled(operations: set[str] | None = None) -> dict[str, Any]:
    """The delta with every external reference made local. `operations`: keep only these operationIds."""
    docs = _documents()
    delta_uri = DELTA.resolve().as_uri()
    used: dict[str, dict[str, Any]] = {}
    anchors: dict[str, dict[str, str]] = {}

    def target(base: str, ref: str) -> str:
        uri, frag = (base, ref[1:]) if ref.startswith("#") else urldefrag(urljoin(base, ref))
        if uri in ("", base) and base == delta_uri:
            return "#" + frag
        if uri.startswith("https://json-schema.org/"):   # "a JSON Schema": any value, for this purpose
            used.setdefault("metaschema", {})
            return "#/components/schemas/x-bundled-metaschema"
        if uri not in docs:
            raise KeyError(f"{ref} (from {base}) names a document the kit does not carry")
        key, doc = docs[uri]
        if key not in used:
            used[key] = {}                       # reserved before the walk: references may be circular
            used[key] = walk(copy.deepcopy(doc), doc.get("$id") or uri, top=True)
        if frag and not frag.startswith("/"):
            anchors.setdefault(key, _anchors(doc))
            if frag not in anchors[key]:
                raise KeyError(f"no $anchor {frag} in {uri}")
            frag = anchors[key][frag]
        return f"#/components/schemas/x-bundled-{key}{frag}"

    def walk(node: Any, base: str, top: bool = False) -> Any:
        if isinstance(node, dict):
            if top:
                for k in ("$id", "$schema"):
                    node.pop(k, None)
            out = {}
            for k, v in node.items():
                if k == "$anchor":
                    continue
                if k == "$id" and isinstance(v, str):   # a nested resource changes the base below it
                    base = urljoin(base, v)
                    continue
                out[k] = target(base, v) if k == "$ref" and isinstance(v, str) else walk(v, base)
            return out
        if isinstance(node, list):
            return [walk(v, base) for v in node]
        return node

    delta = _load(DELTA)
    if operations is not None:
        paths = {}
        for p, ops in delta["paths"].items():
            kept = {m: o for m, o in ops.items() if not isinstance(o, dict) or o.get("operationId") in operations
                    or m == "parameters"}
            if any(isinstance(o, dict) and o.get("operationId") in operations for o in kept.values()):
                paths[p] = kept
        missing = operations - {o.get("operationId") for ops in paths.values() for o in ops.values()
                                if isinstance(o, dict)}
        if missing:
            raise KeyError("no such operations: " + ", ".join(sorted(missing)))
        delta["paths"] = paths
    out = walk(delta, delta_uri)
    out.setdefault("components", {}).setdefault("schemas", {}).update(
        {f"x-bundled-{k}": v for k, v in used.items()})
    return out
