# ruff: noqa: E501
"""core-memory-1: the memory scenarios for the depth world (FRONTIER-COMPARISON-RULINGS §1, item 10). What Pisa knows
of them and Pisa's past cards are seeded (`world.claims`, `world.picks`, harness.seed); each turn is played through
Pisa's ConversationService with the scripted model, and scored by code on the turn's instructions:

- `profile_checks.must`: lines the profile block for this turn carries (their words' topic, the past picks of the kind asked);
- `never`: lines it must not carry (another topic: music at breakfast);
- `said`: the scripted model's reply, which says the profile's line only when the instructions hold it (a
  `hint` step), so a profile that misses it answers "non lo so".

Every word is invented: the artists, the places (the depth world's Castelmira), their words.
"""

from typing import Any

N = "2026-11-"  # 10 Nov 2026 is a Tuesday

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "m1-which-music-and-who-to-hear",
        "now": N + "10T19:00",
        "lang": "it-IT",
        "world": {
            "claims": [
                ("like", "musica: house, in particolare tech house", "active", "confirmed",
                 "words:like:musica:a1"),
                # review M4: a hunch is never acted on before their yes: never in a turn
                ("like", "musica: vinili anni 90", "candidate", "inferred", "words:like:musica:a5"),
                ("like", "artisti: i Gufi Elettrici e Nora Vento", "active", "confirmed", "words:like:artisti:a2"),
                ("dislike", "musica: niente EDM commerciale", "active", "confirmed", "words:dislike:musica:a3"),
                ("routine", "colazione: seduto, con calma", "active", "confirmed", "words:routine:colazione:a4"),
                ("constraint", "Mai catene", "active", "said", None),
            ],
        },
        "turns": [{"owner": "che musica mi piace e chi vado a sentire?"}],
        "script": [[{"hint": "musica: house", "then": "Mi sembra che ti piaccia la house, la tech house, e i Gufi "
                     "Elettrici: cerco chi suona.", "else": "Non lo so ancora: dimmelo tu."}]],
        "profile_checks": [{
            "must": ["musica: house, in particolare tech house", "artisti: i Gufi Elettrici",
                     "musica: niente EDM commerciale"],
            "never": ["colazione: seduto", "vinili anni 90", "unconfirmed"],
            "said": "tech house",
        }],
    },
    {
        "id": "m2-the-usual-breakfast-place",
        "now": N + "10T08:30",
        "lang": "it-IT",
        "world": {
            "claims": [
                ("routine", "colazione: seduto, con calma", "active", "confirmed", "words:routine:colazione:a4"),
                ("like", "musica: house, in particolare tech house", "active", "confirmed", "words:like:musica:a1"),
            ],
            "picks": [
                ("Caffè della Piazza", "cafe", 3, ["go", "went"]),
                ("Bar delle Rondini", "cafe", 5, ["dismissed"]),
                ("Pizza al Mulino", "pizza_restaurant", 6, ["went"]),
            ],
        },
        "turns": [{"owner": "il solito posto per colazione?"}],
        "script": [[{"hint": "«Caffè della Piazza» (cafe), Pisa's card", "then": "Il solito: Caffè della Piazza, "
                     "seduto e con calma.", "else": "Non so quale sia il tuo solito."}]],
        "profile_checks": [{
            "must": ["colazione: seduto, con calma", "[past pick] «Caffè della Piazza» (cafe)",
                     "they went, and like it", "[past pick] «Bar delle Rondini» (cafe)", "didn't go"],
            "never": ["musica:", "Pizza al Mulino"],
            "said": "Caffè della Piazza",
        }],
    },
    {
        "id": "m3-like-last-time",
        "now": N + "10T20:30",
        "lang": "it-IT",
        "world": {
            "claims": [
                ("like", "pizza: napoletana, bordo alto", "active", "confirmed", "words:like:pizza:a5"),
                ("routine", "colazione: seduto, con calma", "active", "confirmed", "words:routine:colazione:a4"),
            ],
            "picks": [
                ("Pizza al Mulino", "pizza_restaurant", 6, ["go", "went"]),
                ("Caffè della Piazza", "cafe", 3, ["went"]),
            ],
        },
        "turns": [{"owner": "come l'altra volta"}],
        "script": [[{"hint": "«Pizza al Mulino» (pizza restaurant)", "then": "Come l'altra volta: Pizza al "
                     "Mulino?", "else": "Quale altra volta?"}]],
        "profile_checks": [{
            "must": ["[past pick] «Pizza al Mulino» (pizza restaurant)", "they went, and like it"],
            "never": ["Caffè della Piazza", "colazione: seduto"],
            "said": "Pizza al Mulino",
        }],
    },
]  # fmt: skip


def problems(scenario: dict[str, Any], told: list[str], replies: list[str]) -> list[str]:
    """What the turns' instructions and replies miss of the scenario's `profile_checks`."""
    out: list[str] = []
    for n, spec in enumerate(scenario["profile_checks"]):
        instructions = told[n] if n < len(told) else ""
        reply = replies[n] if n < len(replies) else ""
        out += [
            f"turn {n + 1}: the profile lacks {line!r}" for line in spec["must"] if line not in instructions
        ]
        out += [
            f"turn {n + 1}: the profile carries {line!r}" for line in spec["never"] if line in instructions
        ]
        if spec["said"] not in reply:
            out.append(f"turn {n + 1}: Pisa said {reply!r}, not {spec['said']!r}")
    return out
