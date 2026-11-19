"""The client guide's code blocks are its example files' code, byte for byte, so the guide cannot rot.

    python3 scripts/ci/check_client_guide.py          # exit 1 on any drift, naming the page and the block
    python3 scripts/ci/check_client_guide.py --fix    # rewrite the blocks from their files

A page (`docs-site/build-a-client/*.md`) embeds code with a marker line right before a fenced block:

    <!-- include: examples/pair.py#transcript -->
    ```python
    ...
    ```

`#transcript` is the region between `# [guide:transcript]` and `# [/guide:transcript]` in the file (dedented,
marker lines inside it left out); with no `#region`, the whole file. The path is relative to the page. Also:
every region an example marks is embedded somewhere, every `python` block is an include, and every relative link
on a page leads to a file that exists. Standard library only.
"""

import argparse
import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs-site" / "build-a-client"
INCLUDE = re.compile(r"^<!-- include: (?P<path>[^#\s]+)(?:#(?P<region>[\w-]+))? -->$")
MARK = re.compile(r"^\s*# \[(?P<end>/?)guide:(?P<region>[\w-]+)\]\s*$")
LINK = re.compile(r"\]\((?!https?:|#|mailto:)(?P<target>[^)#\s]+)(?:#[^)]*)?\)")


def regions(path: Path) -> dict[str, str]:
    """Every marked region of a file, dedented, with the marker lines inside it left out."""
    found: dict[str, list[str]] = {}
    open_: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        mark = MARK.match(line)
        if mark is None:
            for name in open_:
                found[name].append(line)
            continue
        name = mark["region"]
        if not mark["end"]:
            if name in found:
                raise SystemExit(f"{path}:{number}: region {name!r} marked twice")
            found[name], open_ = [], [*open_, name]
        elif name in open_:
            open_.remove(name)
        else:
            raise SystemExit(f"{path}:{number}: region {name!r} closed but never opened")
    if open_:
        raise SystemExit(f"{path}: region(s) {open_} never closed")
    return {name: textwrap.dedent("\n".join(lines)).strip("\n") + "\n" for name, lines in found.items()}


def check(fix: bool) -> list[str]:
    problems: list[str] = []
    used: set[tuple[Path, str]] = set()
    cache: dict[Path, dict[str, str]] = {}
    for page in sorted(GUIDE.glob("*.md")):
        lines = page.read_text(encoding="utf-8").splitlines(keepends=True)
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            out.append(line)
            i += 1
            include = INCLUDE.match(line.strip())
            if line.startswith("```python") and (len(out) < 2 or not INCLUDE.match(out[-2].strip())):
                problems.append(f"{page.name}:{i}: a python block that is not an include")
            if include is None:
                continue
            source = (page.parent / include["path"]).resolve()
            where = (
                f"{page.name}:{i} ({include['path']}{'#' + include['region'] if include['region'] else ''})"
            )
            if not source.is_file():
                problems.append(f"{where}: no such file")
                continue
            if include["region"]:
                found = cache.setdefault(source, regions(source))
                if include["region"] not in found:
                    problems.append(f"{where}: no region {include['region']!r} in the file")
                    continue
                code = found[include["region"]]
                used.add((source, include["region"]))
            else:
                code = source.read_text(encoding="utf-8")
                code = code if code.endswith("\n") else code + "\n"
            if i >= len(lines) or not lines[i].startswith("```"):
                problems.append(f"{where}: the include is not followed by a fenced block")
                continue
            fence = lines[i]
            end = next((j for j in range(i + 1, len(lines)) if lines[j].rstrip() == "```"), None)
            if end is None:
                problems.append(f"{where}: the fenced block never ends")
                continue
            if "".join(lines[i + 1 : end]) != code:
                if not fix:
                    problems.append(f"{where}: the block differs from the file (run with --fix)")
            out += [fence, code, "```\n"]
            i = end + 1
        for number, line in enumerate(lines, 1):
            for link in LINK.finditer(line):
                if not (page.parent / link["target"]).exists():
                    problems.append(f"{page.name}:{number}: the link {link['target']} leads nowhere")
        if fix:
            page.write_text("".join(out), encoding="utf-8")
    for source in sorted((GUIDE / "examples").glob("*")):
        if source.suffix in (".py", ".sh"):
            for region in regions(source):
                if (source.resolve(), region) not in used:
                    problems.append(f"examples/{source.name}#{region}: marked but in no page")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--fix", action="store_true", help="rewrite the blocks from their files")
    problems = check(parser.parse_args().fix)
    for problem in problems:
        print(problem)
    pages = len(list(GUIDE.glob("*.md")))
    print(f"client guide: {pages} pages, {'no drift' if not problems else f'{len(problems)} problems'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
