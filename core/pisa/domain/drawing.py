"""Pisa writes its own drawings, run sealed on their phone. Pure: no I/O.

A drawing is a self-contained page Pisa writes (HTML, inline SVG, inline JavaScript, the libraries the app
bundles) plus `data`, a JSON object the page reads as `window.PISA_DATA`. Before anything is sent, code (never
a model) checks it. The check is defence in depth: a string check can't be complete (string building
passes it); the phone's box (no network, the rule list) and the sealed CSP are the wall.
- `problems`: the size, that it parses (read as a browser parses it), and none of the forbidden ways out (an
  address, the network, storage, a form, a frame, navigation, eval, markup written from values, an inline
  handler); the app's bridge is the only way out ("ask Pisa about this", after their tap; its label is the
  page's text, quarantined by Pisa);
- `seal`: the page's own strict CSP first, each inline script allowed by its hash, never 'unsafe-inline' for
  scripts (markup that values put in the page never runs);
- `knowledge_problems`: a drawing of what Pisa knows, captioned "Da quello che so", has no chart and no
  number its turn didn't carry;
- `unseen`: `data` holds only values this turn's tool results and their words carry ("only what this turn
  saw");

- `outside_in_code`: no string from outside text (pages, mail, reviews, map results) is written in the code:
  such text travels as data only. A refused drawing falls back to the sheet or words.
"""

import base64
import hashlib
import html as html_text
import json
import math
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any, Literal

import regex

from pisa.domain.text import fold_words

HTML_MAX = 200_000  # bytes of the page Pisa writes ("size ≤ 200 KB")
DATA_MAX = 100_000  # bytes of PISA_DATA as JSON
TITLE_MAX = 120
SUMMARY_MAX = 400
SUMMARY_SENTENCES = 3
ASK_MAX = (
    120  # the label of a tap ("ask Pisa about this"): user-perceived characters (grapheme clusters, as iOS)
)
KEY_MAX = 40
NAME_MIN_CHARS = 6  # an outside name shorter than this ("Bar", "Roma") is too common to refuse code over
SHINGLE = 4  # consecutive words of outside text that may not appear in the code
SMALL_INT = 10  # integers up to this (an index, a level, a step) are structure, not facts
KEPT_ROWS = 5  # a conversation keeps its last five drawings (the last one is redrawn)

Kind = Literal["flow", "schema", "chart", "comparison", "timeline", "explorer", "other"]
KINDS: tuple[str, ...] = ("flow", "schema", "chart", "comparison", "timeline", "explorer", "other")

# the one library contract with the app: the files and versions the app pins in its own bundle. A page loads
# one only as <script src="pisa-bundle://lib/<name>.min.js"></script>, never from an address; the wire names
# it with the app's version. Vega runs its expressions without eval through vega-interpreter (the CSP has no
# 'unsafe-eval'): vegaEmbed(el, spec, {ast: true, expr: vega.expressionInterpreter, actions: false}).
LIBRARIES: dict[str, str] = {
    "d3": "7.9.0",
    "mermaid": "12.2.0",
    "vega": "6.4.0",
    "vega-lite": "6.5.0",
    "vega-embed": "7.3.0",
    "vega-interpreter": "2.3.2",
}
# The brief's policy, plus the two directives `default-src` doesn't cover (research/visual-answers §2.2): no
# form submits anywhere, no <base> changes where the page's links point. No 'unsafe-inline' for
# scripts: each inline script of the page Pisa wrote is allowed by its own hash, so markup that outside text
# gets into the page (an <img onerror>, a <script> written at run time) never runs: "data never becomes code"
# is the browser's rule, not only the check's. Styles stay inline.
CSP_HEAD = "default-src 'none'; script-src"
CSP_TAIL = (
    "pisa-bundle:; style-src 'unsafe-inline'; img-src data: pisa-bundle:; form-action 'none'; base-uri 'none'"
)

# XML namespace names are identifiers a page must write to make SVG (createElementNS): never fetched.
_NAMESPACES = (
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/XML/1998/namespace",
)
# RAIL: the forbidden ways out, each with the words the model reads when refused. Matched on the
# page's code (its tags, attributes, scripts and styles; a text node is never run). `True`: any letter case
# (HTML and addresses); JavaScript's names are matched as JavaScript spells them.
_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (why, re.compile(pattern, re.IGNORECASE if anycase else 0))
    for why, pattern, anycase in (
        ("an address (http/https): the page loads nothing", r"https?\s*:", True),
        ("'//' (an address, or a // comment: write /* */ comments)", r"//", True),
        ("a socket address (ws/wss)", r"\bwss?\s*:", True),
        ("fetch", r"\bfetch\b", False),
        ("XMLHttpRequest", r"XMLHttpRequest", True),
        ("WebSocket", r"WebSocket", True),
        ("EventSource", r"EventSource", True),
        ("importScripts", r"importScripts", True),
        ("navigator.sendBeacon", r"sendBeacon", True),
        ("a <form>", r"<\s*form\b", True),
        ("an <iframe> or <frame>", r"<\s*i?frame\b", True),
        ("an <object>", r"<\s*object\b", True),
        ("an <embed>", r"<\s*embed\b", True),
        ("a <base>", r"<\s*base\b", True),
        ("a <link> (styles go inline)", r"<\s*link\b", True),
        ("a <meta http-equiv> (refresh: Pisa adds the page's only one)", r"<\s*meta\b[^>]*http-equiv", True),
        ("window.open", r"\bopen\s*\(", False),
        ("a location write (navigation)",
         r"\blocation\b\s*(?:\.\s*(?:href|pathname|search|hash|host|hostname|protocol|port)\s*)?=(?!=)",
         False),
        ("location.assign/replace/reload (navigation)", r"\blocation\s*\.\s*(?:assign|replace|reload)\b",
         False),
        ("document.cookie", r"\bcookie\b", True),
        ("localStorage or sessionStorage", r"\b(?:local|session)Storage\b", True),
        ("indexedDB", r"\bindexedDB\b", True),
        ("the cache or a service worker", r"\bcaches\b|serviceWorker", False),
        ("a worker", r"\b(?:Shared)?Worker\s*\(", False),
        ("a peer connection", r"RTCPeerConnection", True),
        ("device access (navigator.…)",
         r"\bnavigator\s*\.\s*(?:geolocation|mediaDevices|clipboard|share|bluetooth|usb|serial|hid|"
         r"credentials|storage|permissions|vibrate|getUserMedia)", False),
        ("eval", r"\beval\b", False),
        ("new Function", r"\bFunction\s*\(", False),
        ("a string run as code (setTimeout/setInterval with text)",
         r"\bset(?:Timeout|Interval)\s*\(\s*['\"`]", False),
        ("import (a module or import())", r"\bimport\b", False),
        ("postMessage (the app's bridge is the only way out)",
         r"\bpostMessage\b|\bwebkit\s*\.|messageHandlers", False),
        ("a javascript: address", r"javascript\s*:", True),
        ("@import", r"@import\b", True),
        ("markup written from values (innerHTML, outerHTML, insertAdjacentHTML, document.write, .html()): "
         "set textContent", r"\b(?:inner|outer)HTML\b|insertAdjacentHTML|document\s*\.\s*write|\.html\s*\(",
         False),
        ("a computed global (window[...], self[...])",
         r"\b(?:window|self|globalThis|top|parent|frames|document)\s*\[", False),
        ("srcdoc", r"\bsrcdoc\b", True),
        # the obvious ways around the names above (the phone's box is the wall; this is depth)
        ("a computed global (this[...], Reflect)", r"\bthis\s*\[|\bReflect\b|getOwnPropertyDescriptor",
         False),
        ("a constructor chain (code from a string)", r"\.\s*constructor\b|\[\s*['\"`]constructor", False),
        ("a frame's window (contentWindow, contentDocument)", r"content(?:Window|Document)\b", False),
        ("WebRTC (RTC…)", r"\bRTC\w*|webkitRTC", False),
        ("sound or speech (speechSynthesis, AudioContext)", r"[sS]peechSynthesis|AudioContext", False),
        ("text built from codes (fromCharCode, fromCodePoint, atob, unescape): text goes in data",
         r"\bfromCharCode\b|\bfromCodePoint\b|\batob\b|\bunescape\b", False),
        ("a computed location (location[...])", r"\blocation\s*\[", False),
        ("a computed navigator (navigator[...])", r"\bnavigator\s*\[", False),
        ("a meta refresh made in code (httpEquiv)", r"\bhttp-?equiv\b", True),
        ("a link's rel (preconnect, prefetch, preload: the network)",
         r"\b(?:pre(?:connect|fetch|load|render)|dns-prefetch|modulepreload)\b|\.\s*rel(?:List)?\b", True),
        ("an element that loads or leaves, made in code (iframe, link, meta, base, object, embed, script, "
         "form, media)",
         r"\b(?:createElement(?:NS)?|append|insert|join)\s*\([^)]*?['\"`]\s*(?:i?frame|frameset|link|meta|base|"
         r"object|embed|portal|script|form|audio|video|source|track)\b", True),
        ("markup parsed from a string (DOMParser, createContextualFragment, setHTMLUnsafe)",
         r"DOMParser|createContextualFragment|(?:set|parse)HTMLUnsafe", False),
        ("a blob address (createObjectURL)", r"createObjectURL", False),
        ("execCommand (the clipboard)", r"\bexecCommand\b", False),
        ("Notification", r"\bNotification\b", False),
        # a file input opens the phone's camera, photos or files
        ("a file picker (<input type=file>, showPicker: the camera, photos, files)",
         r"\btype\s*=\s*['\"]?\s*file\b|['\"`]file['\"`]|\bshowPicker\b", True),
        ("a CSS image or source without url() (image-set, src())", r"image-set\s*\(|(?<![\w-])src\s*\(",
         True),
    )
)  # fmt: skip
# research/visual-answers §2.2: a drawing never looks like Pisa's own approval or Face ID surfaces (nothing is
# approved from a drawing; a fake card could only mislead). Matched on the whole page, its text too.
_LOOKALIKE: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (why, re.compile(pattern, re.IGNORECASE))
    for why, pattern in (
        ("an approval look-alike (approva / approve / autorizza / authorize: nothing is approved from a "
         "drawing)", r"\b(?:approv[aoie]\w*|autorizz\w*|authori[sz]\w*)\b"),
        ("a Face ID or Touch ID look-alike", r"\b(?:face|touch)\s*id\b"),
    )
)  # fmt: skip
_URL_ATTRS = frozenset({"src", "href", "xlink:href", "action", "formaction", "srcset", "poster", "data",
                        "background", "ping", "manifest", "codebase", "cite", "longdesc",
                        "usemap"})  # fmt: skip
_CSS_URL = re.compile(r"url\s*\(\s*['\"]?\s*([^'\")\s]*)", re.IGNORECASE)
_BUNDLED = re.compile(r"^pisa-bundle://lib/(d3|mermaid|vega|vega-lite|vega-embed|vega-interpreter)\.min\.js$")


_JS_ESCAPE = re.compile(r"\\u\{([0-9a-fA-F]{1,6})\}|\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")


def _unescaped(text: str) -> str:
    """A script's string as it runs: its \\u and \\x escapes decoded."""
    return _JS_ESCAPE.sub(lambda m: chr(min(int(next(g for g in m.groups() if g), 16), 0x10FFFF)), text)


_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?|\\(.)", re.DOTALL)


def _css_text(css: str) -> str:
    """A style as the browser reads it: its escapes decoded ('u\\72l(' is 'url(', '@\\69mport' '@import')."""
    return _CSS_ESCAPE.sub(
        lambda m: chr(min(int(m.group(1), 16), 0x10FFFF) or 0xFFFD) if m.group(1) else m.group(2), css
    )


def _key(text: str) -> str:
    """Lowercase letters and digits, single spaces, no accents: how a value is found in a text."""
    text = "".join(c for c in (text or "") if unicodedata.category(c) != "Cf")  # zero-width marks out
    return fold_words(text)


class _Page(HTMLParser):
    """The page as the check reads it: its scripts, its texts (nodes, attribute values, comments), the
    libraries it loads and the addresses its attributes name."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.texts: list[str] = []
        self.libraries: list[tuple[str, str]] = []
        self.bad: list[str] = []
        self.open: list[str] = []  # script and style tags not closed yet
        self._raw: list[str] = []
        # the page's code as the parser read it (tags, attribute values decoded, script and
        # style bodies raw and decoded): what a browser runs, not what a regex over the raw page sees
        self.code: list[str] = []
        self.styles: list[str] = []
        self.inline: list[str] = []  # each inline script's body, as the browser hashes it
        self.words: list[
            str
        ] = []  # what they read: text nodes and the attributes that are words (title, alt…)
        self.loads = False  # the script being read has a src (its body never runs)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or f"<{tag}>"
        self.code += [raw, html_text.unescape(raw)]  # an attribute's '&#101;' is an 'e' to the browser
        for name, value in attrs:
            value = value or ""
            self.texts.append(value)
            if name.startswith("on"):
                self.scripts.append(value)
                # the sealed CSP allows scripts by hash only: an inline handler never runs
                self.bad.append(
                    f"an inline handler ({name}=…): it doesn't run under the sealed CSP; use "
                    "addEventListener in a script"
                )
            if name in _URL_ATTRS and value.strip():
                address = value.strip()
                if tag == "script" and name == "src":
                    found = _BUNDLED.match(address)
                    if found is None:
                        self.bad.append(
                            "a script from outside the bundle (only "
                            '<script src="pisa-bundle://lib/<name>.min.js">, '
                            f"the bundle: {', '.join(f'{k}.min.js' for k in LIBRARIES)})"
                        )
                    else:
                        self.libraries.append((found.group(1), LIBRARIES[found.group(1)]))
                elif not (address.startswith("#") or address.lower().startswith("data:image/")):
                    self.bad.append(f"a {name} that isn't '#…' or an inline data: image")
            if name == "style":
                self._css(_css_text(value))
                self.code.append(_css_text(value))
        if tag in ("script", "style"):
            self.open.append(tag)
            self._raw = []
            self.loads = tag == "script" and any(n == "src" for n, _ in attrs)  # a src: its body never runs
        if name_words := [v for n, v in attrs if n in _WORDS_ATTRS and v]:
            self.words += name_words

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):  # a browser ignores the '/': what follows is its body, not markup
            self.bad.append(f"a self-closed <{tag} … />: close it with </{tag}>")
        self.handle_starttag(tag, attrs)
        if tag in ("script", "style") and self.open and self.open[-1] == tag:
            self.open.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.open and self.open[-1] == tag:
            self.open.pop()
            body = "".join(self._raw)
            # in SVG or MathML a script's or style's character references are decoded: both forms are code
            self.code += [
                body,
                html_text.unescape(body),
                _unescaped(body) if tag == "script" else _css_text(body),
            ]
            if _ESCAPES.search(body):  # a parser differential: where the browser ends the block isn't ours
                self.bad.append("'<!--', '-->' or '<script' inside a script or style (write them apart)")
            if tag == "script":
                self.scripts.append(body)
                if not self.loads:
                    self.inline.append(body)
                self.loads = False
            else:
                self.styles.append(body)
                if _MARKUP.search(body):  # in SVG a <style> is markup: '<img …>' would break out
                    self.bad.append("markup inside a <style>")
                self._css(_css_text(body))
            self._raw = []

    def handle_data(self, data: str) -> None:
        if self.open:
            self._raw.append(data)
        else:
            self.texts.append(data)
            self.words.append(data)
            if _MARKUP.search(data):  # <title>, <textarea>, <xmp> read as text here, as markup in SVG
                self.bad.append("markup inside a text (write '&lt;' apart from a letter, or put it in data)")

    def handle_comment(self, data: str) -> None:
        self.texts.append(data)

    def _css(self, css: str) -> None:
        for found in _CSS_URL.finditer(css):
            target = found.group(1)
            if not (target.startswith("#") or target.lower().startswith("data:image/")):
                self.bad.append("a CSS url() that isn't '#…' or an inline data: image")


_WORDS_ATTRS = frozenset({"title", "alt", "aria-label", "aria-description", "placeholder", "label",
                          "data-pisa-ask"})  # fmt: skip
_ESCAPES = re.compile(r"<!--|-->|</?script", re.IGNORECASE)
_MARKUP = re.compile(r"<[A-Za-z!/?]")
# comments and declarations a browser ends elsewhere than Python's parser (an '<!-->', a CDATA section, a
# bogus '<!…>'): what one hides the other may run
_DIFFERENTIAL = re.compile(r"<!--->|<!-->|--!>|<!(?!--|doctype\b)|<\?", re.IGNORECASE)


_STRING = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`", re.DOTALL)
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_PAIRS = {")": "(", "]": "[", "}": "{"}


def script_parses(code: str) -> bool:
    """A light parse of an inline script: its strings and comments close, its brackets balance."""
    rest = _COMMENT.sub(" ", code)
    if "/*" in rest:
        return False
    rest = _STRING.sub('""', rest)
    if re.search(r"(?<!\\)['\"`]", rest.replace('""', "")):
        return False
    stack: list[str] = []
    for char in rest:
        if char in "([{":
            stack.append(char)
        elif char in _PAIRS:
            if not stack or stack.pop() != _PAIRS[char]:
                return False
    return not stack


def _strings_in(code: str) -> list[str]:
    return [m.group(0)[1:-1] for m in _STRING.finditer(_COMMENT.sub(" ", code))] + _COMMENT.findall(code)


# the elements and link hints the phone's insertion firewall refuses, as code builds
# them where the check can see it: a name or markup joined from pieces ('ifr' + 'ame', ['pre','connect']
# .join(''), 'a'.concat('b'), a template's literal parts), a tag computed at createElement, an Attr node.
# Reversed or decoded at run time it can't be seen: the phone's firewall is the wall, this is depth.
_BUILT_TAGS = r"i?frame|frameset|link|meta|base|object|embed|portal"
_BUILT_NAME = re.compile(rf"^\s*(?:{_BUILT_TAGS})\s*$|<\s*(?:{_BUILT_TAGS})\b", re.IGNORECASE)
_BUILT_REL = re.compile(
    r"pre(?:connect|fetch|load|render)|dns-prefetch|modulepreload|^\s*rel\s*$", re.IGNORECASE
)
_LONE_FRAME = re.compile(
    r"^\s*(?:i?frame|frameset|embed|portal)\s*$", re.IGNORECASE
)  # never a word a drawing needs
_JOINS = re.compile(r"\s*(?:\+|\.\s*concat\s*\()\s*\(?\s*")  # what may stand between two pieces of one string
_ARRAY_JOIN = re.compile(
    r"\[([^\[\]]*)\]\s*\.\s*join\s*\(\s*(?:(['\"`])((?:\\.|(?!\2).)*)\2)?\s*\)", re.DOTALL
)
_TAG_ARG = re.compile(r"\bcreateElement(NS)?\s*\(\s*([^)]*)")
_ONE_STRING = re.compile(r"^(?:'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\$])*`)$")


def _literal(token: str) -> str:
    """A string literal's text as it runs (a template's literal parts, its ${…} left out)."""
    body = token[1:-1]
    if token[0] == "`":
        body = re.sub(r"\$\{[^}]*\}", "", body)
    return _unescaped(body)


def built_strings(code: str) -> list[str]:
    """Strings a script builds from pieces: chains of literals joined by + or concat, and arrays of literals
    joined; each template literal's literal parts together."""
    code = _COMMENT.sub(" ", code)
    out: list[str] = []
    chain: list[str] = []
    end = -1
    for found in _STRING.finditer(code):
        token = found.group(0)
        if token[0] == "`" and "${" in token:  # its literal parts, as markup or a hint (never a bare name:
            out.append(
                _literal(token) + "\0"
            )  # `${n} link` is a word; "\0" keeps the name rule from matching
        if chain and _JOINS.fullmatch(code[end : found.start()]):
            chain.append(_literal(token))
        else:
            if len(chain) > 1:
                out.append("".join(chain))
            chain = [_literal(token)]
        end = found.end()
    if len(chain) > 1:
        out.append("".join(chain))
    for found in _ARRAY_JOIN.finditer(code):
        pieces = [_literal(m.group(0)) for m in _STRING.finditer(found.group(1))]
        out.append(_unescaped(found.group(3)).join(pieces) if found.group(2) else ",".join(pieces))
    return out


def built_in_code(scripts: list[str]) -> list[str]:
    """Why the page's scripts build an element or a link hint the phone refuses ([]: they don't)."""
    out: list[str] = []
    for code in scripts:
        if any(_BUILT_NAME.search(t) or _BUILT_REL.search(t) for t in built_strings(code)):
            out.append(
                "forbidden: a frame, link, meta, base, object, embed or portal or a link hint (rel, "
                "preconnect…) built from pieces of strings"
            )
        if any(_LONE_FRAME.match(_literal(m.group(0))) for m in _STRING.finditer(_COMMENT.sub(" ", code))):
            out.append("forbidden: a frame, embed or portal named in code")
        tags = [(m.group(2).split(",")[1:2] if m.group(1) else [m.group(2)]) for m in _TAG_ARG.finditer(code)]
        if any(not t or not _ONE_STRING.match(t[0].strip()) for t in tags):
            out.append("forbidden: an element whose tag is computed (createElement takes one plain string)")
        if re.search(r"\bcreateAttribute(?:NS)?\b|\bsetAttributeNode(?:NS)?\b", code):
            out.append("forbidden: an attribute node (createAttribute, setAttributeNode): use setAttribute")
    return list(dict.fromkeys(out))


def read(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    page.close()
    return page


def _attempt(html: str) -> _Page | str:
    """The page read, or the class of the failure that stopped it. The parser itself takes any text: a
    failure here is a bug in `_Page`, and the page is refused (`problems`: "the page doesn't parse") rather
    than read wrong. Every guard below fails closed through this one place."""
    try:
        return read(html)
    except Exception as exc:
        return exc.__class__.__name__


def parse_error(html: str) -> str | None:
    """Why the page can't be read (the failure's class, for the caller's log), or None."""
    tried = _attempt(html)
    return tried if isinstance(tried, str) else None


def _parsed(html: str) -> _Page | None:
    tried = _attempt(html)
    return None if isinstance(tried, str) else tried


def code_of(html: str) -> str:
    """The page without its text nodes and comments (never run): its tags, attributes (decoded), scripts and
    styles, as the parser reads them (a regex over the raw page let a '<!--' in a script, a
    '</script x>' or a '>' in an attribute hide code, and an attribute's '&#101;' spell fetch)."""
    page = _parsed(html)
    return html if page is None else "\n".join(page.code)  # `problems` refuses a page that doesn't parse


def problems(html: str) -> list[str]:
    """RAIL: why this page may not be sent ([] when it may): size, parse, the forbidden APIs."""
    out: list[str] = []
    if not html.strip():
        return ["the page is empty"]
    page = _parsed(html)  # one that doesn't parse is refused below
    size = len((html if page is None else seal(html)).encode())  # the phone takes the sealed page to HTML_MAX
    if size > HTML_MAX:
        return [f"the page is {size} bytes (at most {HTML_MAX})"]
    if _DIFFERENTIAL.search(html):
        out.append(
            "forbidden: a comment or declaration a browser reads otherwise (<!-->, <![CDATA[, <!…>, <?…>)"
        )
    stripped = code_of(html)
    for name in _NAMESPACES:
        stripped = stripped.replace(name, "")
    stripped = _BUNDLED_IN_CODE.sub("", stripped)  # the bundle's own addresses ('//' in them is no address)
    for why, pattern in _FORBIDDEN:
        if pattern.search(stripped):
            out.append(f"forbidden: {why}")
    for why, pattern in _LOOKALIKE:
        if pattern.search(html):
            out.append(f"forbidden: {why}")
    if page is None:
        return [*out, "the page doesn't parse"]
    if page.open:
        out.append(f"the page doesn't parse: a <{page.open[-1]}> is never closed")
    for code in page.scripts:
        if not script_parses(code):
            out.append("the page doesn't parse: a script's brackets, strings or comments don't close")
            break
    out += list(dict.fromkeys(page.bad))
    out += built_in_code([_unescaped(code) for code in page.scripts])
    return out


def libraries(html: str) -> list[dict[str, str]]:
    """The bundled libraries the page loads, as the wire names them."""
    page = _parsed(html)
    if page is None:
        return []
    return [{"name": n, "version": v} for n, v in dict.fromkeys(page.libraries)]


# only what may come before the CSP, anchored at the start (a doctype, <html>, <head> with no
# quoted attribute): a script before <head>, or a '<head>' inside an attribute or a comment, came first before
_META_AT = re.compile(r'<meta http-equiv="Content-Security-Policy" content="[^"<>]*">')
_BUNDLED_IN_CODE = re.compile(
    r"pisa-bundle://lib/(?:d3|mermaid|vega|vega-lite|vega-embed|vega-interpreter)\.min\.js(?=['\"\s>])"
)
# HTML's own white space only, never Python's \s: before the doctype a no-break space or
# U+001C is text, so the browser opens <body> there and ignores a CSP meta that comes after it
_WS = r"[\t\n\f\r ]*"
_PREFIX = re.compile(
    rf"\A\ufeff?{_WS}(?:<!doctype[^>]*>{_WS})?(?:<html\b[^>\"']*>{_WS})?(?:<head\b[^>\"']*>)?", re.IGNORECASE
)


def _hash(body: str) -> str:
    """A script's CSP hash, of its text as the DOM holds it (the tokenizer turns CR LF and CR into LF, NUL
    into U+FFFD)."""
    text = body.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\ufffd")
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def csp(html: str) -> str:
    """RAIL: the page's own policy: the brief's, with each inline script allowed by its
    hash (in SVG a script's character references are decoded: both forms), never 'unsafe-inline'."""
    page = read(html)
    hashes = dict.fromkeys(_hash(form) for body in page.inline for form in (body, html_text.unescape(body)))
    return " ".join([CSP_HEAD, *hashes, CSP_TAIL])


def csp_meta(html: str) -> str:
    return f'<meta http-equiv="Content-Security-Policy" content="{csp(html)}">'


def seal(html: str) -> str:
    """The page with its strict CSP as its first meta: right after its doctype, <html> and <head>,
    before anything else (a <meta> there opens the <head> when the page has none)."""
    at = _PREFIX.match(html)
    end = at.end() if at is not None else 0
    return html[:end] + csp_meta(html) + html[end:]


def unseal(html: str) -> str:
    """The page as Pisa wrote it: a sealed page without the CSP meta `seal` put first (a page that isn't
    sealed comes back as it is)."""
    at = _PREFIX.match(html)
    end = at.end() if at is not None else 0
    found = _META_AT.match(html, end)
    return html if found is None else html[:end] + html[found.end() :]


def sealed(html: str) -> bool:
    """The page carries its own CSP (each inline script's hash, nothing else) before anything else."""
    bare = unseal(html)
    return bare != html and seal(bare) == html


# --- the data rule: only what this turn saw -----------------------------------------------------------------
_IDENT = re.compile(rf"^[A-Za-z_][A-Za-z0-9_]{{0,{KEY_MAX - 1}}}$")


def leaves(value: Any, path: str = "") -> list[tuple[str, Any]]:
    """(path, value) of every leaf, and of every key that isn't a plain identifier (a key can carry text)."""
    out: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}.{k}" if path else str(k)
            if not _IDENT.match(str(k)):
                out.append((f"{path or '$'} key", str(k)))
            out += leaves(v, here)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out += leaves(v, f"{path}[{i}]")
    else:
        out.append((path or "$", value))
    return out


def _number_forms(value: float | int) -> list[str]:
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return [str(int(value))]
    text = repr(value)
    return [text, text.replace(".", ",")]


def corpus(texts: list[str]) -> str:
    return " " + " | ".join(_key(t) for t in texts if t) + " "


def unseen(data: Any, seen: str) -> list[str]:
    """RAIL: the paths of `data` whose value no tool result of this turn and none of their words
    carry (`seen`: `corpus` of them). A text is found as whole words; a number as itself; booleans, null and
    small integers (indices, levels) are structure."""
    out: list[str] = []
    for path, value in leaves(data):
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            if isinstance(value, float) and not math.isfinite(value):
                out.append(path)
                continue
            if isinstance(value, int) and abs(value) <= SMALL_INT:
                continue
            if not any(f" {_key(form)} " in seen for form in _number_forms(value)):
                out.append(path)
            continue
        text = _key(str(value))
        if text and f" {text} " not in seen:
            out.append(path)
    return out


def data_problems(data: Any, seen: str) -> list[str]:
    if not isinstance(data, dict):
        return ["data is a JSON object (window.PISA_DATA)"]
    size = len(json.dumps(data, default=str).encode())
    if size > DATA_MAX:
        return [f"data is {size} bytes (at most {DATA_MAX})"]
    missing = unseen(data, seen)
    if missing:
        listed = ", ".join(missing[:6]) + (f" and {len(missing) - 6} more" if len(missing) > 6 else "")
        return [
            f"data values no result of this turn or their words carry: {listed} (only what this turn saw)"
        ]
    return []


# --- outside text never in the code -------------------------------------------------------------------------
_NAME = re.compile(r"(?:[A-ZÀ-ÖØ-Þ][\w'\u2019\-]+(?:\s+(?:d[aeiou]l?l?[aeo]?|di|da|de|of|the|&)?\s*)?){2,}")


def names_in(texts: list[str]) -> set[str]:
    """Proper names in outside text (two or more capitalised words: a place, a person, a title)."""
    found: set[str] = set()
    for text in texts:
        for m in _NAME.finditer(text or ""):
            k = _key(m.group(0))
            if len(k.replace(" ", "")) >= NAME_MIN_CHARS and len(k.split()) >= 2:
                found.add(k)
    return found


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = _key(text).split()
    return {tuple(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def outside_in_code(html: str, outside: list[str], names: list[str], words: str) -> int:
    """RAIL: how many of the page's texts (text nodes, attribute values, comments, the
    scripts' strings) carry outside text: a run of SHINGLE words of it, or a name it gives (a place, a
    sender, a title), that their own words don't carry. 0: the code is Pisa's own."""
    page = _parsed(html)
    if page is None:
        return 0  # `problems` refuses a page that doesn't parse
    runs = [
        *page.texts,
        *(_unescaped(s) for code in [*page.scripts, *page.styles] for s in _strings_in(code)),
    ]
    his = f" {_key(words)} "
    shingles: set[tuple[str, ...]] = set()
    for text in outside:
        shingles |= _shingles(text)
    shingles = {s for s in shingles if f" {' '.join(s)} " not in his}
    known = {k for k in names_in(outside) | {_key(n) for n in names if n} if k and f" {k} " not in his}
    known = {k for k in known if len(k.replace(" ", "")) >= NAME_MIN_CHARS}
    hits = 0
    for run in runs:
        text = f" {_key(run)} "
        if text.strip() == "":
            continue
        if any(f" {k} " in text for k in known) or (_shingles(run) & shingles):
            hits += 1
    return hits


# --- "Da quello che so": a drawing of what Pisa knows ---------------------------------------------------
Basis = Literal["knowledge"]
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)*(?![\w])")
_PROSE_WORD = re.compile(r"^[^\W\d_]{2,}[.,;:!?]?$")


def _prose(text: str) -> bool:
    """A script's string that reads as words (two or more words of letters), not a coordinate or a style."""
    return sum(1 for w in text.split() if _PROSE_WORD.match(w)) >= 2


def knowledge_problems(html: str, kind: str, seen: str) -> list[str]:
    """RAIL: a drawing from Pisa's own knowledge (no fetched data this turn) is allowed, captioned
    "Da quello che so" on the phone, but numbers and charts come only from this turn's data: no chart, and no
    number in what they read (text nodes, titles and labels, a script's sentences) that `seen` doesn't carry
    (small integers, a step's number, are structure)."""
    if kind == "chart":
        return ["a chart from Pisa's own knowledge: charts are drawn only from this turn's results (fetch "
                "the numbers first, or draw a schema)"]  # fmt: skip
    page = _parsed(html)
    if page is None:
        return []  # `problems` refuses a page that doesn't parse
    texts = [*page.words, *(s for code in page.scripts for s in _strings_in(code) if _prose(_unescaped(s)))]
    numbers = {
        m.group(0)
        for text in texts
        for m in _NUMBER.finditer(html_text.unescape(_unescaped(text)))
        if not (m.group(0).isdigit() and int(m.group(0)) <= SMALL_INT)
    }
    untraced = sorted(n for n in numbers if f" {_key(n)} " not in seen)
    if untraced:
        return [
            f"{len(untraced)} number(s) in the page's words from Pisa's own knowledge (numbers come only "
            "from this turn's results or their words: fetch them, or leave them out)"
        ]
    return []


# --- their words
# ---------------------------------------------------------------------------------------------- A request a
# picture serves: to draw, to see how something works, a schema, a chart, a timeline, a visual comparison.
# Offered the tool at once; the skill teaches when a picture beats words.
_ASKS = re.compile(
    r"\b(?:disegn\w*|schema\w*|diagramm\w*|grafic[oi]|mappa concettuale|flowchart|diagram\w*|draw\w*|"
    r"chart\w*|"
    r"graph\w*|visualizz\w*|visuali[sz]\w*|timeline|linea del tempo|come funziona|spiegami come|"
    r"how (?:does|do|it) \w+ works?|how \w+ works|explain how|fammi vedere come|mostrami come|show me how|"
    r"confronta\w* (?:visivamente|graficamente|su un grafico)|compare \w+ (?:visually|on a chart))",
    re.IGNORECASE,
)
# Their words for another view of the last drawing ("Fammelo vedere per X / dal punto di vista di Y /
# più semplice"): only when the conversation keeps one.
_REDRAWS = re.compile(
    r"\b(?:fammel[oaie] vedere|mostramel[oaie]|ridisegn\w*|rifall[oaie]|dal punto di vista|più semplice|"
    r"più semplici|semplificalo|in un altro modo|in modo diverso|per (?:un|una|i|gli|le|il|la) \w+|"
    r"show it (?:for|as|from)|from the point of view|simpler|redraw\w*|another way|differently)\b",
    re.IGNORECASE,
)


def asks(words: str) -> bool:
    return bool(_ASKS.search(words or ""))


def redraws(words: str) -> bool:
    return bool(_REDRAWS.search(words or ""))


# --- "ask Pisa about this" (the only way out) ---------------------------------------------------------
# control characters, zero-width spaces and direction marks (joiners stay: emoji sequences, as on the phone)
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
ASK_WORDS = {"it-IT": "Sul disegno: {label}", "en-US": "On the drawing: {label}"}
ASK_MARK = re.compile(r"^(?:Sul disegno|On the drawing): ")


def graphemes(text: str) -> list[str]:
    """The text as user-perceived characters (Unicode extended grapheme clusters, `regex`'s \\X): what Swift's
    `count` counts on the phone (Hangul jamo, Indic conjuncts, Thai marks count once)."""
    return list(regex.findall(r"\X", text))


def ask_label(label: str) -> str:
    """The tap's label as one plain line (≤ ASK_MAX user-perceived characters): no control characters."""
    return "".join(graphemes(" ".join(_CONTROL.sub(" ", label or "").split()))[:ASK_MAX])


def ask_words(label: str, lang: str) -> str:
    return ASK_WORDS.get(lang, ASK_WORDS["en-US"]).format(label=ask_label(label))


def summary_problems(summary: str) -> list[str]:
    text = " ".join((summary or "").split())
    if not text:
        return ["a summary (1-3 plain sentences: VoiceOver reads it, Pisa says its lead)"]
    if len(text) > SUMMARY_MAX:
        return [f"the summary is {len(text)} characters (at most {SUMMARY_MAX})"]
    if len([s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]) > SUMMARY_SENTENCES:
        return [f"the summary has more than {SUMMARY_SENTENCES} sentences"]
    if "<" in text or "://" in text:
        return ["the summary is plain text (no markup, no address)"]
    return []
