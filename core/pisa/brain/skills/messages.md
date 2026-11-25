---
name: messages
title: Messages to people (WhatsApp, iMessage)
when: the user asks to write, tell, text or answer someone by message
triggers: scrivi a | scrivile che | scrivigli che | arrivo in ritardo | su whatsapp | manda un messaggio | dille che | digli che | dì a | avvisa | rispondi su whatsapp | text | message | tell her | let him know | send a whatsapp
tools: prepare_message, compose_message
status: reviewed
source: Pisa's starter set
---
**What matters**: the message says what the user meant, in their voice, to the right person; the user
sends it (Pisa never sends messages to people by itself).

**How Pisa does it**
- Write the whole text as the user would (first person, their tone, short): "Arrivo con dieci minuti di
  ritardo, scusa!".
- `prepare_message` (default WhatsApp): the button on the phone opens the app with the text; he taps Send.
  `compose_message` only when prepare_message isn't offered.
- Say: "Pronto su WhatsApp per Marta: lo mandi tu." Never "mandato".
- Not on the phone (glasses only) → say the text and that it's waiting on the phone.
- No contact matches → ask who ("Marta Rossi o Marta del lavoro?").

**Pitfalls**: claiming it was sent; adding facts the user didn't say (an excuse, a time).
