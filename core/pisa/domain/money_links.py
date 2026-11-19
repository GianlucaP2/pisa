"""The fixed pages Pisa's money buttons may open (`open_url`), and what each service or airline is.

FACT: the research behind these links didn't open every page
live, so an airline's button opens its own site and the note gives the path to its claim form; a service's
button opens its own account page. **Pisa never cancels, claims or pays**: the user taps and does it there.
`pisa/domain/handoffs.py` admits these hosts (and only these) besides its own templates. Pure: no I/O.
"""

from dataclasses import dataclass

# --- flights ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Airline:
    name: str
    url: str
    steps_it: str
    steps_en: str


# IATA prefix → the airline's own site and the path to its claim form
AIRLINES: dict[str, Airline] = {
    "AZ": Airline("ITA Airways", "https://www.ita-airways.com/", "Assistenza → Reclami e rimborsi",
                  "Help → Complaints and refunds"),
    "FR": Airline("Ryanair", "https://www.ryanair.com/", "Aiuto → Richiesta EU261", "Help → EU261 claim"),
    "U2": Airline("easyJet", "https://www.easyjet.com/", "Aiuto → Richiesta di compensazione UE",
                  "Help → EU compensation claim"),
    "EC": Airline("easyJet", "https://www.easyjet.com/", "Aiuto → Richiesta di compensazione UE",
                  "Help → EU compensation claim"),
    "LH": Airline("Lufthansa", "https://www.lufthansa.com/", "Modulo «Flight delay compensation»",
                  "The «Flight delay compensation» form"),
    "W6": Airline("Wizz Air", "https://wizzair.com/", "Aiuto → Reclami", "Help → Claims"),
    "W4": Airline("Wizz Air", "https://wizzair.com/", "Aiuto → Reclami", "Help → Claims"),
    "VY": Airline("Vueling", "https://www.vueling.com/", "Reclami", "Claims"),
    "AF": Airline("Air France", "https://www.airfrance.com/", "Reclami / Rimborsi", "Claim / Refund"),
    "KL": Airline("KLM", "https://www.klm.com/", "Reclami / Rimborsi", "Claim / Refund"),
    "AA": Airline("American Airlines", "https://www.aa.com/", "Richiesta di rimborso", "Refund request"),
    "DL": Airline("Delta", "https://www.delta.com/", "Richiesta di rimborso", "Refund request"),
    "UA": Airline("United", "https://www.united.com/", "Richiesta di rimborso", "Refund request"),
}  # fmt: skip

EU_FORM = "https://europa.eu/youreurope/citizens/travel/passenger-rights/air/index_en.htm"
ENAC_FORM = "https://www.enac.gov.it/passeggeri/i-diritti-dei-passeggeri/reclami"
DOT_FORM = "https://www.transportation.gov/airconsumer/file-consumer-complaint"

# --- subscriptions --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Service:
    key: str
    name: str
    domains: tuple[str, ...]  # the sender's address domain (or a subdomain of one)
    words: tuple[str, ...]  # words a store's receipt (Apple, Google) names it by
    url: str | None  # its own account page; None: steps only
    steps_it: str
    steps_en: str


# The research's 15: the service's own account page; the user cancels there.
SERVICES: tuple[Service, ...] = (
    Service("amazon_prime", "Amazon Prime", (), ("amazon prime", "prime membership", "iscrizione prime"),
            "https://www.amazon.it/gp/primecentral",
            "Gestisci l'iscrizione → Termina l'iscrizione (rimborso se non hai usato Prime dall'addebito)",
            "Manage membership → End membership (a refund if Prime wasn't used since the charge)"),
    Service("netflix", "Netflix", ("netflix.com",), ("netflix",), "https://www.netflix.com/cancelplan",
            "Annulla l'abbonamento dalla pagina che si apre", "Cancel the plan on the page it opens"),
    Service("spotify", "Spotify", ("spotify.com",), ("spotify",), "https://www.spotify.com/account/",
            "Gestisci abbonamento → Annulla (se paghi con Apple o Google, annulla lì)",
            "Manage plan → Cancel (billed through Apple or Google? cancel there)"),
    Service("disney", "Disney+", ("disneyplus.com",), ("disney+", "disney plus"),
            "https://www.disneyplus.com/", "Account → Abbonamento → Annulla abbonamento",
            "Account → Subscription → Cancel subscription"),
    Service("youtube", "YouTube Premium", ("youtube.com",), ("youtube premium", "youtube music"),
            "https://www.youtube.com/paid_memberships", "Abbonamenti a pagamento → Disattiva",
            "Paid memberships → Deactivate"),
    Service("apple", "Apple", ("apple.com", "email.apple.com", "itunes.com"), ("icloud+", "apple one",
            "apple music", "apple tv+"), "https://support.apple.com/en-us/118428",
            "Impostazioni → il tuo nome → Abbonamenti; rimborsi: reportaproblem.apple.com",
            "Settings → your name → Subscriptions; refunds: reportaproblem.apple.com"),
    Service("google", "Google Play / Google One", ("google.com",), ("google one", "google play"),
            "https://play.google.com/store/account/subscriptions", "Abbonamenti → Annulla",
            "Subscriptions → Cancel"),
    Service("microsoft", "Microsoft 365 / Xbox", ("microsoft.com",), ("microsoft 365", "game pass", "xbox"),
            "https://account.microsoft.com/services", "Servizi e abbonamenti → Gestisci → Annulla",
            "Services & subscriptions → Manage → Cancel"),
    Service("adobe", "Adobe", ("adobe.com",), ("creative cloud", "adobe"), "https://account.adobe.com/plans",
            "Piani → Annulla il piano (un piano annuale pagato al mese ha una penale dopo 14 giorni)",
            "Plans → Cancel plan (an annual plan paid monthly has a fee after 14 days)"),
    Service("dazn", "DAZN", ("dazn.com",), ("dazn",), "https://www.dazn.com/",
            "Il mio account → Annulla abbonamento", "My Account → Cancel subscription"),
    Service("now", "NOW", ("nowtv.it",), ("now tv", "nowtv"), "https://www.nowtv.it/",
            "Account → Abbonamenti → Disattiva", "Account → Subscriptions → Cancel"),
    Service("sky", "Sky", ("sky.it",), ("sky tv", "sky q"), "https://www.sky.it/",
            "Disdetta dal modulo online, PEC o raccomandata (L. 40/2007, Bersani)",
            "Cancellation by the online form, PEC or registered letter (L. 40/2007)"),
    Service("max", "Max", ("max.com", "hbomax.com"), ("hbo max",), "https://www.max.com/",
            "Account → Abbonamento → Annulla", "Account → Subscription → Cancel"),
    Service("paramount", "Paramount+", ("paramountplus.com",), ("paramount+",), "https://www.paramountplus.com/",
            "Account → Abbonamento → Annulla", "Account → Subscription → Cancel"),
    Service("peacock", "Peacock", ("peacocktv.com",), ("peacock",), "https://www.peacocktv.com/",
            "Account → Piano → Annulla", "Account → Plan → Cancel"),
    Service("hulu", "Hulu", ("hulu.com",), ("hulu",), "https://secure.hulu.com/account",
            "Annulla dalla pagina dell'account", "Cancel on the account page"),
    Service("audible", "Audible", ("audible.it", "audible.com"), ("audible",), "https://www.audible.it/",
            "Dettagli account → Annulla l'abbonamento", "Account details → Cancel membership"),
    Service("kindle_unlimited", "Kindle Unlimited", (), ("kindle unlimited",),
            "https://www.amazon.com/kindle-dbs/ku/ku-central", "Annulla l'iscrizione a Kindle Unlimited",
            "Cancel the Kindle Unlimited membership"),
    Service("telecom", "Telefono e internet", ("tim.it", "vodafone.it", "windtre.it", "iliad.it",
            "fastweb.it"), (), None,
            "Modulo di recesso o disdetta online dell'operatore, o PEC (L. 40/2007, Bersani)",
            "The operator's online withdrawal form or PEC (L. 40/2007)"),
)  # fmt: skip

# --- stores to buy again (things that run out) ------------------------------------------------------------

# A seller's domain → the store's own page to buy again (the user logs in and orders there).
STORES_AGAIN: dict[str, tuple[str, str]] = {
    "amazon.it": ("Amazon", "https://www.amazon.it/gp/buyagain"),
    "amazon.com": ("Amazon", "https://www.amazon.com/gp/buyagain"),
    "esselunga.it": ("Esselunga", "https://www.esselunga.it/it-it/spesa-online.html"),
    "instacart.com": ("Instacart", "https://www.instacart.com/store"),
}

# The hosts above, for `handoffs.safe_url`.
HOSTS: frozenset[str] = frozenset(
    url.split("/")[2]
    for url in (
        *(a.url for a in AIRLINES.values()),
        EU_FORM,
        ENAC_FORM,
        DOT_FORM,
        *(s.url for s in SERVICES if s.url),
        *(u for _, u in STORES_AGAIN.values()),
    )
)


def domain_of(address: str | None) -> str:
    """The address's domain, lowercased ("" when none)."""
    if not address or "@" not in address:
        return ""
    return address.rsplit("@", 1)[1].strip().lower().rstrip(">")


def under(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith("." + parent)


def service_of(address: str | None, text: str) -> Service | None:
    """The service a subscription is: by its sender's domain, else by the words of a store's receipt."""
    domain = domain_of(address)
    for s in SERVICES:
        if domain and any(under(domain, d) for d in s.domains):
            if s.key == "apple" or s.key == "google":  # a store: the receipt may name another service
                break
            return s
    low = text.casefold()
    for s in SERVICES:
        if s.key not in ("apple", "google") and any(w in low for w in s.words):
            return s
    for s in SERVICES:
        if domain and any(under(domain, d) for d in s.domains):
            return s
        if any(w in low for w in s.words):
            return s
    return None


def store_again(seller_address: str | None) -> tuple[str, str] | None:
    domain = domain_of(seller_address)
    for d, store in STORES_AGAIN.items():
        if domain and under(domain, d):
            return store
    return None
