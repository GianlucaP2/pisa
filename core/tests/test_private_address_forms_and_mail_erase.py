"""Two outbound and erase rules, each the inverse of a security proof of concept (it passed while the gap
held; these pass while the fix holds): private addresses in their NAT64 and deprecated IPv6 forms are refused,
and the invitations marked as mailed go with their mail when it is erased."""

from pisa.connectors.outbound import public
from pisa.domain import erase as erase_rules


def test_nat64_and_deprecated_ipv6_forms_of_private_addresses_are_refused() -> None:
    # 64:ff9b::/96 embeds an IPv4 address (RFC 6052): behind NAT64 these are 127.0.0.1 and 10.0.0.1
    assert not public(["64:ff9b::7f00:1"]) and not public(["64:ff9b::a00:1"])
    assert not public(["::127.0.0.1"]) and not public(["fec0::1"])  # IPv4-compatible and site-local
    assert not public(["2002:7f00:1::1"]) and not public(["100.100.100.100"])  # 6to4 and the tailnet


def test_mailed_invites_are_erased_with_their_mail() -> None:
    families = {part.table: family for family, parts in erase_rules.FAMILIES.items() for part in parts}
    assert families["mailed_invites"] == "mail" and families["email_messages"] == "mail"
