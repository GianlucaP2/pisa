"""The tool effect manifest: what each of Pisa's brain tools does, derived from the tool registry
(`pisa.domain.tools`, where each tool is declared once). Pure: names only, no I/O.

- `read`: reads or shows; changes nothing.
- `own_write`: changes something of their (their calendar, tasks, profile, drafts, Pisa's own settings) and
  says so.
- `proposal`: prepares or proposes; their yes, their tap or their Face ID does it (Pisa prepares, they tap).
- `outward`: reaches someone else (an email sent, a call dialled).
- `spend`: starts or lets go on work that spends (none today).

`results.WRITE_TOOLS` (own_write, outward, spend: a change done), `claims.PROPOSES` and `receipts.RECEIPTED`
read it. RAIL: in a tainted turn `outward` and `spend` are refused centrally
(`pisa.brain.receipts.TaintedTurn`), not only inside each tool. A test builds the agent with every service and
fails when any tool lacks an entry in the registry (`tests/test_core_manifest.py`).
"""

from pisa.domain.tools import REGISTRY, TOOLS, Effect, named

__all__ = ["Effect"]

READ = named(lambda t: t.effect == "read" and not t.system)
OWN_WRITE = named(lambda t: t.effect == "own_write")
# every tool that only proposes or prepares: a hunch asked once, a claim letter they send, a
# cancel page or a bill they contest, a skill to switch on, a merge they tap, a button they open.
PROPOSAL = named(lambda t: t.effect == "proposal")
# An email that leaves (after their heard read-back), a call the phone dials (after they confirm there).
OUTWARD = named(lambda t: t.effect == "outward")
# Work that spends (none today; the gate keeps the slot).
SPEND = named(lambda t: t.effect == "spend")

# The agent's own tools, not Pisa's: the deferred tools' search (`ToolSearch`, when tools are deferred).
SYSTEM = named(lambda t: t.system)
# a write called in the same response as a read runs as
# after it (`pisa.brain.receipts.ReadBeside`), so the taint never depends on which of two parallel calls ends
# first. Fail closed: every read is outside text but these, Pisa's OWN reads (their
# lists, Pisa's own records, their profile, the screen's own objects, the agent's tool search): a read added
# later is outside until it is declared `own` (`tests/test_core_b10.py` checks none of these makes an outside
# block).
OWN_READS = named(lambda t: t.own) | SYSTEM
# The kind a read's outside text is marked with before it runs (its own block marks it again when it ends)
OUTSIDE_READS: dict[str, str] = {t.name: t.outside for t in TOOLS if t.outside is not None}


def outside_read(tool: str) -> str | None:
    """The kind of outside text a read beside a write may carry; None for Pisa's own reads and for every
    declared tool that isn't a read. A tool missing from the manifest counts as an outside read (fail
    closed)."""
    effect = MANIFEST.get(tool)
    if tool in OWN_READS or (effect is not None and effect != "read"):
        return None
    return OUTSIDE_READS.get(tool, "outside-read")


MANIFEST: dict[str, Effect] = {name: tool.effect for name, tool in REGISTRY.items()}

# The writes whose results Pisa builds itself (`results.done` / `needs_yes`); every other write gets the
# receipt.
OWN_LINE = named(lambda t: t.own_line)

# RAIL: the central gate, what a tainted turn refuses.
GATED = OUTWARD | SPEND
# Left wholly to their own rail: the send. After outside text a spoken
# "mandala" never sends (its check reads the whole taint) and the approval card stays for their tap; the gate
# would take the card away too.
OWN_RAIL = named(lambda t: t.own_rail)
# An earlier turn's outside text (still in the history) is held by these tools' own rails, not the gate's
# (none today).
OWN_PAST_RAIL: frozenset[str] = frozenset()
# Pisa's own lists that name a title someone else wrote (an invite's, an email-made task's: `invite`, the
# day's context on any day with a meeting): never the gate's taint, or no call could be dialled on such a day.
# Each tool's own rail still reads them.
LIST_TAINT = frozenset({"invite"})
# Outside text that is the tool's own subject, not a taint for it (none today).
OWN_SOURCE: dict[str, frozenset[str]] = {}

# the acts that ask their yes on a card of their own (an approval, a held change, a
# preparation they tap). One whose arguments carry an outside value asks there, the source named; any other
# write carrying one is refused (`pisa.brain.receipts.OutsideValues`).
ASKS_ON_CARD = PROPOSAL | named(lambda t: t.card)


def of(*effects: Effect) -> frozenset[str]:
    return frozenset(tool for tool, effect in MANIFEST.items() if effect in effects)


def refused(tool: str, now: list[str], past: list[str]) -> list[str]:
    """The outside text (kinds) that refuses this call in this turn, or [] when it may run. `now`: what this
    turn's own tools read; `past`: an earlier turn's reading still in the history. A tool missing from the
    manifest is refused like an outward one (fail closed)."""
    effect = MANIFEST.get(tool)
    if effect is not None and (tool not in GATED or tool in OWN_RAIL):
        return []
    own = OWN_SOURCE.get(tool, frozenset()) | LIST_TAINT
    kinds = [k for k in now if k not in own]
    if tool not in OWN_PAST_RAIL:
        kinds += [k for k in past if k not in own]
    return sorted(set(kinds))
