"""Every error message Pisa writes is a full sentence: it starts with a capital letter (or a name) and ends
with '.', '?' or '!'. Checked statically over every PisaError / ProviderCallError call in the code."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "pisa"
_CALLS = {"PisaError", "ProviderCallError", "ConnectorError"}


def _message_args() -> list[tuple[str, ast.expr]]:
    found: list[tuple[str, ast.expr]] = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name not in _CALLS:
                continue
            index = 1 if name in ("PisaError", "ConnectorError") else 0
            if len(node.args) > index:
                found.append((f"{path.relative_to(ROOT.parent)}:{node.lineno}", node.args[index]))
    return found


def _ends(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.JoinedStr) and expr.values:
        last = expr.values[-1]
        return last.value if isinstance(last, ast.Constant) and isinstance(last.value, str) else ""
    return None  # a variable: checked where it is built


def _starts(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.JoinedStr) and expr.values:
        first = expr.values[0]
        return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
    return None


MESSAGES = _message_args()


def test_there_are_messages_to_check() -> None:
    assert len(MESSAGES) > 40


@pytest.mark.parametrize(("where", "expr"), MESSAGES, ids=[w for w, _ in MESSAGES])
def test_error_message_is_a_sentence(where: str, expr: ast.expr) -> None:
    end, start = _ends(expr), _starts(expr)
    if end is not None:
        assert end.rstrip().endswith((".", "?", "!")), f"{where}: message must end with a full stop"
    if start:
        assert start[0].isupper() or start[0].isdigit() or start[0] in "'\"`", (
            f"{where}: start with a capital"
        )
