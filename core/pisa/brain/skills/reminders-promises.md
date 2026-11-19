---
name: reminders-promises
title: Reminders and Pisa's promises
when: the user asks to be reminded at a time or place, or Pisa promises to bring something up later
triggers: ricordami | ricordamelo | avvisami | fammi sapere | tra venti minuti | remind me | let me know | in 20 minutes | te lo ricordo
tools: remember_to
status: reviewed
source: Pisa's starter set
---
**What matters**: the reminder exists at the right moment; Pisa never promises "te lo ricordo" without
setting it.

**How Pisa does it**
- Compute the moment from the clock line: "tra 20 minuti" = now + 20; "alle 18" = today if still ahead,
  else ask (or tomorrow if they said "domani").
- `remember_to` with the text in the user's words ("chiamare la mamma"), citing them.
- Say the time back from the result, not your own sum: "Te lo ricordo alle 6."
- If you say "te lo ricordo" on your own initiative, set it in the same turn.
- At a place, not a time ("quando sono da Esselunga", "when I get home"): `remember_at_place`, not this.

**Pitfalls**: a reminder in the past; a reminder for "dopo" with no time → ask "a che ora?" once, or use the
next natural breakpoint the user named.
