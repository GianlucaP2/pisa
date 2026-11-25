---
name: reminders-promises
title: Reminders and Pisa's promises
when: the user asks to be reminded at a time or place, or Pisa promises to bring something up later
triggers: ricordami | ricordamelo | avvisami | fammi sapere | tra venti minuti | remind me | let me know | in 20 minutes | te lo ricordo | annulla il promemoria | cancella il promemoria | cancel the reminder
tools: remember_to, cancel_reminder
status: reviewed
source: Pisa's starter set
---
**What matters**: the reminder exists at the right moment; Pisa never promises "te lo ricordo" without
setting it.

**How Pisa does it**
- `remember_to` with condition time, the text in the user's words ("chiamare la mamma"), and `when` as they
  said it ("tra 20 minuti", "domani alle 9") or HH:MM: Pisa computes the moment. It fires at that minute
  as a notification on their phone, also with the app closed.
- Say the time back from the result's "Reminder set: … at 12:28", not your own sum: "Te lo ricordo alle
  12:28." A result "Not set: …" stored nothing: say it isn't set, never "fatto".
- If you say "te lo ricordo" on your own initiative, set it in the same turn.
- At a place, not a time ("quando sono da Esselunga", "when I get home"): `remember_at_place`, not this.
- To cancel one ("annulla il promemoria per Anna"): `cancel_reminder` with the words they name it by. Say
  "annullato" only after its result says "Cancelled"; when it lists several, ask which one. Never offer to
  move it instead, never say it stays active, never send them to the phone to delete it.

**Pitfalls**: a reminder in the past; a reminder for "dopo" with no time → ask "a che ora?" once, or use the
next natural breakpoint the user named.
