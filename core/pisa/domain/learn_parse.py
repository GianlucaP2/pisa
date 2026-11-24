"""A source's text → its segments (paragraph, table, caption, equation, appendix), with section path,
label and, when known, page. Pure parsers, no I/O: arXiv's HTML (LaTeXML), LaTeX source, a PDF's text layer
(one string per page), Markdown, and a plain web page. Whatever the text says is data: parsing reads
its structure, never acts on its words.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from html import unescape
from html.parser import HTMLParser

from pisa.domain.learn import Seg, SegKind, page_of

_VOID = frozenset(
    {"br", "img", "hr", "meta", "link", "input", "col", "source", "wbr", "area", "base", "embed"}
)
_SKIP = frozenset({"script", "style", "nav", "header", "footer", "noscript", "svg", "button", "form"})
_SECTION = re.compile(r"\bltx_(?:section|subsection|subsubsection|appendix|paragraph)\b")
_ARXIV_IN = re.compile(r"(?:arxiv[:\s/]*|abs/)(\d{4}\.\d{4,5})", re.IGNORECASE)
_DOI_IN = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+[^\s\"<>.,;])")


def cite_keys(text: str) -> tuple[str, ...]:
    """The works a bibliography entry names: arXiv ids and DOIs (arXiv's own DOIs as arXiv ids)."""
    keys = [f"arxiv:{m}" for m in _ARXIV_IN.findall(text)]
    for doi in _DOI_IN.findall(text):
        a = re.match(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", doi, re.IGNORECASE)
        keys.append(f"arxiv:{a.group(1)}" if a else f"doi:{doi.lower()}")
    return tuple(dict.fromkeys(keys))


@dataclass
class Parsed:
    title: str | None = None
    segments: list[Seg] = field(default_factory=list)
    has_pages: bool = False


def _space(text: str) -> str:
    return " ".join(text.split())


# --- arXiv HTML (LaTeXML) -----------------------------------------------------------------------------------


@dataclass
class _Capture:
    kind: SegKind
    depth: int
    parts: list[str] = field(default_factory=list)
    caption: list[str] = field(default_factory=list)
    tag: list[str] = field(default_factory=list)
    cites: list[str] = field(default_factory=list)
    label: str | None = None


class _Ltx(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str]] = []  # (tag, class)
        self.sections: list[tuple[int, list[str], bool]] = []  # (stack depth, title parts, appendix)
        self.cap: _Capture | None = None
        self.in_caption = 0
        self.in_tag = 0
        self.in_title: list[str] | None = None
        self.doc_title: list[str] | None = None
        self.title = ""
        self.math = 0
        self.skip = 0
        self.bib: dict[str, list[str]] = {}
        self.bib_now: str | None = None
        self.out: list[tuple[SegKind, str, tuple[str, ...], str | None, tuple[str, ...]]] = []
        self.abstract = 0

    # the open section titles, as printed ("4 Experiments", "Appendix B Details")
    def path(self) -> tuple[str, ...]:
        return tuple(_space("".join(t)) for _, t, _ in self.sections if _space("".join(t)))

    def appendix(self) -> bool:
        return any(a for _, _, a in self.sections)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        cls = a.get("class", "")
        if tag in _VOID:
            if tag == "br" and self.cap is not None:
                self.cap.parts.append(" ")
            return
        self.stack.append((tag, cls))
        depth = len(self.stack)
        if self.skip or tag in _SKIP or ("ltx_bibliography" in cls and tag == "section"):
            if tag in _SKIP or tag == "section":
                self.skip = self.skip or depth
            if "ltx_bibliography" in cls:
                self.skip = 0
                self.bib_now = None
                self.stack[-1] = (tag, cls + " pisa_bib")
            return
        if tag == "li" and "ltx_bibitem" in cls:
            self.bib_now = a.get("id", "")
            self.bib[self.bib_now] = []
            return
        if self.bib_now is not None:
            return
        if tag == "math":
            alt = a.get("alttext", "")
            if self.cap is not None and alt:
                self.cap.parts.append(f" {alt} " if self.cap.kind != "equation" else alt)
                if self.in_caption:
                    self.cap.caption.append(f" {alt} ")
            if self.in_title is not None and alt:
                self.in_title.append(alt)
            self.math = self.math or depth
            return
        if self.math:
            return
        if tag == "section" and _SECTION.search(cls):
            self.sections.append((depth, [], "ltx_appendix" in cls or self.appendix()))
            return
        if tag == "h1" and "ltx_title_document" in cls:
            self.doc_title = []
            return
        if re.fullmatch(r"h[2-6]", tag) and "ltx_title" in cls:
            if "ltx_title_abstract" in cls:
                self.abstract = 1
                return
            if self.sections and not self.sections[-1][1]:
                self.in_title = self.sections[-1][1]
            return
        if tag == "div" and "ltx_abstract" in cls:
            self.abstract = depth
            return
        if self.cap is None:
            if tag == "figure" and "ltx_table" in cls:
                self.cap = _Capture("table", depth)
            elif tag == "figure" and "ltx_figure" in cls:
                self.cap = _Capture("caption", depth)
            elif tag == "table" and "ltx_equation" in cls:
                self.cap = _Capture("equation", depth)
            elif tag == "p" and "ltx_p" in cls:
                self.cap = _Capture("appendix" if self.appendix() else "paragraph", depth)
            return
        if tag == "figcaption":
            self.in_caption = depth
        elif tag == "span" and "ltx_tag" in cls:
            self.in_tag = depth
        elif tag == "a" and a.get("href", "").startswith("#bib."):
            self.cap.cites.append(a["href"][1:])

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID or not self.stack:
            return
        # close up to the matching tag (LaTeXML's HTML is well formed; this survives a stray end tag)
        if tag not in (t for t, _ in self.stack):
            return
        while self.stack:
            open_tag, cls = self.stack.pop()
            depth = len(self.stack) + 1
            self._closed(open_tag, cls, depth)
            if open_tag == tag:
                break

    def _closed(self, tag: str, cls: str, depth: int) -> None:
        if self.skip == depth:
            self.skip = 0
        if "pisa_bib" in cls:
            self.bib_now = None
        if tag == "li" and self.bib_now is not None and "ltx_bibitem" in cls:
            self.bib_now = None
        if self.math == depth:
            self.math = 0
        if self.in_caption == depth:
            self.in_caption = 0
        if self.in_tag == depth:
            self.in_tag = 0
            if self.cap is not None and self.cap.tag:
                tag_text = _space("".join(self.cap.tag)).rstrip(":").strip()
                if self.cap.kind == "equation" and tag_text:
                    self.cap.label = f"Eq. {tag_text}"
                elif tag_text and self.cap.label is None and self.cap.kind in ("table", "caption"):
                    self.cap.label = tag_text
                self.cap.tag = []
        if self.doc_title is not None and tag == "h1":
            self.title = _space("".join(self.doc_title))
            self.doc_title = None
        if self.in_title is not None and re.fullmatch(r"h[2-6]", tag):
            self.in_title = None
        if self.abstract == depth:
            self.abstract = 0
        if self.sections and self.sections[-1][0] == depth:
            self.sections.pop()
        if self.cap is not None and self.cap.depth == depth:
            self._emit(self.cap)
            self.cap = None
        if self.cap is not None and self.cap.kind == "table":
            if tag in ("td", "th"):
                self.cap.parts.append(" | ")
            elif tag == "tr":
                self.cap.parts.append("\n")

    def _emit(self, cap: _Capture) -> None:
        if cap.kind == "equation":
            text = _space("".join(cap.parts))
        elif cap.kind == "table":
            caption = _space("".join(cap.caption))
            rows = "\n".join(
                _space(r).strip(" |")
                for r in "".join(p for p in cap.parts).split("\n")
                if _space(r).strip(" |")
            )
            body = rows.replace(caption, "").strip() if caption else rows
            text = f"{caption}\n{body}".strip()
        elif cap.kind == "caption":
            text = _space("".join(cap.caption)) or _space("".join(cap.parts))
        else:
            text = _space("".join(cap.parts))
        if not text:
            return
        path = ("Abstract",) if self.abstract and not self.sections else self.path()
        self.out.append((cap.kind, text, path, cap.label, tuple(cap.cites)))

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.bib_now is not None:
            self.bib[self.bib_now].append(data)
            return
        if self.math:
            return
        if self.doc_title is not None:
            self.doc_title.append(data)
        if self.in_title is not None:
            self.in_title.append(data)
            return
        if self.cap is None:
            return
        self.cap.parts.append(data)
        if self.in_caption:
            self.cap.caption.append(data)
            if self.in_tag:
                self.cap.tag.append(data)
        elif self.in_tag and self.cap.kind == "equation":
            self.cap.tag.append(data)


def arxiv_html(html: str) -> Parsed:
    """arXiv's HTML (LaTeXML): sections and appendices as printed, paragraphs, tables with their caption and
    cells, figure captions, equations as LaTeX (their alt text), and each paragraph's citations resolved to
    arXiv ids or DOIs through the bibliography."""
    p = _Ltx()
    p.feed(html)
    p.close()
    bib = {k: cite_keys(" ".join(v)) for k, v in p.bib.items()}
    segs: list[Seg] = []
    for kind, text, path, label, cites in p.out:
        keys = tuple(dict.fromkeys(k for c in cites for k in bib.get(c, ())))
        segs.append(Seg(kind, text, path, label, cites=keys))
    return Parsed(_space(p.title) or None, segs)


# --- LaTeX source -------------------------------------------------------------------------------------------

_COMMENT = re.compile(r"(?<!\\)%.*")
_SECTION_CMD = re.compile(r"\\(section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*\{")
_ENV = re.compile(r"\\begin\{(table\*?|figure\*?|equation\*?|align\*?|gather\*?|multline\*?)\}")
_DROP_CMD = re.compile(
    r"\\(?:label|ref|eqref|vspace|hspace|centering|small|footnotesize|toprule|midrule|bottomrule|hline|"
    r"cline\{[^}]*\}|resizebox\{[^}]*\}\{[^}]*\}|includegraphics(?:\[[^\]]*\])?)\b"
)
_CITE = re.compile(r"\\cite[pt]?\*?(?:\[[^\]]*\])*\{([^}]*)\}")


def _braced(text: str, start: int) -> tuple[str, int]:
    """The {…} group opening at `start` (just after its brace): its text and the index after it."""
    depth, i = 1, start
    while i < len(text) and depth:
        if text[i] == "\\":
            i += 2
            continue
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[start : i - 1], i


def _detex(text: str) -> str:
    text = _CITE.sub("", text)
    text = _DROP_CMD.sub(" ", text)
    text = re.sub(r"\\(?:textbf|textit|emph|texttt|textsc|underline|mathrm|text)\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\url\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"~", " ", text)
    text = re.sub(r"\\&", "&", text)
    text = re.sub(r"\\(?:item|par|noindent|newline)\b|\\\\", " ", text)
    return _space(text)


def latex(source: str, bbl: str = "") -> Parsed:
    """A paper's LaTeX (\\input already inlined): sections, \\appendix, tables and figures (caption, label
    numbered in order), equations (LaTeX kept), paragraphs, and \\cite keys resolved through the .bbl."""
    text = "\n".join(_COMMENT.sub("", line) for line in source.splitlines())
    title = None
    if m := re.search(r"\\title\s*(?:\[[^\]]*\])?\s*\{", text):
        title = _detex(_braced(text, m.end())[0])
    if m := re.search(r"\\begin\{document\}", text):
        text = text[m.end() :]
    text = re.split(r"\\begin\{thebibliography\}|\\bibliography\{|\\end\{document\}", text)[0]
    bib = _bbl(bbl)
    segs: list[Seg] = []
    path: list[str] = []
    numbers = {"section": 0, "subsection": 0, "subsubsection": 0}
    appendix, counters = False, {"table": 0, "figure": 0, "equation": 0}
    i, buf = 0, []

    def flush() -> None:
        body = "".join(buf)
        buf.clear()
        for para in re.split(r"\n\s*\n", body):
            cites = tuple(
                k for keys in _CITE.findall(para) for c in keys.split(",") for k in bib.get(c.strip(), ())
            )
            words = _detex(para)
            if len(words) > 1 and not words.startswith("\\"):
                kind: SegKind = "appendix" if appendix else "paragraph"
                segs.append(Seg(kind, words, tuple(path), cites=tuple(dict.fromkeys(cites))))

    while i < len(text):
        sec = _SECTION_CMD.search(text, i)
        env = _ENV.search(text, i)
        app = text.find("\\appendix", i)
        nxt = min(
            (x for x in (sec.start() if sec else -1, env.start() if env else -1, app) if x >= 0), default=-1
        )
        if nxt < 0:
            buf.append(text[i:])
            break
        buf.append(text[i:nxt])
        flush()
        if nxt == app:
            appendix = True
            numbers = {"section": 0, "subsection": 0, "subsubsection": 0}
            i = app + len("\\appendix")
            continue
        if sec is not None and nxt == sec.start():
            level = sec.group(1)
            name, i = _braced(text, sec.end())
            depth = ("section", "subsection", "subsubsection").index(level)
            numbers[level] += 1
            for lower in ("section", "subsection", "subsubsection")[depth + 1 :]:
                numbers[lower] = 0
            if depth == 0:
                num = chr(64 + numbers["section"]) if appendix else str(numbers["section"])
            else:
                head = chr(64 + numbers["section"]) if appendix else str(numbers["section"])
                num = ".".join([head] + [str(numbers[x]) for x in ("subsection", "subsubsection")[:depth]])
            heading = f"{'Appendix ' if appendix and depth == 0 else ''}{num} {_detex(name)}"
            path = [*path[:depth], heading]
            continue
        assert env is not None
        name = env.group(1)
        end = text.find(f"\\end{{{name}}}", env.end())
        end = len(text) if end < 0 else end
        body = text[env.end() : end]
        i = end + len(f"\\end{{{name}}}")
        base = name.rstrip("*")
        if base in ("table", "figure"):
            counters[base] += 1
            caption = ""
            if m := re.search(r"\\caption\s*(?:\[[^\]]*\])?\s*\{", body):
                caption = _detex(_braced(body, m.end())[0])
            label = f"{'Table' if base == 'table' else 'Figure'} {counters[base]}"
            if base == "table":
                if m := re.search(r"\\caption\s*(?:\[[^\]]*\])?\s*\{", body):
                    body = body[: m.start()] + body[_braced(body, m.end())[1] :]
                body = re.sub(r"\\(?:begin|end)\{tabular\*?\}(?:\{[^}]*\})*", "\n", body)
                rows = [_detex(r.replace("&", " | ")) for r in re.split(r"\\\\|\n", body)]
                cells = "\n".join(r for r in rows if r and "|" in r)
                segs.append(Seg("table", f"{label}: {caption}\n{cells}".strip(), tuple(path), label))
            elif caption:
                segs.append(Seg("caption", f"{label}: {caption}", tuple(path), label))
        else:
            numbered = not name.endswith("*")
            label = None
            if numbered:
                counters["equation"] += 1
                label = f"Eq. ({counters['equation']})"
            eq = _space(re.sub(r"\\label\{[^}]*\}", "", body))
            if eq:
                segs.append(Seg("equation", eq, tuple(path), label))
    flush()
    return Parsed(title, segs)


def _bbl(bbl: str) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for m in re.finditer(
        r"\\bibitem(?:\[[^\]]*\])?\{([^}]*)\}(.*?)(?=\\bibitem|\\end\{thebibliography\}|$)", bbl, re.S
    ):
        out[m.group(1).strip()] = cite_keys(m.group(2))
    return out


# --- a PDF's text layer -------------------------------------------------------------------------------------

_HEADING = re.compile(
    r"^(?:([1-9][0-9]?(?:\.[0-9]{1,2}){0,2})|([A-H](?:\.[0-9]{1,2}){0,2}))\.?\s+([A-Z][^.!?]{2,60})$"
)
_CAPTION_START = re.compile(r"^(Table|Figure|Fig\.)\s+(\d+|[A-H]\.?\d*)\s*[:.]", re.IGNORECASE)
_REFERENCES = re.compile(r"^(?:\d+\.?\s+)?(References|Bibliography)$", re.IGNORECASE)


def pdf_pages(pages: Sequence[str], title: str | None = None) -> Parsed:
    """A PDF's text layer, one string a page: headings by their numbers ("4.2 Probing setup"), captions by
    "Table 3:" / "Figure 7:", paragraphs by blank lines, appendices after the references. Every segment
    carries its page. Tables come out as their caption line (the text layer has no cells)."""
    reader = _PdfReader()
    for number, page in enumerate(pages, start=1):
        reader.page(number, page)
    return Parsed(title, reader.segs, has_pages=True)


class _PdfReader:
    def __init__(self) -> None:
        self.segs: list[Seg] = []
        self.path: list[str] = []
        self.para: list[str] = []
        self.after_refs = False
        self.appendix = False
        self.number = 0

    def flush(self, kind_hint: SegKind | None = None, label: str | None = None) -> None:
        text = _space(" ".join(self.para))
        self.para.clear()
        if len(text) < 2 or (self.after_refs and not self.appendix):
            return  # the bibliography itself is not a source of claims
        kind: SegKind = kind_hint or ("appendix" if self.appendix else "paragraph")
        self.segs.append(Seg(kind, text, tuple(self.path), label, self.number, self.number))

    def page(self, number: int, page: str) -> None:
        self.number = number
        for raw in page.splitlines():
            line = raw.strip()
            if not line:
                self.flush()
            elif _REFERENCES.match(line):
                self.flush()
                self.after_refs = True
            elif (head := _HEADING.match(line)) and len(line) < 90:
                self.flush()
                letter = head.group(2)
                if letter and self.after_refs:
                    self.appendix = True
                depth = (head.group(1) or letter or "").count(".")
                self.path = [*self.path[:depth], f"{'Appendix ' if letter and depth == 0 else ''}{line}"]
            elif m := _CAPTION_START.match(line):
                self.flush()
                word = "Table" if m.group(1).lower().startswith("tab") else "Figure"
                self.para.append(line)
                self.flush("table" if word == "Table" else "caption", f"{word} {m.group(2).rstrip('.')}")
            else:
                self.para.append(line)
        self.flush()


def with_pages(parsed: Parsed, pages: Sequence[str]) -> Parsed:
    """Text parsed from LaTeX or HTML, with each segment's page found in the PDF's text layer."""
    if not pages:
        return parsed
    out: list[Seg] = []
    for seg in parsed.segments:
        page = page_of(seg.text, pages)
        out.append(replace(seg, page_from=page, page_to=page))
    return Parsed(parsed.title, out, has_pages=True)


# --- Markdown -----------------------------------------------------------------------------------------------


def markdown(text: str) -> Parsed:
    """Headings (#…) as the section path, paragraphs, pipe tables, $$ equations."""
    segs: list[Seg] = []
    path: list[str] = []
    title: str | None = None
    lines = text.splitlines()
    i, para = 0, list[str]()

    def flush() -> None:
        body = _space(" ".join(para))
        para.clear()
        if body:
            segs.append(Seg("paragraph", body, tuple(path)))

    while i < len(lines):
        line = lines[i].rstrip()
        if m := re.match(r"^(#{1,6})\s+(.*)$", line):
            flush()
            depth = len(m.group(1)) - 1
            name = m.group(2).strip()
            if depth == 0 and title is None:
                title = name
            path = [*path[:depth], name]
        elif line.strip().startswith("$$"):
            flush()
            eq = [line.strip()[2:]]
            while not eq[-1].rstrip().endswith("$$") and i + 1 < len(lines):
                i += 1
                eq.append(lines[i])
            body = _space(" ".join(eq)).removesuffix("$$").strip()
            if body:
                segs.append(Seg("equation", body, tuple(path)))
        elif line.strip().startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.fullmatch(r"[\s|:\-]+", lines[i]):
                    rows.append(_space(lines[i]).strip("| "))
                i += 1
            segs.append(Seg("table", "\n".join(rows), tuple(path)))
            continue
        elif not line.strip():
            flush()
        else:
            para.append(re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", line))
        i += 1
    flush()
    return Parsed(title, segs)


# --- a plain web page ---------------------------------------------------------------------------------------


class _Web(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.depth = 0
        self.title: list[str] = []
        self.in_title = False
        self.heading: tuple[int, list[str]] | None = None
        self.path: list[str] = []
        self.block: list[str] | None = None
        self.out: list[Seg] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            return
        self.depth += 1
        if self.skip or tag in _SKIP:
            self.skip = self.skip or self.depth
            return
        if tag == "title":
            self.in_title = True
        elif re.fullmatch(r"h[1-6]", tag):
            self.heading = (int(tag[1]) - 1, [])
        elif tag in ("p", "li", "blockquote", "figcaption", "pre"):
            self.block = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if self.skip == self.depth:
            self.skip = 0
        self.depth -= 1
        if tag == "title":
            self.in_title = False
        elif re.fullmatch(r"h[1-6]", tag) and self.heading is not None:
            level, words = self.heading
            self.path = [*self.path[:level], _space("".join(words))]
            self.heading = None
        elif tag in ("p", "li", "blockquote", "figcaption", "pre") and self.block is not None:
            text = _space("".join(self.block))
            if len(text) > 1:
                kind: SegKind = "caption" if tag == "figcaption" else "paragraph"
                self.out.append(Seg(kind, text, tuple(p for p in self.path if p)))
            self.block = None

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.in_title:
            self.title.append(data)
        elif self.heading is not None:
            self.heading[1].append(data)
        elif self.block is not None:
            self.block.append(data)


def web_page(html: str) -> Parsed:
    p = _Web()
    p.feed(html)
    p.close()
    return Parsed(_space(unescape("".join(p.title))) or None, p.out)
