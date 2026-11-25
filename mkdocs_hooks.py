"""MkDocs hook for the documentation site (mkdocs.yml, `hooks:`).

The pages in docs-site/ link to files elsewhere in the repository (`../protocol/openapi.json`,
`../deploy/.env.example`), so the links work when the pages are read on GitHub. The built site holds only
docs-site/, so this hook points those links at the file in the repository: `repo_url` (the workflow sets it from
the repository's own owner and name) + `/blob/main/<path>`. In a local build with no `repo_url`, such a link
becomes its plain text. Links inside docs-site/ are left alone, for MkDocs to check.
"""

import posixpath
import re

LINK = re.compile(r"\[([^\]\n]+)\]\((\.\./[^)\s#]+)(#[^)\s]*)?\)")


def on_page_markdown(markdown, page, config, files):
    base = posixpath.dirname(page.file.src_uri)
    repo = (config.get("repo_url") or "").rstrip("/")

    def outside(m: re.Match) -> str:
        target = posixpath.normpath(posixpath.join("docs-site", base, m.group(2)))
        if target.startswith(("docs-site/", "../")):
            return m.group(0)
        if not repo:
            return m.group(1)
        return f"[{m.group(1)}]({repo}/blob/main/{target}{m.group(3) or ''})"

    return LINK.sub(outside, markdown)
