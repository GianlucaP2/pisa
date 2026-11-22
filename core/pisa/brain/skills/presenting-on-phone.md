---
name: presenting-on-phone
title: Showing things on the phone
when: a look beats a sentence: the day, several items, places, a draft, a list; or the user asks to see something
triggers: fammi vedere | mostrami | fammi la lista | com'è la giornata | cosa ho domani | show me | let me see | what's my day | on my phone | tabella | come tabella | in tabella | as a table | in a table
tools: show_plan, get_agenda, show_sheet, save_view
status: reviewed
source: Pisa's starter set
---
**What matters**: what's on the screen matches what Pisa says; Pisa says "te l'ho messo sul telefono" only after the
result says shown.

**How Pisa does it**
- Read what to show (e.g. `get_agenda` for the day), `show_plan` it, and say the gist in one sentence:
  "Te l'ho messa sul telefono: tre cose, la prima alle 9."
- Phone in a pocket / glasses only (Pisa's body line) → say the gist, offer the card.
- More than three items → speak at most three, the rest on the screen.
- **A table** ("come tabella", "in tabella", "as a table"): `show_sheet` with a `table` block (columns, rows:
  the meteo's days, prices, times), never `show_note` lines; "salvala" then keeps that table (`save_view`).

**Pitfalls**: reading a whole list aloud; claiming it's shown when the phone isn't connected.
