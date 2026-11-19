# ruff: noqa: E501
"""The frontier comparison's ten place requests
(a01, a02, a03, a05, a07, a10, a13, a19, a24,
a28) as depth scenarios on the recorded world, with synthetic places in Borgo Lieto (`depth_world`) standing
for the real ones. Each script is what a deep brain does (the map, the guides, `show_findings`), with the
model's own mistakes of 10 Nov in its words where they fit: a pick closed at the asked time (a05), a lead that
counts (a02), an internal note said to them (a01, a02), a correction carried to another topic (a13, a28).

`EXPECT` says, per scenario, what code must make of it: the pick, the places a hard requirement excludes, the
recognition the lead names, the language. Every word here is invented; every address ends in `.example`.
"""

from typing import Any

from tests.replay.depth import CUFFIE, LANTERNE, call, ms, pd, rp, ws
from tests.replay.depth_world import place, walk_min

TUE = "2026-11-10T"
SAT = "2026-11-14T"
G_BRINA = "https://guidabrina.example/borgo-lieto/colazioni"
G_BRINA_R = "https://guidabrina.example/borgo-lieto/ristoranti"
G_ROTONDA = "https://rotondapizze.example/2027/borgo-lieto"
G_OSTERIE = "https://guidaosterie.example/borgo-lieto"
M_TRALCIO = "https://osteriatralcio.example/menu"
M_POZZO = "https://osteriaalpozzo.example/menu"
P_LAVORO = "https://ilfaro.example/borgo-lieto/lavorare"
ANAGRAFE = "https://comune-castelmira.example/anagrafe"
REWRITE = "were not said to the user"  # the rewrite round's instruction (conversation.REWRITE_LINE)


def wm(key: str) -> int:
    return walk_min(place(key), "lieto")


def findings(
    title: str, request: str, criteria: list[str], pick: str, rows: list[tuple[str, str]], lead: str
) -> Any:
    return call(
        "show_findings",
        title=title,
        request=request,
        criteria=criteria,
        pick=place(pick).id,
        places=[{"place_id": place(k).id, "why": why} for k, why in rows],
        lead=lead,
    )


def rewritten(said: str, then: str) -> dict[str, Any]:
    """The model's words with Pisa's own note in them; asked to rewrite (REWRITE in its instructions), `then`."""
    return {"hint": REWRITE, "then": then, "else": said}


COMPARE: list[dict[str, Any]] = []


def scenario(
    sid: str,
    now: str,
    lang: str,
    turns: list[str],
    script: list[Any],
    depth: list[dict[str, Any]],
    typed: bool = False,
) -> None:
    COMPARE.append(
        {
            "id": sid,
            "now": now,
            "lang": lang,
            "area": "lieto",
            "world": {},
            "max_questions": 1,
            "typed": typed,
            "turns": [{"owner": t, "advance_s": 0 if i == 0 else 40} for i, t in enumerate(turns)],
            "script": script,
            "depth": depth,
            "group": "depth-compare",
        }
    )


PICK = {"candidates": 3, "sources": ["map", "guide"], "table": 3, "short": True}

# a01: breakfast from the guides; the guide's other place opens at 9:00 ("apre alle 9:00", ChatGPT's line)
scenario(
    "a01-breakfast-open-at-the-asked-time",
    TUE + "08:20",
    "it-IT",
    ["Colazione in un posto segnalato dalle guide, qui vicino?", "Perché quella e non Caffè Vetrata?"],
    [
        [
            [
                ms("colazione bar"),
                ms("pasticceria cornetti"),
                ws("colazioni Borgo Lieto guida", language="it"),
            ],
            [rp(G_BRINA), pd("caffe-vetrata"), pd("pasticceria-aurora")],
            [
                findings(
                    "Colazione segnalata",
                    "colazione da guida qui vicino",
                    ["segnalato da una guida", "aperto adesso"],
                    "caffe-vetrata",
                    [
                        ("caffe-vetrata", "il brunch segnalato dalla guida"),
                        ("pasticceria-aurora", "Tazza d'oro, cornetti alla crema"),
                        ("bar-borgo", "veloce al banco"),
                    ],
                    f"Per te ora Caffè Vetrata, segnalato dalla Guida Brina, a {wm('caffe-vetrata')} minuti.",
                )
            ],
            "Te li metto sul telefono.",
        ],
        [
            [call("my_findings", place="Caffè Vetrata")],
            rewritten(
                "Caffè Vetrata apre alle 9:00, dopo l'ora in cui chiedevi. Non è in lista: ignoralo.",
                "Caffè Vetrata apre solo alle 9:00, e tu volevi fare colazione adesso.",
            ),
        ],
    ],
    [PICK | {"recognised": ["pasticceria-aurora"], "table": 2}, {"cites": [r"9:00", r"apre"], "short": True}],
)

# a02: pizza from the guides; the lead that counts (23 places) and the follow-up's internal line
scenario(
    "a02-pizza-the-guides-and-which-guides",
    TUE + "20:00",
    "it-IT",
    ["Una pizzeria da guida qui vicino?", "Quali guide la citano?"],
    [
        [
            [ms("pizzeria"), ws("migliori pizzerie Borgo Lieto guida", language="it")],
            [rp(G_ROTONDA), pd("fornace-nova"), pd("pizza-svelta")],
            [
                findings(
                    "Pizza da guida",
                    "una pizzeria da guida",
                    ["citata dalle guide", "a piedi"],
                    "fornace-nova",
                    [
                        ("fornace-nova", "Tre Pale"),
                        ("pizzeria-ciottolo", "Due Pale"),
                        ("pizza-svelta", "veloce"),
                    ],
                    f"Leggo le guide delle pizzerie. Ho trovato 23 posti, il resto è sul telefono. Per te ora ti direi Pizzeria Fornace Nova, a {wm('fornace-nova')} minuti a piedi.",
                )
            ],
            "",
        ],
        [
            [call("my_findings", place="Pizzeria Fornace Nova")],
            rewritten(
                "Rotonda Pizze le dà le Tre Pale 2027. Non l'ho messo: non ho cambiato niente.",
                "È la terza volta di fila che Rotonda Pizze la premia.",
            ),
        ],
    ],
    [PICK | {"recognised": ["fornace-nova"]}, {"cites": [r"Rotonda Pizze", r"Tre Pale"], "short": True}],
)

# a03: dinner for two tomorrow, recognised, asked in English (typed); Belvedere is closed on Wednesday
scenario(
    "a03-dinner-for-two-recognised-in-english",
    TUE + "18:00",
    "it-IT",
    ["Dinner for two tomorrow night, somewhere recognised near here?"],
    [
        [
            [ms("ristorante cena"), ws("migliori ristoranti Borgo Lieto guida", language="it")],
            [rp(G_BRINA_R), pd("osteria-fiordaliso"), pd("ristorante-belvedere")],
            [
                findings(
                    "Dinner for two",
                    "dinner for two tomorrow",
                    ["recognised", "for two", "tomorrow night"],
                    "ristorante-belvedere",
                    [
                        ("ristorante-belvedere", "the view over the village"),
                        ("osteria-fiordaliso", "Stella Brina, seasonal food"),
                        ("trattoria-rondella", "Bib Brina, family trattoria"),
                    ],
                    "I'd go for Ristorante Belvedere, for the view.",
                )
            ],
            "",
        ],
    ],
    [PICK | {"recognised": ["osteria-fiordaliso"], "table": 2}],
    typed=True,
)

# a05: an osteria tomorrow at lunch under €35: Grappolo is closed on Wednesday at lunch, Tralcio's menu is 36 €
scenario(
    "a05-osteria-tomorrow-lunch-under-35",
    TUE + "17:30",
    "it-IT",
    ["Un'osteria per domani a pranzo, sotto i 35 euro?", "Com'è il menu?"],
    [
        [
            [ms("osteria"), ms("trattoria"), ws("osterie Borgo Lieto guida", language="it")],
            [rp(G_OSTERIE), rp(G_BRINA_R), pd("osteria-grappolo"), pd("osteria-pozzo")],
            [ws("osteria del tralcio menu prezzi"), ws("osteria al pozzo menu pranzo")],
            [rp(M_TRALCIO), rp(M_POZZO)],
            [
                findings(
                    "Osteria domani a pranzo",
                    "osteria domani a pranzo sotto i 35 euro",
                    ["domani a pranzo", "sotto i 35 euro"],
                    "osteria-grappolo",
                    [
                        ("osteria-grappolo", "Bib Brina, cacio e pepe"),
                        ("osteria-tralcio", "Chiocciola 2027"),
                        ("osteria-pozzo", "pasta fatta in casa, conto onesto"),
                        ("trattoria-mestolo", "casalinga"),
                    ],
                    "Per domani a pranzo direi Osteria del Grappolo: Bib Brina.",
                )
            ],
            "",
        ],
        [
            [call("my_findings", place="Osteria al Pozzo")],
            "Il menu del pranzo costa 24 euro: tagliatelle al ragù, pollo arrosto con patate e crostata.",
        ],
    ],
    [
        PICK | {"recognised": ["osteria-pozzo"], "table": 2},
        {"cites": [r"24 euro", r"tagliatelle"], "short": True},
    ],
)

# a07: a quiet café to work, asked in English (typed); Chiasso's wifi is slow and it has no sockets
scenario(
    "a07-a-quiet-cafe-to-work-in-english",
    TUE + "15:00",
    "it-IT",
    ["A quiet café to work this afternoon, with wifi and sockets?"],
    [
        [
            [ms("caffè lavorare wifi"), ws("lavorare al caffè Borgo Lieto wifi prese", language="it")],
            [rp(P_LAVORO), pd("caffe-pagina"), pd("caffe-chiasso"), pd("bar-ponte")],
            [
                findings(
                    "A café to work",
                    "a quiet café to work",
                    ["quiet", "wifi", "sockets"],
                    "caffe-chiasso",
                    [
                        ("caffe-chiasso", "close by"),
                        ("caffe-pagina", "sockets at every table"),
                        ("bar-ponte", "quiet in the afternoon"),
                    ],
                    "Caffè Chiasso is the closest.",
                )
            ],
            "",
        ],
    ],
    [PICK | {"recognised": ["caffe-pagina"], "table": 2, "sources": ["map"]}],
    typed=True,
)

# a10: a rainy Saturday with the kids, in English (typed): the ludoteca is closed on Saturday
scenario(
    "a10-rainy-saturday-with-the-kids-in-english",
    TUE + "16:00",
    "it-IT",
    ["Rainy Saturday with the kids, 5 and 8: where can we go?"],
    [
        [
            [ms("museo bambini"), ms("ludoteca bambini"), ms("teatro burattini")],
            [pd("museo-giochi"), pd("ludoteca-arcobaleno"), pd("teatrino-lieto")],
            [
                findings(
                    "Rainy Saturday",
                    "a rainy Saturday with the kids",
                    ["indoors", "for 5 and 8"],
                    "ludoteca-arcobaleno",
                    [
                        ("ludoteca-arcobaleno", "games for small kids"),
                        ("museo-giochi", "workshops from 4"),
                        ("teatrino-lieto", "puppets at 16"),
                    ],
                    "On Saturday, Ludoteca Arcobaleno.",
                )
            ],
            "",
        ],
    ],
    [{"candidates": 3, "table": 2, "short": True}],
    typed=True,
)

# a13: headphones under €200, after a Saturday question: the correction of Saturday never travels here
scenario(
    "a13-headphones-never-a-correction-of-saturday",
    TUE + "18:00",
    "it-IT",
    ["Cosa facciamo sabato se piove?", "Cuffie con cancellazione del rumore sotto i 200 euro: quali?"],
    [
        [
            [ms("museo bambini")],
            [pd("museo-giochi")],
            "Sabato c'è il Museo dei Giochi, aperto dalle 10 alle 19.",
        ],
        [
            [ws("cuffie cancellazione rumore 200 euro migliori", language="it")],
            [rp(CUFFIE)],
            "Le Sonora Quiet 7 a 179 euro. Nella risposta sul sabato ho dato orari che non avevo verificato.",
        ],
    ],
    [{}, {"short": True}],
)

# a19: tonight in town (the festival page, read)
scenario(
    "a19-tonight-in-town",
    SAT + "17:00",
    "it-IT",
    ["Cosa c'è stasera in città?"],
    [
        [
            [ws("eventi stasera Castelmira festival", language="it")],
            [rp(LANTERNE)],
            "Stasera c'è il Festival delle Lanterne, dalle 18 alle 23, ingresso 12 euro.",
        ]
    ],
    [{"short": True}],
)

# a24: the registry office on Thursday (a fact, quick)
scenario(
    "a24-the-registry-office-on-thursday",
    TUE + "11:00",
    "it-IT",
    ["A che ora è aperta l'anagrafe giovedì? Serve l'appuntamento per la carta d'identità?"],
    [
        [
            [ws("anagrafe orari comune Castelmira", language="it")],
            [rp(ANAGRAFE)],
            "Giovedì l'anagrafe è aperta dalle 8:30 alle 12:30 e dalle 14:30 alle 17:00. Per la carta d'identità serve l'appuntamento.",
        ]
    ],
    [{"short": True}],
)

# a28: a coffee now, after tonight's question: the correction of the evening never travels to the coffee
scenario(
    "a28-a-coffee-now-never-a-correction-of-tonight",
    SAT + "17:00",
    "it-IT",
    ["Cosa c'è stasera in città?", "Un caffè qui vicino, adesso"],
    [
        [
            [ws("eventi stasera Castelmira festival", language="it")],
            [rp(LANTERNE)],
            "Stasera c'è il Festival delle Lanterne, dalle 18, ingresso 12 euro.",
        ],
        [
            [ms("bar caffè", open_now=True)],
            "Bar del Borgo, a un minuto, aperto adesso. Stasera non ho verificato che il festival inizi alle 18.",
        ],
    ],
    [{}, {"short": True}],
)

# What code must make of each (the targets): the pick shown first, the places a hard requirement
# excludes (named on the sheet with the reason), the recognition the lead names, the language, the lines that
# must never reach them
EXPECT: dict[str, dict[str, Any]] = {
    "a01-breakfast-open-at-the-asked-time": {
        "pick": "pasticceria-aurora",
        "excluded": {"caffe-vetrata": "apre alle 9:00"},
        "named": "Tazza d'oro",
        "never": ["ignoralo", "Non è in lista"],
    },
    "a02-pizza-the-guides-and-which-guides": {
        "pick": "fornace-nova",
        "named": "Tre Pale",
        "never": ["Ho trovato 23", "non ho cambiato niente", "Non l'ho messo"],
    },
    "a03-dinner-for-two-recognised-in-english": {
        "pick": "osteria-fiordaliso",
        "excluded": {"ristorante-belvedere": "closed on Wednesday"},
        "named": "Stella Brina",
        "lang": "en",
    },
    "a05-osteria-tomorrow-lunch-under-35": {
        "pick": "osteria-pozzo",
        "excluded": {
            "osteria-grappolo": "chiuso il mercoledì a pranzo",
            "osteria-tralcio": "36 € a persona, sopra i 35 €",
        },
        "named": "segnalata dalla Guida delle Osterie",
    },
    "a07-a-quiet-cafe-to-work-in-english": {
        "pick": "caffe-pagina",
        "excluded": {"caffe-chiasso": "not for working"},
        "lang": "en",
    },
    "a10-rainy-saturday-with-the-kids-in-english": {
        "pick": "museo-giochi",
        "excluded": {"ludoteca-arcobaleno": "closed on Saturday"},
        "lang": "en",
    },
    "a13-headphones-never-a-correction-of-saturday": {"never_after": ["sabato", "non avevo verificato"]},
    "a19-tonight-in-town": {},
    "a24-the-registry-office-on-thursday": {},
    "a28-a-coffee-now-never-a-correction-of-tonight": {"never_after": ["non ho verificato", "festival"]},
}
