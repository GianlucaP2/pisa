"""The example phrases Pisa's fixed prompts quote, one table per language.

The prompts' rules are written once, in English, with a `{name}` for each example; the examples come from
the table of Pisa's language (PISA_LANGUAGE), so the cached prefix stays byte-identical turn after turn. A
turn in the other language still gets its own language instruction (conversation.language_instruction).
Both tables have the same keys (a test holds them to it). No I/O."""

from pisa.domain import locale

EXAMPLES: dict[str, dict[str, str]] = {
    "en": {
        # the brain's prompt (brain/conversation.py)
        "register": 'Plain English or Italian with "tu"',
        "promise": '"I\'ll move them", "I\'ll remind you"',
        "purpose": '"so you get there on time"',
        "specific_question": '"3 PM or 5 PM?"',
        "yes_no": '"yes" or "no"',
        "why_did_you": '"Why did you do that?"',
        "spoken_times": '"at half past 3", "Friday at 10"',
        "no_filler": 'no "Anything else?", no "Certainly!". After "thanks"',
        # the decision of what to say unasked (brain/attend.py)
        "informal": "informal, no filler",
        "merged": '"Three things: …"',
        # the talker's rules (domain/whisper.py)
        "talker_register": 'plain English or Italian "tu"',
        "holding_check": '"One moment, checking."',
        "say_back": '"A pizza place from the guide, not just the nearest… looking."',
        "holding": '"One moment."',
        "announcing": '"I can…", "I\'m opening…", "I apologise…"',
        "own_cause": '"I had a problem…", "it got cut off…"',
        "own_promise": '"I\'ll try to fix it…", "I\'ll let you know…"',
        "correction": '"Actually, it\'s closed today."',
        "emotion_words": "stressed, anxious, sad…",
        "named_feeling": '"You sound rushed"',
        "wave_away": '"rough day? Or I\'ll leave it."',
        "aside": '"no, you go ahead…"',
        "fillers": '"Go ahead, I\'m here", "I\'m listening" or "I didn\'t get that"',
        "talker_filler": 'no "Certainly!", no "Anything else?"',
        "misheard_language": "English as German",
        # the voice bible (domain/voice_bible.md)
        "servant": '"Certainly!", "How can I help you?"',
        "narrator": '"Pisa moved…", "Pisa is working on it"',
        "first_person": '"I moved it", "I\'m working on it"',
        "bible_register": 'plain English or Italian with "tu"',
        "borrowed_words": 'an Italian word in English where they would use one ("aperitivo", "piazza")',
        "done": '"Done."',
        "got_it_wrong": '"I got that wrong: …"',
    },
    "it": {
        "register": 'Italian "tu" or plain English',
        "promise": '"li sposto", "te lo ricordo"',
        "purpose": '"così arrivi in tempo"',
        "specific_question": '"Alle 15 o alle 17?"',
        "yes_no": '"sì" or "no"',
        "why_did_you": '"Perché l\'hai fatto?"',
        "spoken_times": '"alle 3 e mezza", "venerdì alle 10"',
        "no_filler": 'no "C\'è altro?", no "Certamente!". After "grazie"',
        "informal": '"tu", no filler',
        "merged": '"Tre cose: …"',
        "talker_register": 'Italian "tu" or English',
        "holding_check": '"Un attimo, controllo."',
        "say_back": '"Pizza al taglio da guida, non la più vicina… cerco."',
        "holding": '"Un attimo."',
        "announcing": '"Posso…", "Sto aprendo…", "Mi scuso…"',
        "own_cause": '"ho avuto un problema…", "si è interrotto…"',
        "own_promise": '"cercherò di risolvere…", "ti farò sapere…"',
        "correction": '"Anzi, è chiuso oggi."',
        "emotion_words": "stressato, ansioso, triste…",
        "named_feeling": '"Ti sento di corsa"',
        "wave_away": '"giornata pesante? O lascio stare."',
        "aside": '"no, prendi pure tu…"',
        "fillers": '"Dimmi pure, sono qui", "Sono in ascolto" or "Non ho capito"',
        "talker_filler": 'no "Certamente!", no "C\'è altro?"',
        "misheard_language": "Italian as French",
        "servant": '"Certamente!", "Come posso aiutarti?"',
        "narrator": '"Pisa ha spostato…", "Pisa ci sta lavorando"',
        "first_person": '"ho spostato", "ci sto lavorando", "I moved it"',
        "bible_register": 'Italian with "tu" or plain English',
        "borrowed_words": 'an English word in Italian where they would use one ("la call", "il meeting")',
        "done": '"Fatto."',
        "got_it_wrong": '"Ho sbagliato: …", "I got that wrong: …"',
    },
}


def examples(lang: str) -> dict[str, str]:
    """The example phrases for `lang` (a tag or a short code)."""
    return EXAMPLES[locale.short(lang)]


def fill(template: str, lang: str) -> str:
    """A prompt template with its `{name}` examples in `lang`'s words."""
    return template.format_map(examples(lang))


LANGUAGE = "{language}"  # a prompt's "Write in {language}.": named per call by `in_language`


def in_language(instructions: str, lang: str) -> str:
    """The instructions with `{language}` named: "English", "Italian" (Pisa's language when None). Other
    braces are left alone (the night's prompts are filled with `.format` first)."""
    return instructions.replace(LANGUAGE, locale.name(lang))
