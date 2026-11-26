"""The admin radar (the renewals extended).
Pure rules, no I/O.

**The record** (`Obligation`): what, due, source, penalty, online channel, state. Every text is code's own,
from the report's tables: a sender's words never become an obligation's text. An amount is copied as
printed in the notice (`amount_text`, digits and currency only), never computed. No tax advice: Pisa says what
is due, when and where; never how much to declare.

**The mail classifier** (`classify`): pagoPA notices (the 18-digit code, the amount as printed, the due date),
SEND notices (legally delivered when opened or **7 days** after it was made available: `send_clock`), the
insurer's 30-day notice, the condominium, AdE / INPS. A message that looks like a renewal or an official
notice and isn't from an official sender, or asks to renew the tessera sanitaria (the Ministry never emails
about it), is **flagged, not tracked**: no reminder, no task, one line in the list. Links are never followed.

**The calendar** (`calendar`): the national dates of `§1` (2026 cycle, checked 2026-11-06), switched by their
own list (income tax, property, condominium, ISEE, car). A later year follows the same rule and says it is to
be confirmed when the AdE publishes it (`last_verified` empty).
"""

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Literal

from pisa.domain.lexicon.dates import day_or_28, day_or_none

State = Literal["upcoming", "ready", "done", "overdue", "flagged"]
Source = Literal["mail", "pec", "vault", "calendar", "bank", "ade"]

VERIFIED = date(2026, 11, 6)  # the report's sources were checked on this day
SEND_DELIVERY = timedelta(days=7)  # SEND: delivered when opened, or 7 days after it was made available

# Official senders (§4: "checks them against an allow-list of official domains"). A suffix match on the
# sender's domain; Comuni and Regioni publish under these too.
OFFICIAL_DOMAINS = (
    "agenziaentrate.gov.it",
    "agenziaentrateriscossione.gov.it",
    "inps.it",
    "pagopa.it",
    "pagopa.gov.it",
    "notifichedigitali.it",
    "ilportaledellautomobilista.it",
    "poliziadistato.it",
    "gov.it",
    "salute.gov.it",
    "postacert.inps.gov.it",
    "pec.agenziaentrate.it",
)


@dataclass(frozen=True)
class Obligation:
    kind: str
    what: str
    due_on: date | None
    source: Source
    source_ref: str
    rule_source_url: str | None = None
    penalty: str | None = None
    channel: str | None = None
    amount_text: str | None = None
    subject: str | None = None
    state: State = "upcoming"
    flag_reason: str | None = None
    remind_on: date | None = None
    last_verified: date | None = None


def state_on(o: Obligation, today: date) -> State:
    """The state today: flagged and done stay; a due day passed is overdue."""
    if o.state in ("flagged", "done"):
        return o.state
    if o.due_on is not None and o.due_on < today:
        return "overdue"
    return o.state


# --- the mail classifier ------------------------------------------------------------------------------------

_NOTICE_CODE = re.compile(r"(?<!\d)((?:\d[ ]?){17}\d)(?!\d)")
# a thousands separator is a dot or one plain space, never a line break: "€ 85\n100" is 85
_AMOUNT = re.compile(
    r"(?:€|EUR|euro) ?(\d{1,3}(?:[ .]\d{3})*(?:,\d{2})?|\d+(?:[.,]\d{2})?)"
    r"|(\d{1,3}(?:[ .]\d{3})*,\d{2}|\d+[.,]\d{2}) ?(?:€|EUR|euro)",
    re.IGNORECASE,
)
_SPACES = re.compile(r"[\u00a0\u2007\u202f\t]")  # a no-break or thin space reads as a plain space
_PRINTED = re.compile(r"[0-9€$., A-Z]*")  # what the table's CHECK allows (amount_text)
_DATE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")
_DUE_CUE = re.compile(
    r"\b(?:entro\s+il|scadenza|scade\s+il|da\s+pagare\s+entro|data\s+di\s+scadenza|"
    r"valid[oa]\s+fino\s+al|termine)\b",
    re.IGNORECASE,
)
_PAGOPA = re.compile(r"\bpago\s?pa\b|\bavviso\s+di\s+pagamento\b|\bcodice\s+avviso\b", re.IGNORECASE)
_SEND = re.compile(
    r"\bnotific\w*\s+(?:digital|a\s+valore\s+legale)\w*|\bSEND\b|\bnotifichedigitali\b", re.IGNORECASE
)
_INSURER = re.compile(
    r"\b(?:polizza|rc\s?auto|r\.c\.\s?auto|assicurazion\w*|quietanza|tagliando\s+assicurativo)\b",
    re.IGNORECASE,
)
_EXPIRY = re.compile(r"\b(?:scadenza|scade|in\s+scadenza|rinnovo|preavviso)\b", re.IGNORECASE)
_CONDO = re.compile(
    r"\b(?:condomin\w*|amministratore\s+(?:di|del)\s+condominio|rata\s+condominiale)\b", re.IGNORECASE
)
_CONDO_DUE = re.compile(r"\b(?:rata|assemblea|bilancio|rendiconto|preventivo)\b", re.IGNORECASE)
_ADE_INPS = re.compile(
    r"\b(?:agenzia\s+delle\s+entrate|cassetto\s+fiscale|inps|riscossione|cartella)\b", re.IGNORECASE
)
_OFFICIAL_CLAIM = re.compile(
    r"\b(?:agenzia\s+delle\s+entrate|inps|pago\s?pa|ministero|tessera\s+sanitaria|"
    r"notifica\s+digitale|equitalia|riscossione)\b",
    re.IGNORECASE,
)
_HEALTH_CARD_RENEWAL = re.compile(
    r"\b(?:rinnov\w*|scadut\w*|aggiorna\w*)\b.{0,60}\btessera\s+sanitaria\b|"
    r"\btessera\s+sanitaria\b.{0,60}\b(?:rinnov\w*|scadut\w*|aggiorna\w*)\b",
    re.IGNORECASE | re.DOTALL,
)
_URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class Mail:
    """An email or a PEC message, as the radar reads it (never stored here)."""

    ref: str
    from_address: str
    subject: str
    text: str  # snippet or body, at most a few thousand characters
    sent_at: datetime


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().strip(">").lower() if "@" in address else ""


# <x>.it is open registration, so `comune.<x>.it` alone proves nothing. A Comune's, a
# Provincia's or a Regione's own domain sits under a reserved geographic second level: comune.<city>.it (a
# province's capital), comune.<name>.<province code>.it, regione.<region>.it.
PROVINCE_CODES = frozenset(
    "ag al an ao ap aq ar at av ba bg bi bl bn bo br bs bt bz ca cb ce ch ci cl cn co cr cs ct cz en fc fe "
    "fg "
    "fi fm fr ge go gr im is kr lc le li lo lt lu mb mc me mi mn mo ms mt na no nu og or ot pa pc pd pe pg "
    "pi "
    "pn po pr pt pu pv pz ra rc re rg ri rm rn ro sa si so sp sr ss su sv ta te tn to tp tr ts tv ud va vb "
    "vc "
    "ve vi vr vs vt vv".split()
)
PROVINCE_CITIES = frozenset(
    "agrigento alessandria ancona aosta arezzo ascoli-piceno asti avellino bari barletta-andria-trani "
    "belluno "
    "benevento bergamo biella bologna bolzano brescia brindisi cagliari caltanissetta campobasso caserta "
    "catania catanzaro chieti como cosenza cremona crotone cuneo enna fermo ferrara firenze foggia "
    "forli-cesena "
    "frosinone genova gorizia grosseto imperia isernia la-spezia laquila latina lecce lecco livorno lodi "
    "lucca "
    "macerata mantova massa-carrara matera messina milano modena monza napoli novara nuoro oristano padova "
    "palermo parma pavia perugia pesaro-urbino pescara piacenza pisa pistoia pordenone potenza prato ragusa "
    "ravenna reggio-calabria reggio-emilia rieti rimini roma rovigo salerno sassari savona siena siracusa "
    "sondrio taranto teramo terni torino trapani trento treviso trieste udine varese venezia verbania "
    "vercelli "
    "verona vibo-valentia vicenza viterbo".split()
)
REGIONS = frozenset(
    "abruzzo basilicata calabria campania emilia-romagna emiliaromagna friuli-venezia-giulia fvg lazio "
    "liguria "
    "lombardia marche molise piemonte puglia sardegna sicilia toscana trentino umbria valle-aosta vda veneto "
    "taa".split()
)


def _public_body(labels: list[str], certified: bool) -> bool:
    """Labels end with a Comune's, a Provincia's or a Regione's own domain (any subdomain before it).

    `comune.<name>.<prov>.it` only from a certified sender (PEC, whose
    provider vouches for the sender): a third level under a province code can be registered by anyone, and no
    list of the comuni ships with Pisa. Ordinary mail from it is unverified."""
    for i, label in enumerate(labels[:-2]):
        rest = labels[i + 1 :]
        if (
            label in ("comune", "provincia")
            and (
                (len(rest) == 2 and rest[0] in PROVINCE_CITIES)
                or (certified and len(rest) == 3 and rest[1] in PROVINCE_CODES)
            )
            and rest[-1] == "it"
        ):
            return True
        if label == "regione" and len(rest) == 2 and rest[0] in REGIONS and rest[-1] == "it":
            return True
    return False


def official(domain: str, certified: bool = False) -> bool:
    """A sender's or a link's domain on the allow-list (a Comune's, a Provincia's or a Regione's own domain
    too; `certified`: a PEC sender, see `_public_body`). Anything else is unverified: a message claiming an
    office from it is never tracked."""
    if not domain:
        return False
    if any(domain == d or domain.endswith("." + d) for d in OFFICIAL_DOMAINS):
        return True
    return _public_body(domain.split("."), certified)


def _due(text: str) -> date | None:
    """The first date after a due cue (else None): the notice's own day, as printed."""
    for cue in _DUE_CUE.finditer(text):
        found = _DATE.search(text, cue.end(), cue.end() + 60)
        if found:
            day, month, year = (int(g) for g in found.groups())
            return day_or_none(year, month, day)
    return None


def amount_as_printed(text: str) -> str | None:
    """The first amount in the text, as printed (digits, separators, the currency word): never computed."""
    found = _AMOUNT.search(_SPACES.sub(" ", text))
    if not found:
        return None
    number = found.group(1) or found.group(2)
    printed = f"€ {number.strip()}"
    return printed if _PRINTED.fullmatch(printed) and len(printed) <= 40 else None


def notice_code(text: str) -> str | None:
    found = _NOTICE_CODE.search(text)
    return found.group(1).replace(" ", "") if found else None


def send_clock(made_available: datetime) -> date:
    """SEND: the notice counts as delivered when opened, or 7 days after it was made available, whichever
    comes first. Pisa can't see the opening: the latest legal delivery day starts their payment or appeal
    clock."""
    return (made_available + SEND_DELIVERY).date()


def suspect(mail: Mail, certified: bool = False) -> str | None:
    """Why a message is phishing-like (flagged, never tracked), else None."""
    text = f"{mail.subject}\n{mail.text}"
    domain = domain_of(mail.from_address)
    if _HEALTH_CARD_RENEWAL.search(text):
        return "the Ministry never emails about the tessera sanitaria's renewal: a renewal email is phishing"
    if _OFFICIAL_CLAIM.search(text) and not official(domain, certified):
        hosts = [h.lower() for h in _URL_HOST.findall(text)]
        if hosts and not all(official(h) for h in hosts):
            return "it names a public office but its links go to sites that aren't official"
        if _EXPIRY.search(text) or _PAGOPA.search(text):
            return "it names a public office but doesn't come from one of its addresses"
    return None


_PAGOPA_INFO = (
    "https://developer.pagopa.it/pago-pa/api",
    "Late payment: the creditor's own rules (a fine or a tax: its notice says)",
    "IO app or home banking, with the 18-digit notice code",
)
_SEND_INFO = (
    "https://notifichedigitali.pagopa.it",
    "The payment or appeal clock starts at delivery, read or not",
    "SEND on the web or in IO (SPID/CIE)",
)
_RC_INFO = (
    "https://www.generali.it/magazine/auto/tacito-rinnovo/",
    "No tacit renewal; 15 days' grace for RC only: not the add-ons, often not abroad",
    "The insurer's site or agent",
)
_CONDO_INFO = (
    "https://www.brocardi.it/codice-civile/libro-terzo/titolo-vii/capo-ii/art1130.html",
    "The administrator calls the assembly within 180 days of the year's end (art. 1130 c.c.)",
    "The administrator's email or PEC",
)
_ADE_INFO = ("https://www.agenziaentrate.gov.it", None, "The AdE or INPS area riservata (SPID/CIE)")


def classify(mail: Mail, source: Source, wanted: dict[str, bool]) -> Obligation | None:
    """One message → at most one obligation (or a flagged line). `wanted`: their radar settings."""
    text = f"{mail.subject}\n{mail.text}"
    ref = mail.ref
    why = suspect(mail, certified=source == "pec")
    if why is not None:
        return Obligation(
            "suspect", "Messaggio sospetto: non lo seguo", None, source, ref, state="flagged", flag_reason=why
        )
    domain = domain_of(mail.from_address)
    sent = mail.sent_at.date()
    if wanted.get("send", True) and _SEND.search(text) and (official(domain) or source == "pec"):
        delivered = send_clock(mail.sent_at)
        url, penalty, channel = _SEND_INFO
        return Obligation(
            "send_notice",
            f"Notifica SEND: consegnata per legge al più tardi il {delivered:%d/%m/%Y}",
            delivered,
            source,
            ref,
            url,
            penalty,
            channel,
            remind_on=sent,
            last_verified=VERIFIED,
        )
    if wanted.get("pagopa", True) and _PAGOPA.search(text) and (code := notice_code(text)):
        due = _due(text)
        url, penalty, channel = _PAGOPA_INFO
        return Obligation(
            "pagopa_notice",
            f"Avviso pagoPA …{code[-4:]}" + (f" in scadenza il {due:%d/%m/%Y}" if due else ""),
            due,
            source,
            ref,
            url,
            penalty,
            channel,
            amount_as_printed(text),
            remind_on=(due - timedelta(days=7)) if due else sent,
            last_verified=VERIFIED,
        )
    if wanted.get("insurers", True) and _INSURER.search(text) and _EXPIRY.search(text):
        due = _due(text)
        url, penalty, channel = _RC_INFO
        return Obligation(
            "insurance_notice",
            "Polizza in scadenza" + (f" il {due:%d/%m/%Y}" if due else ""),
            due,
            source,
            ref,
            url,
            penalty,
            channel,
            amount_as_printed(text),
            remind_on=(due - timedelta(days=10)) if due else sent,
            last_verified=VERIFIED,
        )
    if wanted.get("condominium", True) and _CONDO.search(text) and _CONDO_DUE.search(text):
        due = _due(text)
        url, penalty, channel = _CONDO_INFO
        return Obligation(
            "condominium",
            "Condominio: rata o assemblea" + (f" il {due:%d/%m/%Y}" if due else ""),
            due,
            source,
            ref,
            url,
            penalty,
            channel,
            amount_as_printed(text),
            remind_on=(due - timedelta(days=7)) if due else None,
            last_verified=VERIFIED,
        )
    if wanted.get("ade_inps", True) and _ADE_INPS.search(text) and official(domain, source == "pec"):
        due = _due(text)
        url, penalty, channel = _ADE_INFO
        return Obligation(
            "ade_inps",
            "Comunicazione dell'Agenzia delle Entrate o dell'INPS"
            + (f": scadenza {due:%d/%m/%Y}" if due else ""),
            due,
            source,
            ref,
            url,
            penalty,
            channel,
            remind_on=(due - timedelta(days=7)) if due else None,
            last_verified=VERIFIED,
        )
    return None


# --- the calendar (the 2026 cycle; switched by their list)
# -------------------------------------------------


@dataclass(frozen=True)
class Rule:
    key: str
    part: Literal["income_tax", "property", "isee", "condominium"]
    what: str
    month: int
    day: int
    first_nudge: timedelta  # §4's "first nudge" before the due day
    url: str
    penalty: str | None
    channel: str
    shift_weekend: bool = True  # a tax date on a Saturday or a Sunday moves to the Monday


RULES: tuple[Rule, ...] = (
    Rule(
        "730",
        "income_tax",
        "730: invio della dichiarazione (precompilata dal 30 aprile)",
        9,
        30,
        timedelta(days=29),
        "https://www.agenziaentrate.gov.it/portale/quando-e-come-presentare-il-730-2026-cittadini",
        "Late: within 90 days counts as valid; penalty lower with ravvedimento",
        "AdE precompilata web app (SPID/CIE)",
    ),
    Rule(
        "redditi",
        "income_tax",
        "Modello Redditi PF: invio",
        10,
        31,
        timedelta(days=21),
        "https://fiscomania.com/dichiarazione-tardiva/",
        "Within 90 days: €250, €25 with ravvedimento; after: omitted (120% of the tax, min €250)",
        "AdE web (SPID/CIE)",
    ),
    Rule(
        "irpef_saldo",
        "income_tax",
        "IRPEF: saldo e primo acconto, se dovuti",
        6,
        30,
        timedelta(days=21),
        "https://www.agenziaentrate.gov.it/portale/come-si-paga-l-irpef4",
        "Late: 25% base (12.5% within 90 days), lower with ravvedimento, plus interest",
        "F24 (home banking, F24 web)",
    ),
    Rule(
        "irpef_acconto",
        "income_tax",
        "IRPEF: secondo o unico acconto, se dovuto",
        11,
        30,
        timedelta(days=21),
        "https://fiscomania.com/scadenze-fiscali-novembre-calendario/",
        "Late: 25% base (12.5% within 90 days), lower with ravvedimento; this one can't be split",
        "F24 (home banking, F24 web)",
    ),
    Rule(
        "imu_acconto",
        "property",
        "IMU: acconto",
        6,
        16,
        timedelta(days=21),
        "https://studiocloud.it/blog/scadenze-imu-2026",
        "25% base; ravvedimento 0.083%/day to day 14, 1.25% to 30, 1.39% to 90, plus interest",
        "F24",
    ),
    Rule(
        "imu_saldo",
        "property",
        "IMU: saldo",
        12,
        16,
        timedelta(days=21),
        "https://studiocloud.it/blog/scadenze-imu-2026",
        "25% base; ravvedimento 0.083%/day to day 14, 1.25% to 30, 1.39% to 90, plus interest",
        "F24",
    ),
    Rule(
        "isee",
        "isee",
        "ISEE: nuova DSU (vale fino al 31 dicembre; per l'Assegno Unico entro il 28 febbraio)",
        2,
        28,
        timedelta(days=57),
        "https://www.inps.it/it/it/dettaglio-scheda.it.schede-servizio-strumento.schede-servizi.Portale-unico-ISEE.html",
        None,
        "INPS site or app (precompiled DSU)",
        shift_weekend=False,
    ),
)
# The 2026 dates the report gives that the rule alone wouldn't
KNOWN: dict[tuple[str, int], date] = {("redditi", 2026): date(2026, 11, 2)}


def _shift(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _on(rule: Rule, year: int) -> date:
    if (rule.key, year) in KNOWN:
        return KNOWN[(rule.key, year)]
    day = day_or_28(year, rule.month, rule.day)
    return _shift(day) if rule.shift_weekend else day


def calendar(
    parts: dict[str, bool],
    today: date,
    *,
    horizon: timedelta = timedelta(days=400),
    condominium_year_end: str = "12-31",
) -> list[Obligation]:
    """The national dates of their parts from today to the horizon. A year after 2026 is to be confirmed."""
    out: list[Obligation] = []
    for rule in RULES:
        if not parts.get(rule.part, False):
            continue
        for year in (today.year, today.year + 1, today.year + 2):
            due = _on(rule, year)
            if due < today or due > today + horizon:
                continue
            confirmed = year <= VERIFIED.year
            what = rule.what if confirmed else f"{rule.what} (data da confermare quando l'AdE la pubblica)"
            out.append(
                Obligation(
                    rule.key,
                    what,
                    due,
                    "calendar",
                    f"{rule.key}:{year}",
                    rule.url,
                    rule.penalty,
                    rule.channel,
                    remind_on=due - rule.first_nudge,
                    last_verified=VERIFIED if confirmed else None,
                )
            )
    if parts.get("condominium", False):
        month, day = (int(x) for x in condominium_year_end.split("-"))
        for year in (today.year - 1, today.year, today.year + 1):
            closed = day_or_28(year, month, day)
            due = closed + timedelta(days=180)
            if today <= due <= today + horizon:
                url, penalty, channel = _CONDO_INFO
                out.append(
                    Obligation(
                        "condominium_assembly",
                        "Condominio: l'assemblea per i conti dell'anno (entro 180 giorni)",
                        due,
                        "calendar",
                        f"condominium_assembly:{year}",
                        url,
                        penalty,
                        channel,
                        remind_on=closed + timedelta(days=150),
                        last_verified=VERIFIED,
                    )
                )
    return out


# --- the vault's documents → obligations
# ---------------------------------------------------------------

_DOCUMENTS: dict[str, tuple[str, str, str | None, str]] = {
    # kind -> (what, url, penalty, channel)
    "cie": (
        "Carta d'identità: scadenza (rinnovo dai 180 giorni prima)",
        "https://comune.san-miniato.pi.it/novita/la-carta-didentita-cartacea-resta-valida-fino-alla-scadenza-per-lespatrio-resta-obbligatoria-la-cie-dal-3-agosto-2026/",
        "Paper cards aren't valid abroad from 3 Aug 2026",
        "Agenda CIE (SPID/CIE)",
    ),
    "passport": (
        "Passaporto: scadenza (un passaporto nuovo annulla l'ESTA)",
        "https://passaportonline.poliziadistato.it",
        None,
        "Agenda Passaporto (SPID/CIE)",
    ),
    "driving_licence": (
        "Patente: scadenza (rinnovo negli ultimi 4 mesi)",
        "https://www.mit.gov.it/rinnovo-patente",
        None,
        "Portale dell'Automobilista; medico certificatore",
    ),
    "health_card": (
        "Tessera sanitaria: scadenza (arriva per posta da sola; mai per email)",
        "https://www.agenziaentrate.gov.it/portale/quando-scade",
        None,
        "AdE area riservata",
    ),
    "car_revision": (
        "Revisione dell'auto: entro la fine del mese (nessuna tolleranza)",
        "https://www.ilportaledellautomobilista.it",
        "€173-694, and the car is suspended",
        "A revision centre; the date by plate on the Portale dell'Automobilista",
    ),
    "car_tax": (
        "Bollo auto: entro la fine del mese dopo la scadenza",
        "https://www.lautomobile.aci.it/attualita/bollo-auto-2026-scadenze-tariffe-ed-esenzioni-la-guida-completa/",
        "Ravvedimento 0.08%/day to 15 days, then 1.25%, 1.39%, 3.13%, 3.75%",
        "pagoPA / IO, ACI",
    ),
    "car_insurance": (
        "RC auto: scadenza (15 giorni di tolleranza solo per l'RC, non all'estero)",
        "https://www.generali.it/magazine/auto/tacito-rinnovo/",
        "No tacit renewal; uninsured after the 15 days",
        "The insurer's site or agent",
    ),
}
CAR_KINDS = frozenset({"car_revision", "car_tax", "car_insurance"})


def from_document(document_id: str, kind: str, expires_on: date) -> Obligation:
    """A vault document's expiry as an obligation. Its reminders stay the vault's: no second task."""
    what, url, penalty, channel = _DOCUMENTS.get(kind, ("Documento: scadenza", "", None, "—"))
    return Obligation(
        kind,
        what,
        expires_on,
        "vault",
        document_id,
        url or None,
        penalty,
        channel,
        subject=kind,
        last_verified=VERIFIED,
    )


def missing_car_documents(kinds_held: set[str]) -> list[Obligation]:
    """Their list says they have a car and the vault lacks a car's dates: one line each, asking for them."""
    out = []
    for kind in sorted(CAR_KINDS - kinds_held):
        what, url, penalty, channel = _DOCUMENTS[kind]
        out.append(
            Obligation(
                "missing_document",
                f"Manca la data: {what.split(':')[0].lower()}",
                None,
                "calendar",
                f"missing:{kind}",
                url,
                penalty,
                channel,
                subject=kind,
            )
        )
    return out


def psd2_consent(connector_id: str, name: str, expires_at: datetime, renew_from: datetime) -> Obligation:
    """The bank's consent (PSD2, 90-180 days) and its renewal reminder a week before."""
    return Obligation(
        "psd2_consent",
        f"{name}: rinnova il consenso alla lettura dei conti",
        expires_at.date(),
        "bank",
        f"consent:{connector_id}:{expires_at.date().isoformat()}",
        "https://enablebanking.com/docs/api/reference/",
        "Pisa can't read the accounts after it",
        "The bank's page, through Pisa's Settings → Connections",
        remind_on=renew_from.date(),
    )


def task_title(o: Obligation, lang: str) -> str:
    """The reminder task's title: code's own words, never a sender's."""
    if lang.startswith("en"):
        return {
            "psd2_consent": "Renew the bank's consent",
            "send_notice": "Read the SEND notice",
            "pagopa_notice": "Pay the pagoPA notice",
            "insurance_notice": "Policy renewal",
        }.get(o.kind, o.what[:80])
    return o.what[:80]


def done(o: Obligation) -> Obligation:
    return replace(o, state="done")
