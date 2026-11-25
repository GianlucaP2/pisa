"""What Pisa does about the quirks of the operator's own model servers on the wire (LOCAL-1 L1,
`scripts/local-models/findings.md`). Pure: no I/O but the process's own registry of silent cuts.

- **Tool-call markup in text** (`Markup`): a server that doesn't parse a call (llama.cpp asked for
  `tool_choice: "none"`) writes it as text, `<tool_call>{…}</tool_call>`. Such markup is never text Pisa says:
  it is taken out of the text, streamed or not, and a markup left open at the end goes with it.
- **Silent truncation** (`truncated`): Ollama cuts a prompt longer than its context (4096 tokens unless
  OLLAMA_CONTEXT_LENGTH says otherwise) and answers 200 as if it had read it all; only `usage.prompt_tokens`
  shows it. A prompt the server read at less than half of Pisa's estimate is taken as cut: logged (token
  counts, never the text), kept per model for `/readyz` and the capabilities, and told to the user once.
- **Thinking off** (`thinking_off_body`): the request fields that turn a model's thinking off where the server
  reads them: llama.cpp, vLLM and SGLang pass `chat_template_kwargs.enable_thinking` to the chat template;
  Ollama reads `reasoning_effort: "none"` (and `think: false`).
"""

import time
from typing import Any

from pisa.domain.ai import CHARS_PER_TOKEN

# --- tool-call markup ---------------------------------------------------------------------------------------

# (opening, closing) markers of a tool call written as text by the chat templates in use; None: to the end
MARKUP: tuple[tuple[str, str | None], ...] = (
    ("<tool_call>", "</tool_call>"),
    ("<tool_calls>", "</tool_calls>"),
    ("<function_call>", "</function_call>"),
    ("<function=", "</function>"),
    ("<|tool_call_begin|>", "<|tool_call_end|>"),
    ("<|tool_calls_section_begin|>", "<|tool_calls_section_end|>"),
    ("<|python_tag|>", None),
    ("[TOOL_CALLS]", None),
)


class Markup:
    """A streaming filter: text in, the same text without tool-call markup out. A tail that may be the start
    of a marker is held until the next piece says (a lone "<" is said with the next piece, or at the end)."""

    def __init__(self) -> None:
        self._held = ""
        self._closing: str | None = None  # inside a markup: its closing marker ("" = to the end)
        self.found = False  # some markup was taken out

    def feed(self, text: str) -> str:
        buffer = self._held + text
        self._held = ""
        out: list[str] = []
        while buffer:
            if self._closing is not None:
                if not self._closing:  # to the end: everything goes
                    return "".join(out)
                at = buffer.find(self._closing)
                if at < 0:
                    self._held = _partial_tail(buffer, (self._closing,))
                    return "".join(out)
                buffer = buffer[at + len(self._closing) :]
                self._closing = None
                continue
            starts = [(buffer.find(o), o, c) for o, c in MARKUP if o in buffer]
            if starts:
                at, opening, closing = min(starts, key=lambda s: s[0])
                out.append(buffer[:at])
                buffer = buffer[at + len(opening) :]
                self._closing = closing or ""
                self.found = True
                continue
            tail = _partial_tail(buffer, tuple(o for o, _ in MARKUP))
            out.append(buffer[: len(buffer) - len(tail)])
            self._held = tail
            break
        return "".join(out)

    def flush(self) -> str:
        """The end of the text: a held tail that never became a marker is said; an open markup goes."""
        held, self._held = self._held, ""
        return "" if self._closing is not None else held


def _partial_tail(text: str, markers: tuple[str, ...]) -> str:
    """The longest end of `text` that is the start of a marker (held for the next piece)."""
    best = ""
    for marker in markers:
        for n in range(min(len(marker) - 1, len(text)), len(best), -1):
            if text.endswith(marker[:n]):
                best = text[-n:]
                break
    return best


def strip_markup(text: str) -> str:
    """A whole text without tool-call markup."""
    markup = Markup()
    return markup.feed(text) + markup.flush()


# --- silent truncation --------------------------------------------------------------------------------------

# a prompt read at less than this share of Pisa's estimate was cut; estimates below MIN are never judged
TRUNCATED_SHARE = 0.5
TRUNCATION_MIN_TOKENS = 1024


def wire_chars(value: Any) -> int:
    """The characters of the text a request carries (its messages or its tools, as sent): every string in
    it, but an image's or a sound's data URL (its tokens are not its characters)."""
    if isinstance(value, str):
        return 0 if value.startswith("data:") else len(value)
    if isinstance(value, dict):
        return sum(wire_chars(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return sum(wire_chars(v) for v in value)
    return 0


def truncated(sent_chars: int, prompt_tokens: int | None) -> dict[str, int] | None:
    """The cut, as token counts, when the server read far fewer prompt tokens than were sent; else None."""
    sent = sent_chars // CHARS_PER_TOKEN
    if not prompt_tokens or sent < TRUNCATION_MIN_TOKENS or prompt_tokens >= sent * TRUNCATED_SHARE:
        return None
    return {"sent_tokens": sent, "read_tokens": prompt_tokens}


_CUTS: dict[str, dict[str, Any]] = {}  # model ref → the last cut seen in this process
_TOLD: set[str] = set()  # model refs whose cut the user was told about in this process


def record_cut(ref: str, cut: dict[str, int]) -> dict[str, Any]:
    record = {**cut, "at": int(time.time())}
    _CUTS[ref] = record
    return record


def cuts() -> dict[str, dict[str, Any]]:
    """`/readyz`: each model whose server cut a prompt in this process, with its last cut's token counts."""
    return {ref: dict(record) for ref, record in _CUTS.items()}


def forget_cut(ref: str) -> None:
    """A new probe of the model: what was seen before no longer stands, and the user may be told again."""
    _CUTS.pop(ref, None)
    _TOLD.discard(ref)


def tell_once(ref: str) -> bool:
    """True the first time a model's cut is to be told to the user in this process."""
    if ref in _TOLD:
        return False
    _TOLD.add(ref)
    return True


def reset() -> None:
    """Tests: no cut seen, none told."""
    _CUTS.clear()
    _TOLD.clear()


# what the user hears, once, when the server cut what Pisa sent (a plain sentence, no model)
CUT_NOTICE = {
    "it-IT": "Una cosa: il server del mio modello ha tagliato parte di quello che gli ho mandato, quindi "
    "questa risposta potrebbe non tenerne conto. Chi gestisce il server può aumentarne il contesto.",
    "en-US": "One thing: my model server cut off part of what I sent it, so this answer may have missed "
    "something. Whoever runs the server can raise its context size.",
}


def cut_notice(lang: str) -> str:
    return CUT_NOTICE.get(lang, CUT_NOTICE["it-IT" if lang.startswith("it") else "en-US"])


# --- thinking -----------------------------------------------------------------------------------------------


def thinking_off_body(server: str | None) -> dict[str, Any]:
    """The request fields that turn thinking off on this kind of server (`ModelCaps.server`)."""
    if server == "ollama":
        return {"reasoning_effort": "none", "think": False}
    return {"chat_template_kwargs": {"enable_thinking": False}}
