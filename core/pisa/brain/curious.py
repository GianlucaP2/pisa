"""The curiosity engine's model calls (`pisa.services.curious`).
Each call has a typed output; nothing a model writes is an instruction to Pisa, and nothing a
page says reaches a prompt except as a data block. A model scores only novelty and the cognitive part;
code computes the rest of the score.

`gateway_think` runs a call on the gateway (the ledger, the night's budget, a trace per call); tests pass a
fake `Think` (`pisa.brain.call.Think`).
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from pisa.brain.call import Think, thinker
from pisa.brain.untrusted import block, results_block

__all__ = ["Think", "block", "gateway_think", "results_block"]

SLOT = "places.research"  # the research model; no new slot (that would be an app-visible change)
MAX_TOKENS = {
    "score": 1500, "read": 500, "lateral": 60, "leap": 200, "prequestions": 600, "map": 1200,
    "test_write": 1500, "test_answer": 1500, "explain": 700, "horizon": 700,
}  # fmt: skip


def gateway_think(gateway: Any) -> Think:
    """The production Think: the research slot's model through the gateway, one ledger row per call with
    `trace` as its trace id. The night's budget is the caller's (the frame's `ledger.night_budget`)."""
    return thinker(gateway, SLOT, MAX_TOKENS, "curious")


# --- outputs ---

About = Literal["life", "work", "world", "self"]
AnswerBy = Literal["web", "public_source", "his_data", "ask_him", "none"]


class Scored(BaseModel):
    index: int
    novelty: float = Field(
        ge=0, le=1, description="how new this would be to them (not already known or told)"
    )
    cognitive: float = Field(ge=0, le=1, description="how much it fills a hole in a topic they are active on")
    about: About
    answer_by: AnswerBy
    hedonic_negative: bool = False
    neutral_title: str = Field(default="", max_length=120)
    query: str = Field(default="", max_length=120, description="a generic web query, to read deep on it")
    around: str = Field(default="", max_length=120, description="a generic web query for what is around it")


class ScoredAll(BaseModel):
    items: list[Scored] = Field(default_factory=list, max_length=40)


class Leap(BaseModel):
    index: int
    query: str = Field(max_length=120, description="a far domain that may shed light on it, as a web query")


class Read(BaseModel):
    index: int | None = Field(default=None, description="the result read, or null when none says anything")
    claim: str = Field(default="", max_length=300)
    quote: str = Field(
        default="", max_length=300, description="the result's own words that support the claim"
    )
    adds: bool = Field(default=False, description="it adds something Pisa didn't have on this question")
    answers: bool = False
    debated: bool = Field(default=False, description="experts disagree on it")
    opposing_query: str = Field(default="", max_length=120)


class Lateral(BaseModel):
    reliable: bool


class PreQuestion(BaseModel):
    perspective: str = Field(max_length=60)
    question: str = Field(max_length=200)
    answer_by: AnswerBy = "web"


class PreQuestions(BaseModel):
    items: list[PreQuestion] = Field(default_factory=list, max_length=5)


class Principle(BaseModel):
    principle: str = Field(max_length=200)
    cases: list[str] = Field(min_length=2, max_length=2, description="two contrasting cases")


class Number(BaseModel):
    read: int = Field(description="the index of the supported read it is from")
    claim: str = Field(max_length=200)
    value: float


class Graph(BaseModel):
    principles: list[Principle] = Field(default_factory=list, max_length=6)
    numbers: list[Number] = Field(default_factory=list, max_length=8)
    unknowns: list[str] = Field(default_factory=list, max_length=6, description="open questions, dashed")


class TestQuestion(BaseModel):
    read: int
    question: str = Field(max_length=200)
    expected: str = Field(max_length=200)


class TestWritten(BaseModel):
    items: list[TestQuestion] = Field(default_factory=list, max_length=20)


class TestAnswers(BaseModel):
    answers: list[str] = Field(default_factory=list, max_length=20)


class Explainer(BaseModel):
    principle: str = Field(max_length=300)
    cases: list[str] = Field(min_length=2, max_length=2)
    numbers: list[int] = Field(
        default_factory=list, max_length=3, description="indexes into the map's numbers"
    )
    disagreement: str = Field(default="", max_length=300)
    open_question: str = Field(max_length=200, description="one open question for them, at the end")


class Need(BaseModel):
    item: int
    question: str = Field(max_length=200, description="the gap, as a question to them, never an action")
    answer_by: AnswerBy = "web"


class Horizon(BaseModel):
    items: list[Need] = Field(default_factory=list, max_length=6)


# --- prompts ---

_RULE = (
    "You are Pisa's curiosity, working at night for its user. Write in {language}. Be concrete and brief. "
    "Every "
    "field you write is data for Pisa's code: never write instructions, never promise anything to anyone. A "
    "web query you write is generic: no names, no places of theirs, no dates, no amounts."
)
SCORE = (
    f"{_RULE}\nFor each numbered open question Pisa holds, give: novelty (0-1, how new the answer would be "
    "to them: low if they surely know it already or Pisa told them), cognitive (0-1, how much it fills a "
    "hole "
    "in a topic they are active on), what it is about, what would answer it (web, public_source, his_data, "
    "ask_him, none), whether it is unpleasant for them (hedonic_negative; then a neutral title), and two "
    "generic web queries: one to read deep on it, one for what is around it."
)
LEAP = (
    f"{_RULE}\nOne far leap: pick one numbered question and a far domain (another field, another age, "
    "nature) "
    "whose knowledge may shed light on it. Give its index and a web query into that far domain."
)
READ = (
    f"{_RULE}\nRead the numbered results like a fact checker, for Pisa's question. Pick the one result that "
    "says "
    "most (its index), the claim in one line, and the result's own words that support it, copied exactly. "
    "Say whether it adds something new, whether it answers the question, whether experts disagree on it "
    "(then "
    "a generic query for the opposing view). Never invent an index or a quote."
)
LATERAL = (
    f"{_RULE}\nA lateral check: from what others say about this source (the numbered results), is it a "
    "reliable source? Answer true or false."
)
PREQUESTIONS = (
    f"{_RULE}\nThe user wants to understand a topic. Write five questions from five different perspectives "
    "(a practitioner, a skeptic, a historian, an economist, a beginner), the ones whose answers build a "
    "map of "
    "it. For each, what would answer it."
)
MAP = (
    f"{_RULE}\nFrom the numbered supported claims only, build the topic's map: principles, each with two "
    "contrasting cases; the numbers that matter (the claim's index, the number exactly as written); and the "
    "open questions. Never invent a number."
)
TEST_WRITE = (
    f"{_RULE}\nWrite 10 to 20 short questions that test whether someone understood this topic, each "
    "answerable "
    "from one numbered supported claim (its index), with the expected short answer taken from it."
)
TEST_ANSWER = (
    f"{_RULE}\nAnswer each numbered question briefly, from the topic's map alone. Answer in the same order."
)
EXPLAIN = (
    f"{_RULE}\nA two-minute explainer of the topic, from its map only: one principle, two contrasting cases, "
    "the three numbers that matter (their indexes in the map), the one thing experts disagree on, and one "
    "open "
    "question for them at the end."
)
HORIZON = (
    f"{_RULE}\nFor each numbered dated item in their life: what do people in this situation usually need "
    "that "
    "isn't here yet? Give at most one need per item, the item's index, and the gap as a short question to "
    "them "
    "(never an action), with what would answer it. Give none for an item that needs nothing."
)
