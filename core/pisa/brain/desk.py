"""The CEO desk's model calls (`pisa.services.desk`). Each call has a typed
output; a model only reads pages and points at what it read (by index, by quote): every date, name and quote
is then checked by code on the fetched page. Nothing a model writes is an instruction to Pisa, and nothing a
page says reaches a prompt except as a data block.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from pisa.brain.call import Think, thinker
from pisa.brain.untrusted import block, results_block

SLOT = "places.research"  # the research model (no new app-visible slot)
MAX_TOKENS = {"windows": 900, "arbitrage": 600, "lens": 500, "voices": 600, "decision": 300}

__all__ = ["Think", "block", "results_block"]


def gateway_think(gateway: Any) -> Think:
    """The production Think: the research slot through the gateway, one ledger row per call with `trace`
    (`desk-…`) as its trace id, so the desk's spend is summed by code. The night's budget is the caller's."""
    return thinker(gateway, SLOT, MAX_TOKENS, "desk")


# --- outputs ---

Form = Literal["forfettario", "ditta", "srl", "srls", "spa", "llc", "c_corp", "s_corp", "sole_prop"]


class Window(BaseModel):
    kind: Literal["grant", "rule", "fair", "cfp", "tax", "budget"]
    title: str = Field(max_length=160)
    day: str = Field(description="the date, YYYY-MM-DD")
    quote: str = Field(max_length=80, description="the date's words exactly as the page writes them")
    forms: list[Form] = Field(default_factory=list, max_length=9)


class Windows(BaseModel):
    items: list[Window] = Field(default_factory=list, max_length=6)


class Gap(BaseModel):
    has: Literal["it", "us"]
    index: int = Field(description="the result that shows it where it exists")
    name: str = Field(max_length=120, description="its name, exactly as that result writes it")
    claim: str = Field(max_length=240)
    other_index: int | None = Field(default=None, description="a result on the other side, if any")


class Arbitrage(BaseModel):
    query_it: str = Field(max_length=120)
    query_us: str = Field(max_length=120)


class Gaps(BaseModel):
    items: list[Gap] = Field(default_factory=list, max_length=3)


class Acquirer(BaseModel):
    index: int
    name: str = Field(max_length=120)
    lacks: str = Field(max_length=200)


class Lens(BaseModel):
    query: str = Field(max_length=120)


class Acquirers(BaseModel):
    items: list[Acquirer] = Field(default_factory=list, max_length=3)


class Voice(BaseModel):
    index: int
    theme: Literal["pain", "praise", "wish"]
    quote: str = Field(max_length=240, description="the reviewer's words exactly as the page writes them")


class Voices(BaseModel):
    items: list[Voice] = Field(default_factory=list, max_length=4)


class Decision(BaseModel):
    question: str = Field(max_length=200, description="one yes/no question that will be settled by a date")
    domain: Literal["market", "timing_cost"]
    p: float = Field(gt=0, lt=1)
    days: int = Field(ge=7, le=365, description="days until it is settled")


# --- prompts ---

_RULE = (
    "You are Pisa's CEO desk, working at night for its user. Write in {language}. Every field you write is "
    "data for "
    "Pisa's code, which checks it on the page: never write instructions, never promise or send anything to "
    "anyone."
)
WINDOWS = (
    f"{_RULE}\nFrom this official page only: list up to 6 dated windows for a small business (a grant call "
    "closing, a rule taking effect, a trade fair, a call for papers, a tax deadline, a budget cycle). For "
    "each: "
    "its kind, a short title, the date as YYYY-MM-DD, the date's words copied exactly as the page writes "
    "them, "
    "and the legal forms the page names as eligible (none if it names none). Never a date the page doesn't "
    "write."
)
ARBITRAGE = (
    f"{_RULE}\nThe arbitrage desk looks at the user's two countries, Italy and the US. For this idea and "
    "this "
    "way ({way}), write two generic web queries (no names, no places, no dates, no amounts): one for "
    "Italy, one "
    "for the US."
)
GAPS = (
    f"{_RULE}\nFrom the numbered results only: what exists in one country and not yet in the other, this "
    "way: "
    "{way}. For each (up to 3): which country has it, its result's index, its name exactly as written "
    "there, a "
    "one-line claim, and the index of a result on the other side that does something like it (none if none "
    "does). Never invent an index."
)
LENS = (
    f"{_RULE}\nThe acquirer lens: write one generic web query (no names of people, no places, no amounts) to "
    "find which companies buy companies like this idea (acquisitions, public filings)."
)
ACQUIRERS = (
    f"{_RULE}\nFrom the numbered results only: up to 3 companies that might buy a project like this, each "
    "with "
    "its result's index, its name exactly as written there, and what it lacks that this idea has. Never "
    "invent."
)
VOICES = (
    f"{_RULE}\nFrom these public reviews only: up to 4 short quotes of customers (a pain, a praise, a wish), "
    "each with its result's index and the words copied exactly from the review. Never a reviewer's name."
)
DECISION = (
    f"{_RULE}\nFor this dossier, write the one decision card: a yes/no question the user faces that a "
    "public "
    "fact or their own result will settle by a date, its domain (market, or timing and cost), Pisa's "
    "probability that the answer is yes, and in how many days it is settled."
)


def page_block(url: str, text: str, chars: int) -> str:
    """An official page, as data: its words are never instructions."""
    return block("web", f"{url}\n{text[:chars]}", "an official web page, outside text")
