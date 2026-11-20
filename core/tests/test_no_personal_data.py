"""No personal detail copied from real life is left in the exported tree.

Old tests and replay scenarios took values from real days. Each kind found was replaced by an invented value
of the same shape, and this scan keeps them out of the files the export copies (the list in
`test_no_precise_coordinates`, which scans the coordinates):

- **IBANs**: an Italian IBAN is the SWIFT registry's documented example for Italy or an invented one (bank
  code 99999, valid check digits); any other fails, anywhere.
- **Rome landlines**: an 06 number is in the invented range (06 0000 …, which no geographic number starts
  with) or the product's own radio-taxi number where the product's table or a test of it names it.
- **Mail addresses** in tests and fixtures are on reserved names (example.com/.net/.org, `*.example`, `.test`,
  `.invalid`, `.localhost`) or on a domain a product rule names (a sender Pisa recognises), each listed below
  with the rule; an address inside a URL's userinfo is a link test, not a mailbox.
- **Street addresses** of real places that tests once used (in Rome, Milan, Amsterdam, New York) are gone
  from tests and fixtures; invented streets took their place. Product docstrings may still name a generic
  street ("Via Roma 1") as an example.
"""

import re

from tests.test_no_precise_coordinates import exported_texts

DOCUMENTED_IBAN = "IT60X0542811101000000123456"  # the SWIFT IBAN registry's example for Italy
INVENTED_ABI = "99999"
SELF = "core/tests/test_no_personal_data.py"  # its own examples

TESTS_AND_FIXTURES = ("core/tests/", "protocol/fixtures/", "relay/tests/", "scripts/", "docs-site/")

# the product's radio taxi for Rome (pisa/domain/handoffs.py), where its table or a test of the table names it
TAXI = "063570"
TAXI_FILES = {
    "core/pisa/domain/handoffs.py",
    "core/tests/test_generic_country.py",
    "core/tests/test_world_handoffs.py",
}

# domain: the product rule that names it (a test of the rule needs the real sender's domain)
RULE_DOMAINS = {
    "amazon.it": "pisa/domain/money_links.py and people_offers.py: Amazon's order and marketplace senders",
    "amazon.com": "the same rules, Amazon's US domain",
    "ita-airways.com": "pisa/domain/money_links.py and trips.py: the airline's own sender",
    "booking.com": "pisa/domain/trips.py: a booking site's confirmations",
    "enel.it": "pisa/domain/money_back.py and money.py: a utility's bills (and its subdomains)",
    "a2a.eu": "the same rules, another utility",
    "vodafone.it": "pisa/domain/money_links.py and money.py: a telecom's bills",
    "fastweb.it": "the same rules, another telecom",
    "sky.it": "pisa/domain/money_links.py: a subscription's cancel link",
    "netflix.com": "pisa/domain/money_links.py: a subscription's cancel link",
    "email.apple.com": "pisa/domain/money_links.py: Apple's receipts",
    "esselunga.it": "pisa/domain/money_links.py: a grocer's buy-again link",
    "etsy.com": "pisa/domain/world.py: a merchant's search",
    "invitalia.it": "pisa/domain/desk.py: a public grants agency",
    "agenziaentrate.it": "pisa/domain/obligations.py: the tax agency's certified mail",
    "notifichedigitali.it": "pisa/domain/obligations.py: SEND, the public notices service",
    "gmail.com": "pisa/services/intake.py: a free-mail provider (a person, not a company)",
    "google.com": "pisa/services/foreign.py: Google Calendar's iCalUID suffix and calendar ids: no mailbox",
}
# pisa/domain/obligations.py: a Comune's own domain (comune.<capital>.it, comune.<name>.<province>.it) and the
# look-alikes its tests must refuse
PUBLIC_BODY = re.compile(r"^(?:pec\.)?comune\.[a-z-]+(?:\.[a-z]{2})?\.it$")
RESERVED = re.compile(r"(?:^|\.)(?:example|test|invalid|localhost)$|(?:^|\.)example\.(?:com|net|org)$")

IBAN = re.compile(r"\b(IT\d{2} ?[A-Z](?: ?[0-9A-Z]){22})\b")
ROME = re.compile(r"(?<![\w+/.-])(?:(?:\+|00)39[ .-]?)?(06(?:[ .-]?\d){4,8})(?![\w])")
MAIL = re.compile(r"(?<![\w.+%/:-])[\w.+%-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b(?!\.\w)")
STREETS = re.compile(
    r"\b(?:Via (?:Roma|Torino|Prenestina|Verdi|Marmorata|Cordusio|dei Coronari|Paolo Sarpi|Padova|Mazzini|"
    r"Garibaldi|Dante|Appia Nuova|Po|dei Gelsi|delle Acacie|dei Faggi|dei Cipressi|dei Carpini|"
    r"delle Magnolie|dei Gelsomini|Ravaglia)|Viale (?:Monza|degli Olmi)|Corso (?:Magenta|Como)|"
    r"Piazza (?:del Duomo|Duomo|Venezia|del Glicine|dei Pini)|Largo dei Platani|Vicolo dei Lavandai|"
    r"Kalverstraat|Museumplein|Keizersgracht|Kerkstraat|Gasplein|Wall St)\b",
    re.IGNORECASE,
)


def _line(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def _allowed_domain(domain: str) -> bool:
    domain = domain.lower()
    if RESERVED.search(domain) or PUBLIC_BODY.match(domain):
        return True
    return any(domain == d or domain.endswith("." + d) for d in RULE_DOMAINS)


def ibans(text: str) -> list[tuple[int, str]]:
    out = []
    for m in IBAN.finditer(text):
        iban = m.group(1).replace(" ", "")
        if iban != DOCUMENTED_IBAN and iban[5:10] != INVENTED_ABI:
            out.append((_line(text, m.start()), iban))
    return out


def rome_landlines(text: str, taxi_ok: bool = False) -> list[tuple[int, str]]:
    out = []
    for m in ROME.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if digits[2:6] == "0000" or (taxi_ok and digits == TAXI):
            continue
        out.append((_line(text, m.start()), m.group(1)))
    return out


def mail_domains(text: str) -> list[tuple[int, str]]:
    return [
        (_line(text, m.start()), m.group(1)) for m in MAIL.finditer(text) if not _allowed_domain(m.group(1))
    ]


def real_streets(text: str) -> list[tuple[int, str]]:
    return [(_line(text, m.start()), m.group(0)) for m in STREETS.finditer(text)]


def _scan(rel: str, text: str) -> list[str]:
    found = [f"{rel}:{n} IBAN" for n, _ in ibans(text)]
    found += [f"{rel}:{n} 06 number" for n, _ in rome_landlines(text, taxi_ok=rel in TAXI_FILES)]
    if rel.startswith(TESTS_AND_FIXTURES) and rel != SELF:
        found += [f"{rel}:{n} mail on {d}" for n, d in mail_domains(text)]
        found += [f"{rel}:{n} street" for n, _ in real_streets(text)]
    return found


def test_no_exported_file_carries_a_real_iban_phone_mail_or_street() -> None:
    found = [hit for rel, text in exported_texts() if rel != SELF for hit in _scan(rel, text)]
    assert not found, f"personal details copied from real life (invent one of the same shape): {found}"


def test_the_scan_catches_each_kind() -> None:
    assert ibans("IBAN IT12 A123 4512 3451 2345 6789 012") and not ibans(f"IBAN {DOCUMENTED_IBAN}")
    assert not ibans("IT76L9999999999000000001234")  # invented bank code
    assert rome_landlines("chiama lo 06 1234 5678") and rome_landlines("tel:+390612345678")
    assert not rome_landlines("chiama lo 06 0000 5678") and not rome_landlines("2026-06-12 alle 06:30")
    assert rome_landlines("+39063570") and not rome_landlines("+39063570", taxi_ok=True)
    assert mail_domains("scrivi a anna@negozio.it") == [(1, "negozio.it")]
    assert not mail_domains("anna@x.example, bo@example.com, noreply@ita-airways.com, t@comune.roma.it")
    assert not mail_domains("https://user:pw@wa.me/1")  # a URL's userinfo
    assert real_streets("Casa, Via Roma 1") and not real_streets("Via Immaginaria 1")
