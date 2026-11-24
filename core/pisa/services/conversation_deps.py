"""Everything a conversation is built with besides its pool, gateway, owner and clock: one typed set,
handed to `ConversationService` whole by the runtime.

- `tools`: the services its turns' tools read, one per tool family (`brain.conversation.ToolFamilies`). Each
  turn's context takes them as they are, so a new tool family is a field there and a group in the brain's
  `GROUPS`, filled by the runtime; the conversation itself never names it.
- the rest: what the conversation reads itself, by code, around its turns (the lexicon, what Pisa said
  unasked, attend's folded items, the skills, the reflex, the knock and the night's voices). Some are the
  same service as a tool family, read beyond that family's port (the profile, the night's voice, the
  episodes, the connectors)."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pisa.brain.conversation import ToolFamilies
from pisa.domain import experimental
from pisa.services.unasked import Unasked

if TYPE_CHECKING:
    from pisa.services.attend import AttendService
    from pisa.services.connectors import ConnectorsService
    from pisa.services.episodes import EpisodeService
    from pisa.services.lexicon import LexiconService
    from pisa.services.profile import ProfileService
    from pisa.services.reflex import ReflexService
    from pisa.services.skills import SkillsService
    from pisa.services.voice1 import KnockService
    from pisa.services.voice2 import Voice2Service
    from pisa.services.voice3 import NightVoice
    from pisa.services.voice4 import CeoVoice
    from pisa.services.voice6 import Prurito


@dataclass(kw_only=True)
class ConversationDeps:
    tools: ToolFamilies = field(default_factory=ToolFamilies)
    # the profile's own reads: the core profile, the talk, the taste, a turn's start (`tools.profile` too)
    profile: "ProfileService | None" = None
    lexicon: "LexiconService | None" = None  # the understanding step's names
    unasked: Unasked = field(default_factory=Unasked)  # what Pisa said unasked, per device (newest wins)
    attend: "AttendService | None" = None  # what Pisa folded into the next talk
    skills: "SkillsService | None" = None  # None: the trigram index, without embeddings
    reflex: "ReflexService | None" = None  # the reflex's reads on the partial words and the shared state
    knock: "KnockService | None" = None  # the knock, the morning, il tragitto
    perceive: "Voice2Service | None" = None  # perceive then ask, Pisa's states, its traits and opinions
    night: "NightVoice | None" = None  # the night's story, the drawer, spars, bets, il quarto (`tools.night`)
    ceo: "CeoVoice | None" = None  # il consiglio, la prova generale, "sciame su questo"
    prurito: "Prurito | None" = None  # il prurito, a miss's reason answered after a knock
    episodes: "EpisodeService | None" = None  # what `recall` last answered (`tools.memory`)
    connectors: "ConnectorsService | None" = None  # which connectors are on, each turn (`tools.sources`)
    # the experimental features that are on (`domain.experimental`): an off one's tools are never offered and
    # "what can you do" never names it. The runtime passes PISA_EXPERIMENTAL's; built alone, every one is on.
    enabled: frozenset[str] = experimental.ALL
