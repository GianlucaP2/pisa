"""The quote cutter: what the sender just wrote, and the history under it. Synthetic mail only."""

import base64
from typing import Any

import pytest

from pisa.connectors.google.gmail import parse_message
from pisa.domain.quote_cut import CUT_VERSION, cut_html, cut_text

GMAIL_REPLY = (
    '<div dir="ltr">Thanks, I will send the parcel on Monday.</div><br>'
    '<div class="gmail_quote"><div dir="ltr" class="gmail_attr">On Tue, 4 Nov 2025 at 10:00, '
    "Dana Test &lt;dana@example.com&gt; wrote:<br></div>"
    '<blockquote class="gmail_quote" style="margin:0px 0px 0px 0.8ex">Please refund my order AB-1042.'
    "<blockquote>Earlier: the shoes are too small.</blockquote></blockquote></div>"
)


def test_quote_cut() -> None:
    assert CUT_VERSION >= 1
    # --- HTML: a Gmail reply, nested quotes and all
    new_markup, quoted_markup = cut_html(GMAIL_REPLY)
    assert "Thanks, I will send the parcel on Monday." in new_markup
    assert "refund" not in new_markup and "wrote" not in new_markup
    assert "AB-1042" in quoted_markup and "too small" in quoted_markup
    # a blockquote on its own (Apple Mail), the header outside it is the text cutter's
    apple = (
        "<div>Yes, done.</div><div>On 4 Nov 2025, at 10:00, Dana wrote:</div>"
        "<blockquote>Is it done?</blockquote>"
    )
    new_markup, quoted_markup = cut_html(apple)
    assert "Is it done?" not in new_markup and "Is it done?" in quoted_markup
    # Outlook: the reply header block and all that follows
    outlook = (
        '<div>Fine by me.</div><div id="appendonsend"></div><hr>'
        '<div id="divRplyFwdMsg">From: Dana</div><p>old</p>'
    )
    new_markup, quoted_markup = cut_html(outlook)
    assert "Fine by me." in new_markup and "old" not in new_markup and "old" in quoted_markup
    # a Gmail forward goes like a quote: someone else wrote it (§38), and so does a bare forward header's rest
    forward = (
        '<div>See below.</div><div class="gmail_quote"><div class="gmail_attr">---------- Forwarded message '
        "---------<br>From: Shop</div>Your order shipped.</div>"
    )
    new_markup, quoted_markup = cut_html(forward)
    assert new_markup == "<div>See below.</div>" and "Your order shipped." in quoted_markup
    bare = "<p>FYI</p><p>---------- Forwarded message ---------<br>From: Shop</p><p>Your order shipped.</p>"
    new_markup, quoted_markup = cut_html(bare)
    assert new_markup == "<p>FYI</p>" and "Your order shipped." in quoted_markup
    # an unclosed quote runs to the end; nothing quoted, nothing cut
    assert cut_html("<p>plain</p>") == ("<p>plain</p>", "")
    assert cut_html("<p>a</p><blockquote>b")[0] == "<p>a</p>"

    # --- plain text: the header with its > lines, in English and Italian
    english = (
        "Sounds good.\n\nOn Tue, 4 Nov 2025 at 10:00, Dana Test <dana@example.com> wrote:\n"
        "> Please refund AB-1042.\n> Thanks"
    )
    cut = cut_text(english)
    assert cut.new_text == "Sounds good."
    assert "AB-1042" in cut.quoted and "wrote:" in cut.quoted
    assert cut.cut_version == CUT_VERSION
    italian = (
        "Va bene, lo spedisco lunedì.\n\nIl giorno mar 4 nov 2025 alle ore 10:00 Dana Test "
        "<dana@example.com> ha scritto:\n> Mi rimborsa l'ordine?"
    )
    cut = cut_text(italian)
    assert cut.new_text == "Va bene, lo spedisco lunedì." and "rimborsa" in cut.quoted
    # a header wrapped over two lines
    wrapped = "OK\n\nOn Tue, 4 Nov 2025 at 10:00, Dana Test\n<dana@example.com> wrote:\n> hello"
    assert cut_text(wrapped).new_text == "OK"
    # an inline reply: what is written under the quoted block is new
    inline = "On Tue, Dana wrote:\n> Do you ship abroad?\nYes, we do.\n> And the cost?\nFive euros."
    cut = cut_text(inline)
    assert "Yes, we do." in cut.new_text and "Five euros." in cut.new_text
    assert "Do you ship abroad?" in cut.quoted and "And the cost?" in cut.quoted
    # Original Message: everything after it
    original = (
        "Received.\n\n-----Original Message-----\nFrom: Dana\nSent: Tuesday\nSubject: refund\n\n"
        "Please refund."
    )
    cut = cut_text(original)
    assert cut.new_text == "Received." and "Please refund." in cut.quoted
    # a header with no > lines under it: the rest is the history
    bare = "Thanks.\nOn Tue, 4 Nov 2025, Dana wrote:\nPlease refund."
    assert cut_text(bare) == cut_text(bare) and cut_text(bare).new_text == "Thanks."
    # stray > lines
    assert cut_text("hi\n> old line\n> older\nbye").new_text == "hi\nbye"
    # a forward header takes everything after it (en, it); a plain mail is returned whole
    for header in ("---------- Forwarded message ---------", "---------- Messaggio inoltrato ---------",
                   "Begin forwarded message:"):  # fmt: skip
        cut = cut_text(f"Thanks, see below.\n\n{header}\nFrom: Shop\nShipped.")
        assert cut.new_text == "Thanks, see below." and "Shipped." in cut.quoted, header
    assert cut_text("Just text.").new_text == "Just text." and cut_text("Just text.").quoted == ""
    assert cut_text("").new_text == ""
    # "On the other hand ..." is not a header
    assert cut_text("On the other hand, he wrote: nothing.\nbye").quoted == ""


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _gmail(parts: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1762250000000",
        "labelIds": ["INBOX"],
        "snippet": "s",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "Shop <orders@example.com>"},
                {"name": "To", "value": "owner@example.org"},
                {"name": "Subject", "value": "Re: refund"},
            ],
            "parts": [{"mimeType": mime, "body": {"data": _b64(text)}} for mime, text in parts],
        },
    }


def test_quote_cut_in_the_fetch_path() -> None:
    """parse_message cuts the HTML before it becomes text, and leaves `body` as it was."""
    both = parse_message(
        _gmail(
            [
                ("text/plain", "Thanks, shipping Monday.\n\nOn Tue, Dana wrote:\n> refund AB-1042"),
                ("text/html", GMAIL_REPLY),
            ]
        )
    )
    assert both.new_text == "Thanks, I will send the parcel on Monday."
    assert both.quoted is not None and "AB-1042" in both.quoted and "too small" in both.quoted
    assert both.cut_version == CUT_VERSION
    assert both.body is not None and "AB-1042" in both.body  # the old body is untouched
    plain_only = parse_message(_gmail([("text/plain", "Shipped!\n\n> old\n> older")]))
    assert plain_only.new_text == "Shipped!" and plain_only.quoted == "> old\n> older"
    no_quote = parse_message(_gmail([("text/plain", "Order AB-1042 is on its way.")]))
    assert no_quote.new_text == "Order AB-1042 is on its way." and no_quote.quoted == ""
    empty = parse_message({**_gmail([]), "payload": {"headers": []}})
    assert empty.new_text is None and empty.cut_version == 0


@pytest.mark.parametrize("markup", ["", "<", "<div", "<blockquote></blockquote>", "</div></div>"])
def test_quote_cut_survives_broken_html(markup: str) -> None:
    new_markup, quoted_markup = cut_html(markup)
    assert isinstance(new_markup, str) and isinstance(quoted_markup, str)


def test_a_mail_made_of_dash_runs_costs_no_time_to_cut() -> None:
    """E1 QC-1 (§38): the forward pattern backtracked on a dash run, and every tag and line ran it."""
    import time

    from pisa.domain import quote_cut
    from pisa.domain.mail import BODY_MAX_CHARS

    assert quote_cut.TEXT_MAX_CHARS == 4 * BODY_MAX_CHARS
    assert quote_cut.MARKUP_MAX_CHARS == 32 * BODY_MAX_CHARS
    started = time.perf_counter()
    cut_html(("<div>" + "-" * 395 + "x</div>") * 2000)
    cut_text(("-" * 400 + "x\n") * 1000)
    assert time.perf_counter() - started < 1.0
    # the real headers still match, dashes and all
    assert cut_text("ok\n---------- Forwarded message ---------\nold").new_text == "ok"
