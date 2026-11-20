"""`pisa client call --data`: the body as given, from a file (`@path`) or from stdin (`@-`), so a key or
password never has to sit in the command line."""

import io

import pytest


def test_the_body_as_given_from_a_file_or_from_stdin(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from pisa.cli_pair import _body  # pyright: ignore[reportPrivateUsage]

    assert _body(None) is None
    assert _body('{"key": "x"}') == '{"key": "x"}'
    path = tmp_path / "body.json"
    path.write_text('{"key": "from-a-file"}\n', encoding="utf-8")
    assert _body(f"@{path}") == '{"key": "from-a-file"}'
    monkeypatch.setattr("sys.stdin", io.StringIO('{"key": "from-stdin"}\n'))
    assert _body("@-") == '{"key": "from-stdin"}'
