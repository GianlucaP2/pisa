"""Schemathesis hooks for `api_fuzz.py` (loaded through SCHEMATHESIS_HOOKS).

- `no_server_fault` stands in for Schemathesis's `not_a_server_error`: every 5xx fails, except a 502 in the
  API's error envelope. 502 is the documented answer when a provider or connector upstream fails, and the
  fuzzed server is offline (`offline_serve.py`), so every upstream fails: a route that stores a made-up key
  and then uses it must answer 502, never 500. A 500 (an unhandled error), a 503 or a 504 always fails.
- `filter_case` drops a generated case whose path is another operation's concrete path: OpenAPI matches a
  concrete path before a template ("/v1/connectors/google" is `GET /v1/connectors/google`, never
  `GET /v1/connectors/{connector_id}` with the id `google`), so checking that answer against the template's
  schema would test nothing the API says.
"""

import json
from pathlib import Path
from typing import Any

import schemathesis

OPENAPI = Path(__file__).resolve().parents[2] / "protocol" / "openapi.json"
CONCRETE = {
    (method.upper(), path)
    for path, operations in json.loads(OPENAPI.read_text())["paths"].items()
    if "{" not in path
    for method in operations
}


@schemathesis.check
def no_server_fault(ctx: Any, response: Any, case: Any) -> None:
    if response.status_code < 500:
        return None
    if response.status_code == 502:
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and isinstance(error.get("code"), str) and error["code"] != "internal":
            return None  # an upstream failure, said as one (the server is offline)
    raise AssertionError(f"Server error {response.status_code}: {response.text[:300]}")


@schemathesis.hook
def filter_case(context: Any, case: Any) -> bool:
    if "{" not in case.path:
        return True
    return (case.method.upper(), case.formatted_path) not in CONCRETE
