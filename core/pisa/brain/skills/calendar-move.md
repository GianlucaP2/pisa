---
name: calendar-move
title: Move events (one, several, to another day)
when: the user asks to move, postpone, bring forward or change the day or time of events, including the ones Pisa just created
triggers: sposta | sono in ritardo | sistemami il pomeriggio | riorganizza | running late | spostalo | spostali | rimanda | anticipa | posticipa | cambia l'orario | mettilo alle | fallo diventare | non è oggi | intendevo sabato | move it | push it | reschedule | change the time | make it
tools: get_agenda, move_event, answer_proposal
status: reviewed
source: Pisa's starter set
---
**What matters**: the right event ends at the new start, keeping its length; others are untouched.

**How Pisa does it**
- Which event: if you created or found it in this conversation, use its id from your earlier results (your
  history keeps them). Otherwise `get_agenda` with the user's word for it ("la cena") and the day if said.
- Several matches → the most specific question ("Quella di stasera da Nino o quella di domani coi
  colleghi?"), once. One clear match → move it.
- "Spostali a sabato" after Pisa made three events → three `move_event` calls in one round, same times, new
  day; `owner_said` cites the words ("spostali a sabato").
- A time "alle 21" without a day keeps the event's own day.
- If the user moves things that are still **proposals** (the ledger lists them as pending), there is no
  event yet: create them at the corrected day/time with the user's words, and `answer_proposal` the old
  ones. Say what now exists.
- Invited people: the result asks one yes that names them; ask it once.
- Say the time from the result's `stored` (the calendar's own read-back), not his words: he said "dopo le
  sei", stored says 18:30 → "Spostato a giovedì alle 18:30". No `stored` → say only the receipt.

**Pitfalls**: moving by title alone when two events share it; saying "li sposto" (a promise) instead of
moving; changing the time when the user changed only the day.

**Example (it)**
Pisa just created e1–e3 for Friday. Owner: "no aspetta, intendevo sabato" → move_event(e1, Sat 10:00) ‖
move_event(e2, Sat 13:00) ‖ move_event(e3, Sat 17:00), owner_said ["intendevo sabato"] → "Spostati a sabato,
stessi orari."

**Example (en)**
Pisa just created e1–e3 for Friday. Owner: "no wait, I meant Saturday" → move_event(e1, Sat 10:00) ‖
move_event(e2, Sat 13:00) ‖ move_event(e3, Sat 17:00), owner_said ["I meant Saturday"] → "Moved to Saturday,
same times."
