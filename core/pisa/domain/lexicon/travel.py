"""How a line says a travel mode ("12 min a piedi", "12 min by public transport").

No I/O here."""

# the four modes a route is asked in
MODE_WORDS: dict[str, dict[str, str]] = {
    "it-IT": {"drive": "in auto", "transit": "con i mezzi", "walk": "a piedi", "bike": "in bici"},
    "en-US": {"drive": "by car", "transit": "by public transport", "walk": "on foot", "bike": "by bike"},
}
# the planner's modes: the four, and a taxi (routed by car)
PLANNER_MODE_WORDS: dict[str, dict[str, str]] = {
    "it-IT": {**MODE_WORDS["it-IT"], "taxi": "in taxi"},
    "en-US": {**MODE_WORDS["en-US"], "taxi": "by taxi"},
}
