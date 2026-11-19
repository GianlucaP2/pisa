"""The synthetic user of the understanding evaluation set:
 an Italian from Turin in Rotterdam for an invented lights
market, Saturday 24 October 2026. Every venue, person and event is invented (the cities and the station are
public places); `tests/support/synth/utterances.py` builds the evaluation set from these entries."""

from datetime import date

from pisa.domain.understand import Entry as E
from pisa.domain.understand import Lexicon, lexicon

TODAY = date(2026, 10, 24)  # a Saturday

ENTRIES = (
    # places
    E("Lantaarn Hostel Rotterdam", "place", ("Lantaarn", "Lantaarn Hostel"), "Rotterdam", "home"),
    E("BRAM'S Broodjes", "place", ("Bram's", "Bram's Broodjes"), "Rotterdam", "today"),
    E("Haverbrug", "place", ("Haverbrug terrein",), "Utrecht", "today"),
    E("Utrecht", "place", (), "Utrecht", "today"),
    E("Rotterdam Centraal", "place", ("Centraal", "Centraal Station"), "Rotterdam", "today"),
    E("Kraanhal", "place", (), "Rotterdam", "week"),
    E("Graansilofabriek", "place", ("Graansilo",), "Rotterdam", "week"),
    E("Café de Mol", "place", ("de Mol", "Mol"), "Rotterdam", "week"),
    E("Spiegelmuseum", "place", (), "Rotterdam", "week"),
    E("Kip & Kop", "place", (), "Rotterdam", "week"),
    E("WKH", "place", ("WKH werf",), "Rotterdam", "visited"),
    E("Wilgenpark", "place", (), "Rotterdam", "visited"),
    E("Gino a Vanchiglia", "place", ("da Gino",), "Torino", "known"),
    E("Tabacchi Gallo", "place", (), "Torino", "known"),
    E("Vanchiglia", "place", (), "Torino", "known"),
    E("Caselle", "place", (), "Torino", "known"),
    E("Zestienhoven", "place", (), "Rotterdam", "week"),
    # people
    E("Dario Ferri", "person", ("Dario",), None, "contact"),
    E("Femke de Wit", "person", ("Femke",), None, "contact"),
    E("Chiara Marini", "person", ("Chiara",), None, "contact"),
    E("Joost", "person", (), None, "contact"),
    E("Elena Neri", "person", ("Elena",), None, "contact"),
    E("Elena Conti", "person", ("Elena",), None, "contact"),
    E("Irene", "person", (), None, "contact"),
    # events
    E("Lichtjesmarkt", "event", ("Lichtjes",), "Utrecht", "today"),
    E("KADE", "event", ("KADE Kraanhal",), "Rotterdam", "week"),
    E("Cena con Dario", "event", (), "Torino", "known"),
    # tasks
    E("Personalizza i biglietti Lichtjesmarkt", "task", (), None, "week"),
    E("Preventivo del sito per Chiara", "task", (), None, "week"),
)


def owner() -> Lexicon:
    return lexicon(ENTRIES, TODAY)
