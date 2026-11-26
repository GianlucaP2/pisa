"""Item 3: the test-time checker for the one path the `Personal` type can't follow at run
time, an f-string (Python builds a plain `str` from it, so a door would see an unmarked string).

Two halves, both over `DOOR_MODULES`:
- **at run time** (this plugin, loaded by `conftest.py`): `taint.watch` makes an f-string or a
  `str.format` over a `Personal` raise `PersonalInFString` in a door module, in every test that runs one;
- **statically** (`fstring_findings`, run by `tests/test_taint.py`): an f-string in a door module whose
  operand is a name annotated `Personal` (a parameter or an annotated assignment in the same function), a
  call that makes one (`Personal(...)`, `taint.Personal(...)`, `personal(...)`) or a function whose return
  annotation names `Personal` (`list[Personal]` too), a name assigned from such a call (awaited or not), and
  a loop or comprehension variable over such a call or name. It reads names only: a value that leaves the
  type at run time without an f-string (`!r`, `str.upper(p)`, `"".join`, `str.__add__`) is not seen.
"""

import ast
import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest

from pisa.domain import taint

# The modules whose f-strings may not take a `Personal`: every module that builds or sends a door's text.
DOOR_MODULES = frozenset(
    {
        "pisa.brain.research_tools",
        "pisa.connectors.apple_maps",
        "pisa.connectors.google_maps",
        "pisa.connectors.tavily",
        "pisa.connectors.web_reader",
        "pisa.services.curious",
        "pisa.services.desk",
        "pisa.services.ideas",
        "pisa.services.night",
        "pisa.services.place_providers",
        "pisa.services.voice4",
        "pisa.services.voice6",
    }
)


def pytest_configure(config: pytest.Config) -> None:
    taint.watch(DOOR_MODULES)


def pytest_unconfigure(config: pytest.Config) -> None:
    taint.watch(())


_MAKERS = {"Personal", "personal"}


def _names_personal(annotation: ast.expr | None) -> bool:
    return annotation is not None and "Personal" in ast.unparse(annotation)


def _callee(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _module_path(module: str) -> Path:
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin is not None, module
    return Path(spec.origin)


def _his(value: ast.expr, makers: set[str], marked: set[str]) -> bool:
    """A value that is his: a maker's call (awaited or not) or a name already marked."""
    if isinstance(value, ast.Await):
        value = value.value
    if isinstance(value, ast.Call):
        return _callee(value) in makers
    return isinstance(value, ast.Name) and value.id in marked


def fstring_findings(source: str, filename: str = "<door>") -> Iterator[str]:
    """Each f-string over a `Personal` operand in `source`, as `file:line: operand`."""
    tree = ast.parse(source, filename)
    returns_personal = {
        f.name
        for f in ast.walk(tree)
        if isinstance(f, ast.FunctionDef | ast.AsyncFunctionDef) and _names_personal(f.returns)
    }
    scopes: list[ast.AST] = [
        tree,
        *(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)),
    ]
    for scope in scopes:
        marked: set[str] = set()
        if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            args = scope.args
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if a is not None and _names_personal(a.annotation):
                    marked.add(a.arg)
        body = scope.body if isinstance(scope, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef) else []
        nodes = [n for stmt in body for n in ast.walk(stmt)]
        makers = _MAKERS | returns_personal

        grew = True
        while grew:  # until nothing new is marked: a loop over a list can come after the list's name
            before = len(marked)
            for node in nodes:
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    if _names_personal(node.annotation):
                        marked.add(node.target.id)
                elif isinstance(node, ast.Assign) and _his(node.value, makers, marked):
                    marked |= {t.id for t in node.targets if isinstance(t, ast.Name)}
                elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension) and _his(
                    node.iter, makers, marked
                ):
                    marked |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
            grew = len(marked) > before
        for node in nodes:
            if not isinstance(node, ast.JoinedStr):
                continue
            for part in node.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                for leaf in ast.walk(part.value):
                    hit = (isinstance(leaf, ast.Name) and leaf.id in marked) or (
                        isinstance(leaf, ast.Call) and _callee(leaf) in makers
                    )
                    if hit:
                        yield f"{filename}:{node.lineno}: {ast.unparse(part.value)}"
                        break


def door_findings() -> list[str]:
    """Every finding in the door modules (deduplicated: a nested function is walked by its parent too)."""
    found: list[str] = []
    for module in sorted(DOOR_MODULES):
        path = _module_path(module)
        found += fstring_findings(path.read_text(), str(path))
    return sorted(set(found))
