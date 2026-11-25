---
name: calendar-create
title: Create events (one, several at once, a range)
when: the user asks to put one or more things with a day and a time on the calendar, or lists their plans and says to put them in
triggers: metti in calendario | mettilo in calendario | metti tutto sul calendario | segna in agenda | aggiungi un evento | appuntamento | ho il dentista alle | add to my calendar | put it in my calendar | schedule | book the slot | segnami | fissa
tools: create_event, get_agenda
status: reviewed
source: Pisa's starter set
---
**What matters**: every item the user named exists, on the day and at the time they meant, once. Nothing is
said as done unless a result says `done`.

**How Pisa does it**
- Split the words into items: one event per title + time ("palestra alle 10, pranzo con Nino alle 13 e
  dentista alle 17" = 3 events). Call `create_event` **for all of them in the same round** (parallel calls).
- Each call cites its own piece of the user's words in `owner_said` (["palestra alle 10"], ["pranzo con
  Nino alle 13"]), plus the piece with the day if it is elsewhere (["domani"]). That is what makes it act at
  once; without it the result is a proposal.
- The day: resolve it yourself from the clock line. At night read the skill night-domani.
- No end said: leave `end` empty (60 minutes). "Dalle 9 alle 18" → start and end. A range of days ("da
  martedì a giovedì, dalle 9 alle 18") → one event per day, same hours.
- After the writes, read back with `get_agenda` for that day when you created more than one; then say it in
  one sentence with the weekday and the times as people say them, and that it can be undone.
- Say each event's day and time from its result's `stored` (what the calendar holds), never from the request.
- A result that says `needs_yes`: ask its question once, for all the proposals together ("Li aggiungo tutti
  e tre, venerdì?"); never say they exist.

**Pitfalls**
- Three events asked means three created: never say "li sposto" without doing it. A proposal is not an
  event; `move_event` on a proposal fails.
- Don't create a duplicate: an error "already exists" means it is there; say so.
- A time with no day after midnight, a time before 07:00, or "alle 5": the result may ask for one yes.

**Example (it)**
Owner (Fri 10:00): "Domani palestra alle 10 e cena con Sara alle 20:30, segnale."
→ create_event(Palestra, Sat 10:00, owner_said ["palestra alle 10", "domani"]) ‖ create_event(Cena con Sara,
Sat 20:30, owner_said ["cena con Sara alle 20:30", "domani"]) → both `done` → "Fatto: sabato palestra alle 10
e cena con Sara alle 8 e mezza."

**Example (en)**
Owner (Fri 10:00): "Gym at 10 tomorrow and dinner with Sara at 8:30, note it."
→ create_event(Gym, Sat 10:00, owner_said ["gym at 10", "tomorrow"]) ‖ create_event(Dinner with Sara,
Sat 20:30, owner_said ["dinner with Sara at 8:30", "tomorrow"]) → both `done` → "Done: Saturday, gym at 10
and dinner with Sara at half past 8."
