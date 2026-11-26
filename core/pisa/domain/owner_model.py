"""Who the owner is in his own mail, in code. Pure rules, no I/O.

His account address, and the aliases he writes from. An alias is learned from the From header of a SENT
message, and counts only once he confirmed it (the services keep that, `services/owner_identity.py`). A
message is owner-authored when its From is the account or a confirmed alias; quoted text keeps its claimed
author, so this is only ever asked of a message's own From.
"""

from collections.abc import Iterable
from dataclasses import dataclass

SENT = "SENT"


def norm(address: str | None) -> str:
    """The address as compared: trimmed and lower-cased (an empty one stays empty)."""
    return (address or "").strip().lower()


@dataclass(frozen=True)
class OwnerModel:
    account: str
    aliases: frozenset[str] = frozenset()  # confirmed ones only

    def authored(self, from_address: str | None) -> bool:
        """The From is his account or a confirmed alias."""
        sender = norm(from_address)
        return bool(sender) and (sender == norm(self.account) or sender in self.aliases)


def alias_candidates(
    account: str, sent: Iterable[tuple[tuple[str, ...] | list[str], str | None, str | None]]
) -> dict[str, str | None]:
    """The addresses he wrote from, other than his account, in the messages that carry the SENT label:
    address -> the display name seen with it (the first one). `sent`: (labels, From address, From name)."""
    found: dict[str, str | None] = {}
    own = norm(account)
    for labels, address, name in sent:
        key = norm(address)
        if SENT not in labels or not key or "@" not in key or key == own:
            continue
        found.setdefault(key, (name or "").strip() or None)
    return found
