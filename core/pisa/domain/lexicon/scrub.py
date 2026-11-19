"""What the night strips from a query before it leaves Pisa (domain/night) and the user loop's
anonymiser replaces (domain/owner_loop): the words that date a day, currency words, street words, and
the e-mail, link and street-address patterns, in the languages their words may use.

No I/O here."""

import re

# Words that date a day of theirs, in the languages their queries may use (always stripped)
QUERY_MONTHS = frozenset(
    "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre "
    "gen feb mar apr mag giu lug ago sett ott nov dic "
    "january february march april may june july august september october november december "
    "jan sep sept oct dec "
    "enero febrero abril mayo junio julio agosto septiembre setiembre octubre noviembre diciembre "
    "janvier fevrier mars avril mai juin juillet aout septembre octobre novembre decembre "
    "januar februar marz juni juli oktober dezember "
    "janeiro fevereiro marco maio junho julho setembro outubro novembro dezembro".split()
)
QUERY_DAYS = frozenset(
    "lunedi martedi mercoledi giovedi venerdi sabato domenica oggi domani ieri stasera stanotte stamattina "
    "dopodomani monday tuesday wednesday thursday friday saturday sunday today tomorrow yesterday tonight "
    "lunes martes miercoles jueves viernes hoy manana ayer lundi mardi mercredi jeudi vendredi samedi "
    "dimanche aujourd hui demain hier montag dienstag mittwoch donnerstag freitag samstag sonntag heute "
    "morgen "
    "gestern".split()
)
CURRENCY_WORDS = frozenset(
    "eur euro euros usd dollaro dollari dollar dollars gbp sterlina sterline pound pounds chf franchi franc "
    "francs yen dolares pesos libras cent cents centesimi centesimo".split()
)
STREET_WORDS = (
    "via|viale|piazza|piazzale|p.zza|corso|largo|vicolo|strada|lungomare|lungotevere|contrada|località|"
    "localita|frazione|street|st.|avenue|ave.|road|rd.|boulevard|blvd.|lane|drive|calle|rue|strasse"
)
PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # an e-mail
    re.compile(r"(?:https?://|www\.)\S+"),  # a link
    # an address: the street's word, then a name of capitalised words, then a house number
    re.compile(rf"(?i:\b(?:{re.escape(STREET_WORDS).replace(chr(92) + '|', '|')}))\s+"
               r"(?:(?:d[ei]l?l?['\u2019]|de\s|di\s|del\s|della\s)?[A-ZÀ-Ý][^\s,;]*\s*){1,5}(?:,?\s*\d+\w*)?"),
]  # fmt: skip
EMAIL_PATTERN, LINK_PATTERN, ADDRESS_PATTERN = PATTERNS
