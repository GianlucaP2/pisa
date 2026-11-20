# ruff: noqa: E501
"""The understanding step's evaluation set (`tests/data/understand/utterances.jsonl`), synthetic and seeded.

The synthetic owner is `tests.understand_lexicon` (an Italian from Turin in Rotterdam for an invented
market). Each line is one utterance as the recogniser wrote it, with what the user meant, the names Pisa should
fix and the question Pisa should ask. The categories and how many of each (by language and split) are fixed below;
the words come from the templates and the misheard forms here, picked by a seeded generator. Every name is
invented.

    uv run python -m tests.support.synth.utterances   # rewrites the file; the same seed gives the same file
"""

import hashlib
import json
import random
from pathlib import Path
from typing import Any

from pisa.domain.phonetic import fold

SEED = 49
OUT = Path(__file__).resolve().parents[2] / "data" / "understand" / "utterances.jsonl"
LEXICON = "synthetic-rotterdam"

# (category, lang, split) → how many
COUNTS: dict[tuple[str, str, str], int] = {
    ("name_fix", "it", "dev"): 16, ("name_fix", "it", "test"): 56,
    ("name_fix", "en", "dev"): 26, ("name_fix", "en", "test"): 40,
    ("name_ok", "it", "dev"): 4, ("name_ok", "it", "test"): 16,
    ("name_ok", "en", "dev"): 6, ("name_ok", "en", "test"): 14,
    ("control_word", "it", "dev"): 6, ("control_word", "it", "test"): 18,
    ("control_word", "en", "dev"): 7, ("control_word", "en", "test"): 16,
    ("control_new", "it", "dev"): 8, ("control_new", "it", "test"): 4,
    ("control_new", "en", "dev"): 3, ("control_new", "en", "test"): 7,
    ("repair", "it", "dev"): 4, ("repair", "it", "test"): 11,
    ("repair", "en", "dev"): 5, ("repair", "en", "test"): 7,
    ("false_start", "it", "dev"): 3, ("false_start", "it", "test"): 2,
    ("false_start", "en", "dev"): 1, ("false_start", "en", "test"): 3,
    ("repair_name", "it", "dev"): 3, ("repair_name", "it", "test"): 2,
    ("repair_name", "en", "dev"): 2, ("repair_name", "en", "test"): 3,
    ("control_repair", "it", "dev"): 2, ("control_repair", "it", "test"): 3,
    ("control_repair", "en", "dev"): 1, ("control_repair", "en", "test"): 4,
    ("ask", "it", "dev"): 3, ("ask", "it", "test"): 8,
    ("ask", "en", "dev"): 6, ("ask", "en", "test"): 5,
    ("control_ask", "it", "test"): 3, ("control_ask", "en", "test"): 3,
}  # fmt: skip

# How the recogniser writes each name it doesn't know (some are real words, some only the model can confirm)
MISHEARD: dict[str, list[str]] = {
    "Lantaarn Hostel Rotterdam": ["lanterna", "lantarn", "lanterna hostel", "lantern hostel", "lantarne"],
    "BRAM'S Broodjes": [
        "bram brodies",
        "brams brodjes",
        "bram s broodies",
        "bramms brotjes",
        "brahms broodjes",
    ],
    "Kraanhal": ["kraan hal", "kran hal", "kranal", "craan hall"],
    "Graansilofabriek": ["graan silo fabriek", "gran silo fabrik", "grain silo fabric", "gransilo fabriek"],
    "Haverbrug": ["haver brug", "averbrug", "haver brook", "haverbrook"],
    "Utrecht": ["utreht", "u trecht", "ootrecht"],
    "Café de Mol": ["caffe de moll", "cafe de mole", "cafe da mol", "cafe the mol"],
    "Spiegelmuseum": ["spigel museum", "spiegel museo", "spiegl museum", "shpiegel museum"],
    "Kip & Kop": ["kip e kop", "kipp e kopp", "kip and cop", "kip n kop"],
    "WKH": ["w k h"],
    "Wilgenpark": ["wilgen park", "vilgenpark", "willgen park"],
    "Zestienhoven": ["zestien hoven", "sestienhoven", "zesteen hoven"],
    "Femke de Wit": ["femche de vit", "femke de vit", "femke the wit"],
    "Dario Ferri": ["dario feri", "dario ferry", "daryo ferri"],
    "Chiara Marini": ["chiara mariny", "chiara marinni", "kiara marini", "chiara marine"],
    "Joost": ["iost", "yoost", "joest"],
    "Lichtjesmarkt": ["lichtjes markt", "likjes markt", "lichtjesmark"],
}
KIND = {
    **{n: "place" for n in (
        "Lantaarn Hostel Rotterdam", "BRAM'S Broodjes", "Kraanhal", "Graansilofabriek", "Haverbrug", "Utrecht",
        "Café de Mol", "Spiegelmuseum", "Kip & Kop", "WKH", "Wilgenpark", "Zestienhoven",
    )},
    **{n: "person" for n in ("Femke de Wit", "Dario Ferri", "Chiara Marini", "Joost")},
    "Lichtjesmarkt": "event",
}  # fmt: skip
SAID_RIGHT = {  # forms said as Pisa knows them
    "place": ["lantaarn", "brams broodjes", "kraanhal", "haverbrug", "graansilo", "de mol", "spiegelmuseum",
              "kip & kop", "wilgenpark", "zestienhoven", "rotterdam centraal", "centraal station"],
    "person": ["femke", "dario ferri", "joost", "chiara marini", "irene"],
    "event": ["lichtjesmarkt", "kade"],
}  # fmt: skip
CONTEXTS: dict[str, dict[str, list[str]]] = {
    "place": {
        "it": ["portami al {x}", "quanto ci metto per arrivare al {x}", "a che ora chiude {x}",
               "andiamo da {x} dopo pranzo", "quanto manca al {x}", "segna {x} per stasera"],
        "en": ["take me to {x}", "how long to {x} from here", "when does {x} close", "how far is the {x}",
               "lets meet at {x} later"],
    },
    "person": {
        "it": ["chiama {x}", "scrivi a {x} che arrivo tardi", "ricordami di rispondere a {x}"],
        "en": ["call {x}", "text {x} that im running late", "remind me to answer {x}"],
    },
    "event": {
        "it": ["a che ora apre il {x}", "i biglietti per il {x} sono pronti"],
        "en": ["what time does the {x} open", "are my tickets for the {x} ready"],
    },
}  # fmt: skip

CONTROL_WORD = {
    "it": [
        "la lanterna del giardino e rotta", "accendi la lanterna in terrazzo", "prendiamo il ferry delle sei",
        "sono contento che sia sabato", "devo pagare la bolletta della luce", "andiamo al mercato dopo pranzo",
        "voglio un te caldo", "compra il pane integrale", "chiudi la porta del balcone", "che tempo fa oggi",
        "metti la musica piu bassa", "il treno e in ritardo", "ho fame", "dove ho lasciato le chiavi",
        "domani piove", "spegni la luce in cucina", "mi serve un ombrello", "il museo e chiuso il lunedi",
        "la festa e stata bellissima", "porta il cane al parco", "compra il latte e le uova",
        "il caffe e troppo forte", "chiama un taxi", "quanto costa il biglietto del tram", "che ore sono",
        "devo stirare la camicia", "il parcheggio e pieno", "fa freddo stasera",
    ],
    "en": [
        "i bought a new camera", "the ferry leaves at six", "turn off the kitchen light", "i am hungry",
        "where did i leave my keys", "it is going to rain tomorrow", "the museum is closed on mondays",
        "the party was great", "buy some milk and eggs", "the coffee is too strong", "call a taxi",
        "how much is a tram ticket", "what time is it", "the train is late", "the parking lot is full",
        "it is cold tonight", "play some music", "i need an umbrella", "the lantern on the porch is broken",
        "lets have a quiet evening", "open the window please", "the market is busy today",
        "my phone is almost dead", "set the table for two", "the bakery sells good bread",
        "remind me to water the plants", "i lost my scarf",
    ],
}  # fmt: skip
CONTROL_NEW = {
    "it": [
        "andiamo al ristorante de zwaan che dice femke", "prenota da pizzeria aurora", "c e un bar che si chiama "
        "vos", "andiamo al bar del porto", "segna il cafe de kogel", "ricordami di chiamare ottavia",
        "il museo di storia naturale apre alle nove", "andiamo al mercato della binnenrotte",
        "c e una libreria che si chiama boekhuis", "scrivi a bartolomeo", "prenota alla trattoria il gelso",
        "andiamo al teatro luxor",
    ],
    "en": [
        "take me to cafe de kogel", "book a table at pizzeria aurora", "theres a bar called vos",
        "remind me to call ottavia", "lets go to the binnenrotte market", "text bartolomeo",
        "the bookshop is called boekhuis", "we could try the gelso trattoria", "the luxor theatre opens at seven",
        "go to the harbour bar",
    ],
}  # fmt: skip
REPAIR = {  # (said, meant)
    "it": [
        ("devo andare li anzi all altro posto", "devo andare all altro posto"),
        ("prenota per le sette anzi per le otto", "prenota per le otto"),
        ("sposta il pranzo a giovedi no a venerdi", "sposta il pranzo a venerdi"),
        ("ricordamelo stasera anzi domani", "ricordamelo domani"),
        ("metti la sveglia alle cinque anzi no alle sei", "metti la sveglia alle sei"),
        ("andiamo a piedi scusa in bici", "andiamo in bici"),
        ("dopo cena o meglio dopo il film", "dopo il film"),
        ("fammi vedere il calendario volevo dire la mappa", "fammi vedere la mappa"),
        ("compra il pane anzi i cornetti", "compra i cornetti"),
        ("chiamami alle tre anzi alle quattro", "chiamami alle quattro"),
        ("vado in ufficio no a casa", "vado a casa"),
        ("prendi il tram anzi la metro", "prendi la metro"),
        ("segna martedi anzi mercoledi", "segna mercoledi"),
        ("mandagli un messaggio scusa una mail", "mandagli una mail"),
        ("prenota per tre anzi per quattro", "prenota per quattro"),
    ],
    "en": [
        ("book it for seven i mean eight", "book it for eight"),
        ("remind me tonight no wait tomorrow", "remind me tomorrow"),
        ("move lunch to thursday sorry friday", "move lunch to friday"),
        ("take the tram i mean the metro", "take the metro"),
        ("call me at three no wait at four", "call me at four"),
        ("show me the calendar i mean the map", "show me the map"),
        ("buy bread i mean croissants", "buy croissants"),
        ("set it for tuesday i mean wednesday", "set it for wednesday"),
        ("send him a text sorry an email", "send him an email"),
        ("a table for three i mean four", "a table for four"),
        ("go to the office no wait to the station", "go to the station"),
        ("after dinner i mean after the film", "after the film"),
    ],
}  # fmt: skip
FALSE_START = {
    "it": [
        ("devo andare li devo andare all altro posto", "devo andare all altro posto"),
        ("vorrei vorrei un tavolo per tre", "vorrei un tavolo per tre"),
        ("ehm allora ehm che tempo fa stasera", "allora che tempo fa stasera"),
        ("il il tram passa quando", "il tram passa quando"),
        ("devo devo comprare le uova", "devo comprare le uova"),
    ],
    "en": [
        ("i need i need to leave at seven", "i need to leave at seven"),
        ("um where um where is the station", "where is the station"),
        ("can can you check the forecast", "can you check the forecast"),
        ("so so when is my train", "so when is my train"),
    ],
}  # fmt: skip
CONTROL_REPAIR = {
    "it": ["non e caro anzi e economico", "no non voglio uscire", "no grazie", "non mi convince anzi mi piace",
           "scusa che giorno e oggi"],
    "en": ["it is actually quite close", "no i dont need that", "sorry i didnt catch that",
           "actually that works", "no thanks"],
}  # fmt: skip
ASK = {  # (said, ask)
    "it": [
        ("metti la sveglia alle ventisette", "time"), ("prenota alle ventotto", "time"),
        ("sveglia alle diciannove di mattina", "time"),
        ("domani lunedi devo chiamare chiara", "day"), ("ricordamelo domani giovedi", "day"),
        ("oggi martedi andiamo al wilgenpark", "day"),
        ("torno al lantaarn a torino", "city"), ("portami allo spiegelmuseum di torino", "city"),
        ("andiamo da tabacchi gallo a rotterdam", "city"),
        ("chiama elena", "which"), ("scrivi a elena che arrivo", "which"),
    ],
    "en": [
        ("set an alarm at twenty seven", "time"), ("book it at thirty two", "time"),
        ("wake me at seventeen in the morning", "time"),
        ("tomorrow monday call dario", "day"), ("today thursday i go to the spiegelmuseum", "day"),
        ("remind me tomorrow tuesday", "day"),
        ("take me to the wilgenpark in turin", "city"), ("is gino a vanchiglia in rotterdam", "city"),
        ("the haverbrug in turin", "city"),
        ("call elena", "which"), ("text elena that im late", "which"),
    ],
}  # fmt: skip
CONTROL_ASK = {
    "it": ["domani domenica devo chiamare chiara", "metti la sveglia alle sei di mattina",
           "torno al lantaarn a rotterdam"],
    "en": ["tomorrow sunday call dario", "today saturday i go to the spiegelmuseum",
           "is gino a vanchiglia in turin"],
}  # fmt: skip


def _item(category: str, lang: str, split: str, text: str, meant: str, fixes: list[list[str]],
          ask: str | None) -> dict[str, Any]:  # fmt: skip
    ident = hashlib.sha1(f"{category}|{lang}|{text}".encode()).hexdigest()[:10]  # noqa: S324 - an id, not a secret
    return {"id": ident, "lang": lang, "category": category, "text": text, "lexicon": LEXICON,
            "expected": {"meant": meant, "corrections": fixes, "ask": ask}, "split": split}  # fmt: skip


def _name_fix(rng: random.Random, lang: str) -> tuple[str, str, list[list[str]]]:
    name = rng.choice(sorted(MISHEARD))
    heard = rng.choice(MISHEARD[name])
    ctx = rng.choice(CONTEXTS[KIND[name]][lang])
    return ctx.format(x=heard), ctx.format(x=fold(name).strip()), [[heard, name]]


def _name_ok(rng: random.Random, lang: str) -> str:
    kind = rng.choice(["place", "place", "person", "event"])
    return rng.choice(CONTEXTS[kind][lang]).format(x=rng.choice(SAID_RIGHT[kind]))


def _repair_name(rng: random.Random, lang: str) -> tuple[str, str, list[list[str]]]:
    name = rng.choice([n for n in sorted(MISHEARD) if KIND[n] in ("place", "person")])
    heard = rng.choice(MISHEARD[name])
    firsts = ["femke", "joost", "irene", "dario"] if KIND[name] == "person" else SAID_RIGHT["place"]
    first = rng.choice([s for s in firsts if fold(s) not in fold(name)])
    if KIND[name] == "person":
        verb, glue = rng.choice(
            [("chiama", ""), ("scrivi a", "a ")] if lang == "it" else [("call", ""), ("text", "")]
        )
        term = rng.choice(["anzi", "no"] if lang == "it" else ["sorry", "i mean"])
        return f"{verb} {first} {term} {glue}{heard}", f"{verb} {fold(name).strip()}", [[heard, name]]
    verb = rng.choice(["portami al", "andiamo al"] if lang == "it" else ["take me to", "lets go to"])
    term = rng.choice(["anzi al", "no al"] if lang == "it" else ["actually to", "no wait to"])
    return f"{verb} {first} {term} {heard}", f"{verb} {fold(name).strip()}", [[heard, name]]


def build(seed: int = SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)  # noqa: S311 - a reproducible data set, not a secret
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    pools: dict[tuple[str, str], list[Any]] = {}
    for (cat, lang), pool in {
        **{("control_word", lang): list(v) for lang, v in CONTROL_WORD.items()},
        **{("control_new", lang): list(v) for lang, v in CONTROL_NEW.items()},
        **{("repair", lang): list(v) for lang, v in REPAIR.items()},
        **{("false_start", lang): list(v) for lang, v in FALSE_START.items()},
        **{("control_repair", lang): list(v) for lang, v in CONTROL_REPAIR.items()},
        **{("ask", lang): list(v) for lang, v in ASK.items()},
        **{("control_ask", lang): list(v) for lang, v in CONTROL_ASK.items()},
    }.items():
        rng.shuffle(pool)
        pools[(cat, lang)] = pool
    for (cat, lang, split), n in COUNTS.items():
        made = 0
        while made < n:
            fixes: list[list[str]] = []
            ask: str | None = None
            if cat == "name_fix":
                text, meant, fixes = _name_fix(rng, lang)
            elif cat == "name_ok":
                text = meant = _name_ok(rng, lang)
            elif cat == "repair_name":
                text, meant, fixes = _repair_name(rng, lang)
            else:
                pick = pools[(cat, lang)].pop()
                if cat in ("repair", "false_start"):
                    text, meant = pick
                elif cat == "ask":
                    (text, ask), meant = pick, pick[0]
                else:
                    text = meant = pick
            if text in seen:
                continue
            seen.add(text)
            out.append(_item(cat, lang, split, text, meant, fixes, ask))
            made += 1
    return out


def main() -> None:
    OUT.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in build()))


if __name__ == "__main__":
    main()
