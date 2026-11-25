"""What of an email or a page a person sees, and how much was hidden. Pure rules (the
standard library's HTML parser, no rendering, no remote content).

Text the user never sees is the common place to hide instructions for a model. Left
out, and counted (never kept, never logged):
- **zero-width, bidirectional and tag characters** (`ZERO_WIDTH`);
- **CSS-hidden elements**: display:none, visibility:hidden, mso-hide:all, zero opacity, the `hidden`
  attribute, a zero-height box that hides its overflow, a box moved far off the page;
- **tiny fonts**: 2 px / 2 pt or less, or under a fifth of an em;
- **text the colour of what is behind it**: white on white (an email's and a page's background is white
  unless an element sets one: `bgcolor`, `background`), or transparent text.

A hidden element ends where a browser ends it: at its end tag, an ancestor's end tag, or the next paragraph
or list item for one left open (an unclosed one never hides the rest). `hidden` counts
the hidden elements that held any text, plus the runs of zero-width characters: a number for the user's
weekly report, never the text.
"""

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser

ZERO_WIDTH = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]+")

_BLOCK = {
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article", "hr",
}  # fmt: skip
_SKIP = {"script", "style", "head", "title", "noscript", "template", "svg"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "wbr", "col", "area", "base", "source"}
# a style read as declarations, with thresholds: what a person can't see is hidden, not only
# what is exactly zero (opacity 0.01, a 3 px font, a 1 px box that hides its overflow, a near-white colour)
OPACITY_MIN = 0.1
FONT_MIN_PX = 4.0  # a font under 4 px (or 0.25 em) is unreadable
BOX_MAX_PX = 2.0  # a box this small that hides its overflow shows nothing
OFF_PAGE_PX = -999.0
NEAR = 12  # the largest channel difference (of 255) between a text's colour and what is behind it
EM_PX = 16.0
_CLASS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_IMPLIED_END = {"p": {"p"}, "li": {"li"}, "tr": {"tr"}, "td": {"td", "th"}, "th": {"td", "th"}}
# A browser closes an open <p> when one of these starts (M-B).
_CLOSES_P = {
    "address", "article", "aside", "blockquote", "dd", "details", "div", "dl", "dt", "fieldset", "figure",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "menu", "nav", "ol",
    "p", "pre", "section", "table", "ul",
}  # fmt: skip
_SCOPE = {"table", "td", "th", "tr", "tbody", "thead", "tfoot", "caption", "html", "body"}
_SPECIAL = _BLOCK | _SCOPE | _CLOSES_P
WHITE = "#ffffff"
_NAMED = {"white": WHITE, "black": "#000000", "transparent": "transparent"}


def colour(value: str | None) -> str | None:
    """A CSS or HTML colour as `#rrggbb` (or `transparent`), or None when it isn't one Pisa reads."""
    if not value:
        return None
    v = value.strip().lower().replace(" ", "")
    v = v.split("url(")[0] or v  # `background: #fff url(x)`
    if v in _NAMED:
        return _NAMED[v]
    if m := re.fullmatch(r"#?([0-9a-f]{3})", v):
        return "#" + "".join(c * 2 for c in m.group(1))
    if m := re.fullmatch(r"#?([0-9a-f]{6})", v):
        return "#" + m.group(1)
    if m := re.fullmatch(r"rgba?\((\d{1,3}),(\d{1,3}),(\d{1,3})(?:,([\d.]+))?\)", v):
        if m.group(4) is not None and float(m.group(4)) < OPACITY_MIN:
            return "transparent"
        return "#" + "".join(f"{min(255, int(m.group(i))):02x}" for i in (1, 2, 3))
    return None


def near(a: str, b: str) -> bool:
    """Two colours a person can't tell apart ("#fefefe" on white)."""
    if "transparent" in (a, b):
        return a == b
    return max(abs(int(a[i : i + 2], 16) - int(b[i : i + 2], 16)) for i in (1, 3, 5)) <= NEAR


def declarations(style: str) -> dict[str, str]:
    """A style attribute's (or rule's) declarations, the last of each winning; names and values lower case."""
    out: dict[str, str] = {}
    for part in style.split(";"):
        name, sep, value = part.partition(":")
        if sep:
            out[name.strip().lower()] = value.replace("!important", "").strip().lower()
    return out


def _px(value: str | None) -> float | None:
    if not value:
        return None
    m = re.match(r"(-?[\d.]+)\s*(px|pt|r?em|%)?", value.strip())
    if not m:
        return None
    try:
        number = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2) or "px"
    return number * EM_PX if unit in ("em", "rem") else (None if unit == "%" else number)


def hides(d: dict[str, str]) -> bool:
    """Declarations that keep what they style from a person's eyes (the colour aside)."""
    if (
        d.get("display") == "none"
        or d.get("visibility") in ("hidden", "collapse")
        or d.get("mso-hide") == "all"
    ):
        return True
    try:
        if "opacity" in d and float(d["opacity"].rstrip("%")) < OPACITY_MIN:
            return True
    except ValueError:  # a browser drops an opacity it can't read: the element shows
        pass
    if (size := _px(d.get("font-size"))) is not None and size < FONT_MIN_PX:
        return True
    if d.get("overflow") == "hidden" or d.get("overflow-x") == "hidden" or d.get("overflow-y") == "hidden":
        boxes = [_px(d.get(k)) for k in ("height", "max-height", "width", "max-width")]
        if any(b is not None and b <= BOX_MAX_PX for b in boxes):
            return True
    if re.fullmatch(r"rect\(\s*0(?:px)?[\s,]+0(?:px)?[\s,]+0(?:px)?[\s,]+0(?:px)?\s*\)", d.get("clip", "")):
        return True
    if re.search(r"inset\(\s*50%", d.get("clip-path", "")):
        return True
    if re.search(r"scale[xy]?\(\s*0(?:\.0+)?\s*[,)]", d.get("transform", "")):
        return True
    return any(
        (v := _px(d.get(k))) is not None and v <= OFF_PAGE_PX
        for k in ("left", "top", "text-indent", "margin-left")
    )


@dataclass(frozen=True)
class Visible:
    text: str  # what a person sees, as plain text
    ld: tuple[str, ...]  # schema.org JSON-LD blocks, raw (booking and order emails carry one)
    hidden: int  # the hidden runs left out (elements with text, zero-width runs): a count, never the text
    # what each hidden element held, in memory only (compared with a mail's plain part, never kept)
    hidden_texts: tuple[str, ...] = ()
    # the class of the failure that stopped the reading (a bug in `_Text`: the parser takes any text); the
    # text is what was read up to it, never the markup. The caller logs it.
    unparsed: str | None = None


def plain(text: str) -> tuple[str, int]:
    """Plain text without its zero-width, bidirectional and tag characters, and how many runs there were."""
    runs = len(ZERO_WIDTH.findall(text))
    return (ZERO_WIDTH.sub("", text) if runs else text), runs


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ld: list[str] = []
        self.hidden = 0
        self._skip = 0
        self._open: list[str] = []  # every open element, outermost first
        self._back: list[str | None] = []  # the background each open element sets (None: its parent's)
        self._hidden_at: int | None = None  # where in `_open` the outermost hidden element is
        self._hidden_text = False  # the hidden element holds text (counted once when it ends)
        self._hidden_now: list[str] = []
        self.hidden_texts: list[str] = []
        self._in_ld = False
        self._in_style = False
        self._style = ""
        self._rules: dict[str, dict[str, str]] = {}  # `.class` and `#id` rules from <style>

    def _background(self) -> str:
        return next((b for b in reversed(self._back) if b is not None), WHITE)

    def _declared(self, attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        """An element's style: its class and id rules (from the <style> blocks read so far), then its own."""
        found = dict(attrs)
        d: dict[str, str] = {}
        for name in (found.get("class") or "").split():
            d |= self._rules.get(f".{name}", {})
        if found.get("id"):
            d |= self._rules.get(f"#{found['id']}", {})
        return d | declarations(found.get("style") or "")

    def _is_hidden(self, attrs: list[tuple[str, str | None]], d: dict[str, str], back: str | None) -> bool:
        if "hidden" in dict(attrs) or hides(d):
            return True
        text = colour(dict(attrs).get("color"))  # <font color>
        if "color" in d:
            text = colour(d["color"])
        return text is not None and (text == "transparent" or near(text, back or self._background()))

    def _read_rules(self) -> None:
        css = re.sub(r"/\*.*?\*/", "", self._style, flags=re.DOTALL)
        for selectors, body in _CLASS_RULE.findall(css):
            d = declarations(body)
            for selector in selectors.split(","):
                if m := re.fullmatch(r"\s*(?:[a-z0-9]+)?([.#][\w-]+)\s*", selector, re.IGNORECASE):
                    self._rules[m.group(1)] = self._rules.get(m.group(1), {}) | d

    def _close(self, at: int) -> None:
        del self._open[at:]
        del self._back[at:]
        if self._hidden_at is not None and at <= self._hidden_at:
            self._hidden_at = None
            self.hidden += self._hidden_text
            if self._hidden_text:
                self.hidden_texts.append(" ".join("".join(self._hidden_now).split()))
            self._hidden_text = False
            self._hidden_now = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and (dict(attrs).get("type") or "").lower() == "application/ld+json":
            self._in_ld = True
            self.ld.append("")
        if tag == "style":
            self._in_style, self._style = True, ""
        if tag in _SKIP:
            self._skip += 1
            return
        if tag in _CLOSES_P and "p" in self._open:
            at = len(self._open) - 1 - self._open[::-1].index("p")
            if not any(t in _SCOPE for t in self._open[at + 1 :]):
                self._close(at)
        if tag in _IMPLIED_END and self._open and self._open[-1] in _IMPLIED_END[tag]:
            self._close(len(self._open) - 1)  # <p>…<p>: the first one ends here
        if tag not in _VOID:
            d = self._declared(attrs)
            back = colour(dict(attrs).get("bgcolor"))
            for name in ("background", "background-color"):
                if (c := colour(d.get(name))) not in (None, "transparent"):
                    back = c
            if self._hidden_at is None and self._is_hidden(attrs, d, back):
                self._hidden_at = len(self._open)
            self._open.append(tag)
            self._back.append(back)
        if self._hidden_at is None and tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            self._in_ld = False
            if tag == "style" and self._in_style:
                self._in_style = False
                self._read_rules()
            return
        if tag in ("body", "html"):  # a browser closes nothing here: later text joins the open element
            return
        if tag in self._open:
            at = len(self._open) - 1 - self._open[::-1].index(tag)
            # An inline end tag doesn't close across a block (`<span><div hidden>X</span>…`: still hidden);
            # a block's end tag does (`<div><span hidden>X</div>…`: visible again).
            if tag in _SPECIAL or not any(t in _SPECIAL for t in self._open[at + 1 :]):
                self._close(at)
        if self._hidden_at is None and tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self.ld[-1] += data
        elif self._in_style:
            self._style += data
        elif self._skip:
            return
        elif self._hidden_at is None:
            self.parts.append(data)
        elif data.strip():
            self._hidden_text = True
            self._hidden_now.append(data)

    def finish(self) -> None:
        self.close()
        if self._hidden_at is not None:  # an element still open at the end
            self.hidden += self._hidden_text
            if self._hidden_text:
                self.hidden_texts.append(" ".join("".join(self._hidden_now).split()))


def html(markup: str) -> Visible:
    """An HTML email's or page's visible text (not yet normalised), its JSON-LD, and the hidden runs."""
    parser = _Text()
    unparsed = None
    try:
        parser.feed(markup)
        parser.finish()
    except Exception as exc:  # a page Pisa can't parse: what was read so far, never the markup
        unparsed = exc.__class__.__name__
    text, runs = plain("".join(parser.parts))
    return Visible(
        text=text, ld=tuple(parser.ld), hidden=parser.hidden + runs, hidden_texts=tuple(parser.hidden_texts),
        unparsed=unparsed,
    )  # fmt: skip


def normalize(text: str) -> str:
    text = unescape(text).replace("\r\n", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
