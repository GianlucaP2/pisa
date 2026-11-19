---
name: night-domani
title: "Domani", "oggi" and "stanotte" after midnight
when: the user talks between midnight and 05:00 about today, tomorrow, tonight, or a weekday
triggers: domani notte | dopo mezzanotte | after midnight
pin: night  # always in the turn between 00:00 and 05:00, whatever the words
tools: create_event, move_event
status: reviewed
source: Pisa's starter set
---
**What matters**: the day the user means, not the calendar date.

**How Pisa does it**
- Before 05:00 someone who hasn't slept means by "domani" the day that is starting (at 02:38 on Friday,
  "domani" = Friday), and by "oggi" the day that just ended (Thursday) — unless they name the date or weekday.
  The clock line says it each night.
- Act on that reading and **say the weekday** in the reply ("venerdì alle 10"), so a wrong guess is
  corrected in one word and undone.
- "Stanotte" = the coming hours. "Domattina" = the coming morning.
- A weekday name is never ambiguous: use it.

**Pitfalls**: asking "domani venerdì o sabato?" every night (one yes too many: act and name the day); writing
the day as a date the user doesn't say ("il 31").

**Example (it)**
02:38 Friday. Owner: "Domani alle 9 chiamo il commercialista, segnalo." → create_event(Chiamare il
commercialista, Fri 09:00, ["domani alle 9 chiamo il commercialista"]) → "Segnato, venerdì alle 9."

**Example (en)**
02:38 Friday. Owner: "Tomorrow at 9 I call the accountant, note it." → create_event(Call the
accountant, Fri 09:00, ["tomorrow at 9 I call the accountant"]) → "Noted, Friday at 9."
