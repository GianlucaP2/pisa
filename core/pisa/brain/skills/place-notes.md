---
name: place-notes
title: A note for when he is at a place (Esselunga, the pharmacy, the office)
when: the user asks to be reminded of something when he is at or passes by a place, not at a time
triggers: ricordami quando sono da | quando passo da | quando arrivo a | quando sono al supermercato | quando vado da | appena arrivo in ufficio | remind me when I'm at | when I get to | when I pass by | note dei posti | togli la nota
tools: remember_at_place, list_place_notes, forget_place_note
status: reviewed
source: Pisa's starter set
---
**What matters**: Pisa says it when he gets there, once a visit. It is not a task and not a timed reminder.

**How Pisa does it**
- "ricordami quando sono da Esselunga di comprare il bagnoschiuma" → one `remember_at_place` (text: his
  words for what to do, "comprare il bagnoschiuma"; place: as he named it, "Esselunga"). Nothing else: no
  `add_task`, no `remember_to`.
- Say it back short: "Quando sei da Esselunga te lo ricordo."
- "Togli la nota dell'Esselunga" → `forget_place_note`; "che note ho nei posti?" → `list_place_notes`.

**Pitfalls**: a task with the place in its title; a reminder at a guessed time; asking when he'll go there.
