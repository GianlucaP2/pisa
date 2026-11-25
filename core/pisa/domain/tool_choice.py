"""The user's words that name one of Pisa's newer tools, so it is offered at once (not behind
`search_tools`) in that turn. Pure rules.

With only the common tools loaded, models picked the older tool in sight (add_task with a place for "ricordami
quando sono da Esselunga…", compose_message for "scrivi a Marco su WhatsApp…") or none (no closing sheet, no
document, no skill, no ride). A tool these words point to joins the turn's tools; the model
still decides, and every tool's own rails still apply (this only shows a tool, it never calls one).
"""

import re

from pisa.domain import accountant as accountant_rules
from pisa.domain import answers as answers_rules
from pisa.domain import app_help as app_help_rules
from pisa.domain import autonomy as autonomy_rules
from pisa.domain import countries, coverage
from pisa.domain import drawing as drawing_rules
from pisa.domain import intentions as intention_rules
from pisa.domain import learn as learn_rules
from pisa.domain import research as research_rules
from pisa.domain import talk as talk_rules
from pisa.domain import vault as vault_rules
from pisa.domain import views as view_rules
from pisa.domain import visual as visual_rules

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # a reminder Pisa set, cancelled on their words ("annulla il promemoria per Anna", §27 w3)
    (
        "cancel_reminder",
        re.compile(
            r"\b(?:annull\w*|cancell\w*|elimin\w*|togli\w*|rimuov\w*|cancel\w*|delete|remove|drop)\b.{0,40}"
            r"\b(?:promemoria|ricordo|avviso|reminders?|alerts?)\b"
            r"|\bnon\s+(?:ricordarmel\w*|mi\s+serve\s+(?:più\s+)?il\s+promemoria)\b"
            r"|\b(?:promemoria|reminders?)\b.{0,40}\b(?:annull\w*|cancell\w*|elimin\w*|togli\w*|cancel\w*|delete|remove)",
            re.IGNORECASE,
        ),
    ),
    # the connectors' readers, offered at once only when their words point to them
    (
        "health_summary",
        re.compile(  # their sleep, steps and training, never "ho sonno"
            r"\b(?:(?:come\s+)?ho\s+dormito|sonno\s+di\s+(?:stanotte|ieri|questa|stamattina)|quanti\s+passi|"
            r"passi\s+(?:di\s+)?(?:oggi|ieri)|(?:i\s+miei|gli)\s+allenamenti|battito\s+a\s+riposo|hrv|vo2\s?max|"
            r"how\s+did\s+i\s+sleep|how\s+many\s+steps|my\s+(?:sleep|workouts)|resting\s+heart\s+rate)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "money_recent",
        re.compile(  # their account, never "movimenti di stretching" or a transfer they must make
            r"\b(?:quanto\s+ho\s+speso|ho\s+speso|movimenti\s+(?:del|sul)\s+conto|estratto\s+conto|"
            r"saldo\s+del\s+conto|(?:mi\s+)?(?:è|e')\s+arrivato\s+(?:il|lo)\s+(?:bonifico|stipendio)|"
            r"how\s+much\s+did\s+i\s+spend|bank\s+(?:account|transactions))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "obligations",
        re.compile(  # the radar's words, never a contract's revision. Their own deadlines asked for
            # ("ho scadenze questo mese?") are the radar's
            r"\b(?:(?:ho|hai|le\s+mie|quali|che|prossime)\s+scadenze|scadenze\s+(?:di\s+|del\s+|della\s+|"
            r"entro\s+)?(?:questo|questa|oggi|domani|settimana|mese)|any\s+deadlines|"
            r"scadenze\s+(?:fiscali|amministrative|dell'auto|della\s+macchina)|cosa\s+(?:mi\s+)?scade|"
            r"quando\s+scade\s+(?:il|la|l')\s*(?:bollo|patente|passaporto|carta|revisione|assicurazione|rc)|"
            r"bollo|imu|tari|f24|730|pago\s?pa|avviso\s+di\s+pagamento|revisione\s+(?:dell'auto|della\s+macchina|"
            r"auto)|rc\s+auto|isee|notifica\s+send|tax\s+deadlines?)\b",
            re.IGNORECASE,
        ),
    ),
    # a note for when they are at a place (never a task, never a timed reminder)
    (
        "remember_at_place",
        re.compile(
            r"\b(?:quando|appena|se)\s+(?:sono|passo|arrivo|vado|entro|mi\s+trovo)\s+(?:da|a|al|alla|allo|ai|"
            r"in|dal|dalla|dallo|nel|nella|vicino)\b|\bwhen\s+i(?:'m|\s+am|\s+get|\s+pass|\s+go)\s+"
            r"(?:at|to|by|near|in)\b",
            re.IGNORECASE,
        ),
    ),
    # a message they send themselves, prepared as a button (prepare_message, not compose_message)
    (
        "prepare_message",
        re.compile(
            r"\bwhats\s*app\b|\bimessage\b|\b(?:scrivi|scrivere|manda\s+un\s+messaggio|messaggia|"
            r"rispondi)\s+(?:a|ad|al|alla)\s+\w|\b(?:text|message)\s+(?!me\b)\w+|\bsend\s+\w+\s+a\s+"
            r"(?:message|text)",
            re.IGNORECASE,
        ),
    ),
    # "chiudiamo" (the closing sheet)
    (
        "close_day",
        re.compile(
            r"^\W*(?:pisa\W+)?chiudiamo\b|\bchiudiamo\s+(?:la\s+)?giornata\b|\bclose\s+(?:the|my)\s+day\b|"
            r"^\W*let'?s\s+(?:wrap\s+up|close)\b",
            re.IGNORECASE,
        ),
    ),
    # a wish ("vorrei che tu potessi…") becomes a skill
    (
        "wish_skill",
        re.compile(
            r"\bvorrei\s+che\s+tu\s+(?:potessi|sapessi|riuscissi)\b|\bmi\s+piacerebbe\s+che\s+tu\b|"
            r"\bimpara\s+a\b|\bi\s+wish\s+you\s+could\b|\blearn\s+(?:how\s+)?to\b",
            re.IGNORECASE,
        ),
    ),
    # a call they place themselves ("chiama il dentista", "chiamalo"), never "come si chiama",
    # "mi chiama", nor a ride ("chiamami un Uber")
    (
        "prepare_call",
        re.compile(
            r"(?<!\bsi\s)(?<!\bmi\s)(?<!\bti\s)(?<!\bci\s)\b(?:chiam(?:a|ami|alo|ala|ali|ale|agli|iamo)|"
            r"telefona(?:gli|le|mi)?|ritelefona(?:gli|le)?|richiama(?:lo|la|li)?)\b"
            r"(?!\s+(?:(?:un|una|il|lo)\s+)?(?:uber|lyft|taxi|cab|free\s*now|itaxi))|"
            r"\b(?:call|phone|ring)\s+(?!me\b|us\b)(?:him|her|them|the\s+\w+|my\s+\w+|[A-Z]\w+)\b",
            re.IGNORECASE,
        ),
    ),
    # the days they spent in a country (the residency count), never a search round
    (
        "residency_count",
        re.compile(
            r"\bquanti\s+giorni\b.*\b(?:sono\s+stat[oaie]|siamo\s+stat[ie]|ho\s+(?:passato|trascorso)|"
            r"sono\s+rimast[oa])\b|\bresidenz\w*\b|\b183\s+giorni\b|"
            r"\bhow\s+many\s+days\b.*\b(?:was\s+i|have\s+i\s+been|did\s+i\s+spend|i\s+spent)\b|"
            r"\bresiden(?:cy|t)\b",
            re.IGNORECASE,
        ),
    ),
    # a habit they say out loud ("stasera di nuovo sushi", "come al solito"): Pisa's hunch to ask
    (
        "note_hunch",
        re.compile(
            r"\b(?:di\s+nuovo|ancora\s+una\s+volta|come\s+sempre|come\s+al\s+solito|come\s+ogni\s+\w+|"
            r"anche\s+(?:stasera|oggi)\s+\w+|again|as\s+usual|like\s+always|like\s+every\s+\w+)\b",
            re.IGNORECASE,
        ),
    ),
    # someone's hour ("che ore sono da Laura?", "posso chiamare mamma?"); a person
    # only: never "il ristorante", "l'Uber", "un NCC", "che ora è per la cena"
    (
        "their_time",
        re.compile(
            r"\bche\s+or[ae]\s+(?:sono|è|e')\s+da\s+(?!(?:il|lo|la|i|gli|le|un|una|uno)\b|l['\u2019])\w|"
            r"\bè\s+tardi\s+per\s+(?:chiamar|telefonar)\w*|"
            r"\bposso\s+(?:chiamar|telefonar)(?:e|l[aoei]|gli)\b"
            r"(?!\s+(?:(?:il|lo|la|i|gli|le|un|una|uno|un['\u2019])\b|l['\u2019]|(?:uber|lyft|taxi|cab|ncc|free\s*now|itaxi)\b))|"
            r"\bwhat\s+time\s+is\s+it\s+(?:for|at)\s+(?!(?:the|a|an)\b)\w|"
            r"\bcan\s+i\s+(?:call|phone|ring)\b(?!\s+(?:the|a|an|uber|lyft|taxi|cab)\b)",
            re.IGNORECASE,
        ),
    ),
    # a ride (Uber, a taxi) they request in the app
    ("prepare_ride", re.compile(r"\b(?:uber|lyft|taxi|free\s*now|itaxi|cab)\b", re.IGNORECASE)),
    # a table they book
    (
        "prepare_table",
        re.compile(r"\bprenot\w*\s+(?:un\s+)?tavolo\b|\bbook\s+(?:me\s+)?a\s+table\b", re.IGNORECASE),
    ),
    # their fog of war ("la nebbia", "dove non sono mai stato")
    (
        "show_fog_of_war",
        re.compile(
            r"\bnebbia\b|\bfog\s+of\s+war\b|\bdove\s+non\s+sono\s+mai\s+stato\b|"
            r"\bwhere\s+i(?:'ve|\s+have)\s+never\s+been\b",
            re.IGNORECASE,
        ),
    ),
    # the Map of the Life
    (
        "show_map_of_life",
        re.compile(r"\bmappa\s+della\s+(?:mia\s+)?vita\b|\bmap\s+of\s+(?:my|the)\s+life\b", re.IGNORECASE),
    ),
)


# their list of what others owe them and of their own promises; where they were with one
_LOOPS_LIST = re.compile(
    r"\b(?:cosa|che\s+cosa)\s+sto\s+aspettando\b|\bchi\s+mi\s+deve\b|\bcosa\s+mi\s+devono\b|"
    r"\b(?:che|quali|le\s+mie)\s+promesse\b|\bwhat\s+am\s+i\s+waiting\s+(?:for|on)\b|\bwho\s+owes\s+me\b|"
    r"\bmy\s+promises\b",
    re.IGNORECASE,
)
_LOOPS_RESUME = re.compile(
    r"\ba\s+che\s+punto\s+(?:ero|eravamo|sono)\b|\bdove\s+ero\s+rimasto\b|\briprendiamo\b|"
    r"\bwhere\s+was\s+i\b|\bwhere\s+did\s+i\s+leave\s+off\b",
    re.IGNORECASE,
)


_OPEN_IN_APP = re.compile(
    r"\b(?:aprimi|apri|apro|mostrami|fammi\s+vedere|portami\s+(?:a|su|in)|open|show\s+me|take\s+me\s+to)\b"
    r".{0,40}\b(?:oggi|impostazioni|riepilogo|privacy|attivit[àa]|scheda|today|settings|task)\b",
    re.IGNORECASE,
)
_SETTING_CHANGE = re.compile(
    r"\b(?:riepilogo|briefing)\b.{0,60}\b(?:alle|at|parole|words|più\s+corto|shorter)\b|"
    r"\b(?:preavviso|avvisami|remind\s+me)\b.{0,40}\b(?:minuti|minutes)\s+(?:prima|before)\b",
    re.IGNORECASE,
)


def tools_for(words: str) -> frozenset[str]:
    """The newer tools these words point to (none for most turns)."""
    text = words or ""
    found = {name for name, pattern in _PATTERNS if pattern.search(text)}
    found |= answers_rules.tools_for(text)  # prices, events by date, a table's times
    if research_rules.asks(text):  # a pick's tools and the sheet in the first round
        found |= research_rules.RESEARCH_TOOLS
    if vault_rules.asks_to_add(text):  # a document's expiry they ask Pisa to keep
        found |= {"add_document", "list_documents"}
    if accountant_rules.asks(text):  # "prepara i documenti per il commercialista"
        found.add("accountant_package")
    if re.search(r"\b(?:e-?mail|mail|posta|newsletter)\b", text, re.IGNORECASE):  # its whole text
        found.add("read_my_email")
    if app_help_rules.asks(text):  # "cosa fa il pulsante in alto a sinistra?"
        found.add("app_help")
    if _OPEN_IN_APP.search(text):  # "aprimi Oggi": the app's own screen (a client that picked `pisa.app`)
        found.add("open_in_app")
    if _SETTING_CHANGE.search(text):  # "il riepilogo alle 7": one of Pisa's settings, on their yes
        found.add("propose_setting_change")
    if learn_rules.about_course(text):  # a turn about a course offers Pisa Learn's tools at once
        found |= learn_rules.LEARN_TOOLS
    if talk_rules.asks_team(text):  # "segnalalo al team": the feedback channel
        found.add("tell_the_team")
    if re.search(r"\baire\b", text, re.IGNORECASE):  # their answer about AIRE
        found.add("note_aire")
    if view_rules.asks_to_save(text):  # "salvala come meteo" keeps a view
        found.add("save_view")
    if visual_rules.asks(text):  # a structure, a flow or numbers to see: the sheet
        found.add("show_sheet")
    if drawing_rules.asks(text):  # a picture serves these words: the drawing tool
        found.add("show_drawing")
    if autonomy_rules.binds(text):  # their yes to Pisa's Sunday proposal ("falli pure senza chiedermelo")
        found.add("accept_autonomy")
    if autonomy_rules.asks_again(text):
        found.add("ask_again")
    # "ricordami di…", "devo…", "non farmi dimenticare…", "aspetto il contratto da Marco": their
    # intention kept (a place they name stays a place note); "cosa sto aspettando?", "a che punto ero?"
    if _LOOPS_LIST.search(text):
        found.add("my_intentions")
    elif (intention_rules.intends(text) or intention_rules.waits(text)) and "remember_at_place" not in found:
        found.add("keep_intention")
    if _LOOPS_RESUME.search(text):
        found.add("resume_intention")
    if countries.said_country(text) is not None or re.search(
        r"\b(?:si\s+pu[oò]\s+fare|can\s+be\s+done)\s+(?:ovunque|anywhere)\b", text, re.IGNORECASE
    ):  # a task bound to a country by their words
        found.add("set_task_country")
    return frozenset(found)


# A model can have these tools in front of it and answer without them ("stasera di
# nuovo sushi" got "ti trovo un posto?", "posso chiamare mamma?" got a guess of the hour). One line, only on
# the turn their words point to one (the first request's prompt stays as it is).
HINTS = {
    # a model may ask in words the guard can't keep (two
    # questions, a statement about them first): the tool first (Pisa then says its question), else ONE
    # question.
    "note_hunch": (
        "Their words say a habit (again, as usual): call note_hunch, guess = ONE short yes-or-no question to "
        'them ("Il sushi del venerdì è ormai una tua abitudine?"), evidence = their words; '
        "Pisa says it for you. "
        "Your reply is that one question and nothing else: nothing about them before it, no second question, "
        "no offer, no one named. Never say their habit as a fact."
    ),
    "their_time": "They ask about someone's hour: their_time says it and when to call; answer from it.",
}


def hints(words: str) -> str:
    """The turn's hints for the tools their words point to, one line each ("" for most turns).

    : when their words already decide (`coverage.no_choice`), that line comes first,
    before any tool is called."""
    found = tools_for(words)
    lines = [line for tool, line in HINTS.items() if tool in found]
    if (decided := coverage.no_choice(words)) is not None:
        lines.insert(
            0,
            f"No options to put to them: {decided} Say each move as your own act with its new time "
            '("Sposto la call alle 16 e la palestra alle 17: va bene?"), never as a maybe.',
        )
    return "\n".join(lines)
