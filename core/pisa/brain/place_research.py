"""What the web says about a place (slot places.research).

The model reads web pages and reviews and writes a short summary plus facts, each tied to a source by its
number. Pages and reviews are untrusted text: each sits in its own nonce-delimited data block, and the
agent has no tools, so whatever a page says can only end up as text in the summary.
"""

from dataclasses import dataclass

import pydantic_ai
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage

from pisa.brain.models.compat import output_spec
from pisa.brain.untrusted import data_block
from pisa.domain.lexicon.languages import name_by_tag

pydantic_ai.BANNER_ENABLED = False

SLOT = "places.research"
SUMMARY_MAX_WORDS = 120
MAX_FACTS = 8
MAX_OUTPUT_TOKENS = 1024


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(description="One fact about the place, in one sentence.")
    source: int = Field(description="The number of the source it comes from (1 = the first source).")


class Research(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(
        description=f"What the sources say about the place, at most {SUMMARY_MAX_WORDS} words."
    )
    facts: list[Fact] = Field(description=f"At most {MAX_FACTS} useful facts, each with its source number.")


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    text: str


@dataclass(frozen=True)
class PlaceFacts:
    name: str
    address: str | None
    category: str | None


def prompt(
    place: PlaceFacts, question: str | None, sources: list[Source], reviews: list[str], lang: str
) -> tuple[str, str]:
    """(instructions, user prompt). Every page and the reviews are separate data blocks."""
    rules = [
        "You research a place for the user of a personal assistant. Read the sources and write what they "
        "say about the place: what it is, what people praise or criticise, prices, tips, anything that helps "
        "decide whether and when to go.",
        f"Write in {name_by_tag(lang)}. The summary has at most {SUMMARY_MAX_WORDS} words.",
        f"Give at most {MAX_FACTS} facts; each one cites the number of the source it comes from.",
        "Use only what the sources say. If they are about a different place, say so instead of guessing.",
    ]
    # The place's name and address come from a provider too: data, like the pages.
    label = data_block(
        "place",
        place.name
        + (f", {place.address}" if place.address else "")
        + (f" ({place.category})" if place.category else ""),
        what="the place being researched",
    )
    rules.append(label.rule)
    parts = ["The place:\n" + label.text]
    if question:
        block = data_block("owner-question", question, what="the user's question, to focus the research")
        rules.append(block.rule)
        parts.append("Focus on the user's question:\n" + block.text)
    for i, source in enumerate(sources, start=1):
        block = data_block(
            f"source-{i}", f"{source.title}\n{source.url}\n\n{source.text}", what=f"web page {i}"
        )
        rules.append(block.rule)
        parts.append(f"Source {i}:\n{block.text}")
    if reviews:
        what = "reviews of the place (not a numbered source: mention them in the summary only)"
        block = data_block("reviews", "\n---\n".join(reviews), what=what)
        rules.append(block.rule)
        parts.append("Reviews:\n" + block.text)
    return "\n".join(rules), "\n\n".join(parts)


async def research(
    model: Model,
    place: PlaceFacts,
    question: str | None,
    sources: list[Source],
    reviews: list[str],
    lang: str,
) -> tuple[Research, RunUsage]:
    instructions, user = prompt(place, question, sources, reviews, lang)
    agent: Agent[None, Research] = Agent(
        model,
        output_type=output_spec(model, Research),
        instructions=instructions,
        model_settings=ModelSettings(max_tokens=MAX_OUTPUT_TOKENS),
        name="pisa-place-research",
        retries=1,
    )

    @agent.output_validator
    def _check(output: Research) -> Research:
        words = len(output.summary.split())
        if words > SUMMARY_MAX_WORDS:
            raise ModelRetry(f"The summary has {words} words; the limit is {SUMMARY_MAX_WORDS}.")
        bad = [f.source for f in output.facts if not 1 <= f.source <= len(sources)]
        if bad:
            raise ModelRetry(f"Source numbers must be 1 to {len(sources)}; got {bad}.")
        return output.model_copy(update={"facts": output.facts[:MAX_FACTS]})

    result = await agent.run(user)
    return result.output, result.usage
