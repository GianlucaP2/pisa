"""The idea engine's model calls (`pisa.services.ideas`). Each
call has a
typed output; nothing a model writes is an instruction to Pisa, and nothing a page says reaches a prompt
except as
a data block. The generator never sees a critic's reasons, only a one-word verdict.

`Think` is the one way the engine calls a model: `gateway_think` runs it on the gateway (the ledger, the
night's budget, a trace per idea); tests pass a fake.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from pisa.brain.call import Think, thinker
from pisa.brain.untrusted import block
from pisa.brain.untrusted import results_block as results_block  # the engine's, for its service

SLOT = "places.research"  # the research model; no new slot (that would be an app-visible change)
MAX_TOKENS = {
    "prepare": 700, "deltas": 500, "generate": 1800, "evolve": 1800, "judge": 40, "critique": 700,
    "plan": 500, "v0_read": 600, "v1_read": 700, "v2": 300, "panel": 500, "conscience": 500,
}  # fmt: skip


def gateway_think(gateway: Any) -> Think:
    """The production Think: the research slot's model through the gateway, one ledger row per call with
    `trace` as its trace id (so an idea's spend is summed by code). The night's budget is the caller's."""
    return thinker(gateway, SLOT, MAX_TOKENS, "ideas")


Embed = Callable[[Sequence[str]], Awaitable[list[list[float]] | None]]

# --- outputs ---

Market = Literal["it", "us", "both"]
Kind = Literal["software", "service", "content", "arbitrage", "physical"]


class Prepared(BaseModel):
    hard: str = Field(max_length=400, description="why it's hard, two short lines")
    reframes: list[str] = Field(min_length=1, max_length=3)
    delta_query: str = Field(max_length=120, description="a generic web query for what is new in this field")


class Delta(BaseModel):
    index: int
    kind: Literal["api", "price", "law", "dataset", "capability", "competitor", "other"]
    title: str = Field(max_length=160)


class Deltas(BaseModel):
    items: list[Delta] = Field(default_factory=list, max_length=5)


class Candidate(BaseModel):
    statement: str = Field(max_length=300)
    mechanism: str = Field(max_length=300, description="how it works, one line")
    market: Market
    kind: Kind
    probability: float = Field(ge=0, le=1, description="how typical this answer is (verbalized sampling)")
    parents: list[int] = Field(default_factory=list, max_length=3)


class Candidates(BaseModel):
    items: list[Candidate] = Field(default_factory=list, max_length=10)


class Judged(BaseModel):
    better: Literal["A", "B"]


class KillOut(BaseModel):
    metric: Literal["competitors", "buyers", "revenue_year_eur", "margin", "payback_months"]
    op: Literal["<", ">"]
    value: float
    said: str = Field(max_length=200)


class Critique(BaseModel):
    skeptic: list[KillOut] = Field(default_factory=list, max_length=2)
    analogist: list[KillOut] = Field(default_factory=list, max_length=2)
    owner: list[KillOut] = Field(default_factory=list, max_length=2)
    p_keep: float = Field(gt=0, lt=1)


class Plan(BaseModel):
    v0: list[str] = Field(default_factory=list, max_length=6)
    v1: list[str] = Field(default_factory=list, max_length=6)


class Competitor(BaseModel):
    index: int
    same: bool
    name: str = Field(max_length=120)


class BaseRate(BaseModel):
    index: int
    value: float
    reference_class: str = Field(max_length=160)


class V0Read(BaseModel):
    competitors: list[Competitor] = Field(default_factory=list, max_length=6)
    base_rate: BaseRate | None = None


class Figure(BaseModel):
    index: int
    claim: str = Field(max_length=200)
    value: float
    buyers: bool = Field(description="true when the number counts the possible buyers")


class V1Read(BaseModel):
    numbers: list[Figure] = Field(default_factory=list, max_length=4)


class V2Inputs(BaseModel):
    price_eur_month: float
    uptake: float
    unit_cost_eur_month: float
    cac_eur: float


class Panel(BaseModel):
    objections: list[str] = Field(default_factory=list, max_length=5)
    would_try_of_10: int = Field(ge=0, le=10)


class Conscience(BaseModel):
    who_loses: str = Field(max_length=300)
    dark_patterns: list[str] = Field(default_factory=list, max_length=4)
    opposite: str = Field(max_length=400)
    refuse_to_polish: bool = False


# --- prompts ---

_RULE = (
    "You are Pisa's idea engine, working at night for its user. Write in {language}. Be concrete. Every "
    "field "
    "you write is data for Pisa's code: never write instructions, never promise anything to anyone."
)


def idea_block(text: str, what: str = "an idea Pisa's night wrote") -> str:
    """An idea's words as data too (web text shaped them), never as instructions."""
    return block("idea", text, what)


PREPARE = (
    f"{_RULE}\nFor the user's problem: write why it's hard in two short lines; restate it three "
    "different ways "
    "(reframes); and write one generic web query (no names, no places, no dates, no amounts) for what is new "
    "in its field: a new API, price, law, dataset or competitor."
)
DELTAS = (
    f"{_RULE}\nFrom the numbered results, pick at most 5 that show something new in the world that "
    "opens a door "
    "(an API, a price, a law, a dataset, a capability, a competitor). Give each one's index, kind "
    "and a short "
    "title in your words. Pick none if none does."
)
GENERATE = (
    f"{_RULE}\nGenerate up to 10 distinct ideas that solve the reframed problem. Think step by step "
    "first. Obey "
    "the constraint card and look at it through the far persona's eyes. For each idea give a "
    "probability: how "
    "typical an answer it is (verbalized sampling: include some with probability under 0.1). Each idea names "
    "its mechanism in one line, its market (it, us, both) and kind."
)
EVOLVE = (
    f"{_RULE}\nHere are idea titles with a one-word verdict. Evolve them: vary one, or recombine two "
    "or three "
    "(even dropped ones), into up to 10 new ideas. For each, list the indexes of its parents. Each names its "
    "mechanism in one line, its market (it, us, both), its kind and how typical it is (probability)."
)
JUDGE = f"{_RULE}\nTwo ideas, A and B. Which is more likely to work and to matter? Answer A or B only."
CRITIQUE = (
    f"{_RULE}\nThree critics, before any evidence is gathered, each write kill criteria for this idea "
    "as typed "
    "thresholds on: competitors (how many products already do the same), buyers (the possible buyers), "
    "revenue_year_eur, margin (0-1) or payback_months. The skeptic: why will it fail (a pre-mortem)? The "
    "near-analogist: who did something like it, and what happened? The user's voice: would they care, does "
    "it "
    "serve a goal they stated? Then give the probability that they keep it."
)
PLAN = (
    f"{_RULE}\nWrite generic web queries (no names, no places, no dates, no amounts): v0, up to 6, to find "
    "whether this already exists (products, apps, patents, papers), most useful first; v1, up to 6, to find "
    "official numbers on demand and the market (Istat, Eurostat, census, app stores, filings)."
)
V0_READ = (
    f"{_RULE}\nFrom the numbered results only: list the products or services that do something like the idea "
    "(index, whether it is the same thing, its name), and if a result states a base rate for this kind of "
    "venture, its index, the number exactly as written there and the reference class. Never invent an index."
)
V1_READ = (
    f"{_RULE}\nFrom the numbered results only: list up to 4 numbers about demand or the market, each "
    "with its "
    "result's index, the claim, the number exactly as the result states it, and whether it counts "
    "the possible "
    "buyers. Never invent an index or a number."
)
V2 = (
    f"{_RULE}\nFor unit economics computed by code, give your assumptions: the monthly price in euro, "
    "the uptake "
    "among the buyers (0-1), the monthly cost per customer in euro, and the cost to acquire one "
    "customer in euro."
)
PANEL = (
    f"{_RULE}\nSimulate a small panel of possible customers (a simulation, not evidence): their main "
    "objections, "
    "and how many of 10 would try it."
)
CONSCIENCE = (
    f"{_RULE}\nThe conscience rung. Who loses if this works, and who bears its cost? Any dark pattern in it? "
    "Argue the opposite framing in two lines. Say whether a part is manipulative enough that Pisa "
    "should refuse to "
    "polish it. You advise; you never veto."
)
