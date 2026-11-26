"""The not-advice line: Pisa informs, never advises, and every count it shows says so.

No I/O here."""

# the words, as a step of a stack or the middle of a line writes them (no full stop)
NOT_ADVICE_IT = "È un conteggio, non una consulenza"
# the line closing a count, in each language
NOT_ADVICE = {"it-IT": f"{NOT_ADVICE_IT}.", "en-US": "It's a count, not advice."}
# the head of a claim's or a bill's closing line in English ("A count, not advice: …")
NOT_ADVICE_EN_HEAD = "A count, not advice"
# what a stored stack is matched with (services/stanotte's LIKE): the Italian words, lower case
NOT_ADVICE_MATCH = "conteggio, non una consulenza"
