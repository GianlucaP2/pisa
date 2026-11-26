"""Cut the quoted history out of a mail, so what is left is what the sender just wrote. Pure rules, no I/O,
no dependency.

Two cutters, the same markers:
- `cut_html` works on the HTML before it becomes text: a `blockquote`, a Gmail quote block (`gmail_quote`),
  Yahoo's and Proton's, Outlook's reply header, and everything after it;
- `cut_text` works on plain text: the "On ... wrote:" header (and "Il giorno ... ha scritto:" and a few other
  languages), the "-----Original Message-----" line, and `>` lines.

A forwarded message goes too: someone else wrote it, so it never makes an event, a storyline or evidence
(release audit §38: a forward's old "that day works" opened a booking, and its injected line came with it).
`CUT_VERSION` goes up whenever the rules change, and each row keeps the version that cut it, so a night job
can cut older rows again.
"""

import re
from dataclasses import dataclass

CUT_VERSION = 2
# What the cutters read at most, so a huge mail costs no more than this to cut (E1 QC-1): four times what
# is kept of a body as text (`pisa.domain.mail.BODY_MAX_CHARS`, 8 000), and eight times that for markup,
# which is mostly tags
TEXT_MAX_CHARS = 32_000
MARKUP_MAX_CHARS = 256_000


@dataclass(frozen=True)
class Cut:
    """What the sender just wrote (`new_text`), and the history under it (`quoted`)."""

    new_text: str
    quoted: str
    cut_version: int = CUT_VERSION


# --- plain text ---------------------------------------------------------------------------------------------

# "On Tue, 4 Nov 2025 at 10:00, Jane Doe <jane@example.com> wrote:", which a mail client may wrap over lines
_HEADER = re.compile(
    r"^\s*(?:on\b.{3,400}?\bwrote\s?:|il\b.{3,400}?\bha scritto\s?:|le\b.{3,400}?\b[aà] [ée]crit\s?:"
    r"|am\b.{3,400}?\bschrieb\b.{0,200}?:|el\b.{3,400}?\bescribi[oó]\s?:)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_HEADER_START = re.compile(r"^\s*(?:on|il|le|am|el)\b", re.IGNORECASE)
_HEADER_LINES = 3  # a header wraps over at most this many lines
_ORIGINAL = re.compile(
    r"^\s*[-_=]{2,}\s*(?:original message|messaggio originale|message d.origine|urspr[uü]ngliche nachricht"
    r"|mensaje original)\s*[-_=]{2,}\s*$",
    re.IGNORECASE,
)
# `\W*` alone takes the dashes and spaces before the words: a second dash group beside it backtracked
# quadratically on a long dash run (E1 QC-1)
_FORWARD = re.compile(
    r"^\W*(?:forwarded message|begin forwarded message|messaggio inoltrato|inoltrato)\b",
    re.IGNORECASE,
)
_QUOTED_LINE = re.compile(r"^\s*>")


def _header_end(lines: list[str], at: int) -> int | None:
    """The index after a "... wrote:" header that starts at line `at` (it may wrap), or None."""
    if not _HEADER_START.match(lines[at]):
        return None
    for count in range(1, _HEADER_LINES + 1):
        if at + count > len(lines):
            break
        if _HEADER.match(" ".join(line.strip() for line in lines[at : at + count])):
            return at + count
    return None


def _squash(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def cut_text(text: str) -> Cut:
    """Plain text without its quoted history. A header with `>` lines under it takes that block only (what is
    written under the block is new: an inline reply); a header with no `>` lines, the "Original Message" line
    and a "Forwarded message" line take everything after them."""
    lines = text[:TEXT_MAX_CHARS].replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    gone: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _ORIGINAL.match(line) or _FORWARD.match(line):
            gone += lines[i:]
            break
        end = _header_end(lines, i)
        if end is not None:
            after = end
            while after < len(lines) and not lines[after].strip():
                after += 1
            if after < len(lines) and _QUOTED_LINE.match(lines[after]):
                block_end = after
                while block_end < len(lines) and (
                    _QUOTED_LINE.match(lines[block_end]) or not lines[block_end].strip()
                ):
                    block_end += 1
                gone += lines[i:block_end]
                i = block_end
                continue
            gone += lines[i:]
            break
        if _QUOTED_LINE.match(line):
            start = i
            while i < len(lines) and (_QUOTED_LINE.match(lines[i]) or not lines[i].strip()):
                i += 1
            gone += lines[start:i]
            continue
        kept.append(line)
        i += 1
    return Cut(_squash(kept), _squash(gone))


# --- HTML ---------------------------------------------------------------------------------------------------

_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9:-]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>")
_VOID = frozenset(
    """
    area base br col embed hr img input link meta param source track wbr
    """.split()
)
_CLASS = re.compile(r"""\bclass\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_ID = re.compile(r"""\bid\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_QUOTE_CLASSES = frozenset({"gmail_quote", "yahoo_quoted", "protonmail_quote", "gmail_extra"})
# an Outlook reply: its header block and everything after it is the history
_TO_END_IDS = frozenset({"divrplyfwdmsg", "appendonsend", "mail-editor-reference-message-container"})
_PEEK = 400  # characters after a quote block's opening tag that tell whether it is a forward
_TAGS_IN_PEEK = re.compile(r"<[^>]*>")


def _attr(pattern: re.Pattern[str], attrs: str) -> str:
    m = pattern.search(attrs)
    return (m.group(1) or m.group(2) or "") if m else ""


def _opens_quote(name: str, attrs: str) -> str | None:
    """The kind of quote the element opens: "block", "end" (the history to the end of the mail) or None."""
    if name == "blockquote":
        return "block"
    if _attr(_ID, attrs).lower() in _TO_END_IDS:
        return "end"
    if name in ("div", "section", "span") and _QUOTE_CLASSES & set(_attr(_CLASS, attrs).lower().split()):
        return "block"
    return None


def _forward(markup: str, after: int) -> bool:
    """The element that opens at `after` begins with a "Forwarded message" line: a forward, not a reply."""
    peek = _TAGS_IN_PEEK.sub(" ", markup[after : after + _PEEK])
    return bool(_FORWARD.match(" ".join(peek.split())))


def cut_html(markup: str) -> tuple[str, str]:
    """(the markup without its quote blocks, the quote blocks). A `blockquote` or a Gmail quote block goes
    whole, a block that opens a forward too; an Outlook reply header and a forward's header in no quote block
    take everything after them."""
    markup = markup[:MARKUP_MAX_CHARS]
    stack: list[tuple[str, int, bool]] = []  # (tag name, where it starts, is the quote's root)
    spans: list[tuple[int, int]] = []
    inside = False  # an outer quote block is open: the ones inside it are part of it
    for m in _TAG.finditer(markup):
        closing, name, attrs = m.group(1) == "/", m.group(2).lower(), m.group(3)
        if not closing:
            if name in _VOID or attrs.rstrip().endswith("/"):
                continue
            kind = None if inside else _opens_quote(name, attrs)
            if kind is None and not inside and _forward(markup, m.end()):
                kind = "end"  # a forward in no quote block: its header and everything after it
            if kind == "end":
                spans.append((m.start(), len(markup)))
                break
            root = kind == "block"
            stack.append((name, m.start(), root))
            inside = inside or root
        else:
            for depth in range(len(stack) - 1, -1, -1):
                if stack[depth][0] == name:
                    _, start, root = stack[depth]
                    del stack[depth:]
                    if root:
                        spans.append((start, m.end()))
                        inside = False
                    break
    for _, start, root in stack:  # a quote block left open runs to the end
        if root:
            spans.append((start, len(markup)))
            break
    kept: list[str] = []
    gone: list[str] = []
    at = 0
    for start, end in sorted(spans):
        if start < at:
            continue
        kept.append(markup[at:start])
        gone.append(markup[start:end])
        at = end
    kept.append(markup[at:])
    return "".join(kept), "\n".join(gone)
