---
name: world-handoffs
title: Book a table, a ride, a call, a shop: Pisa prepares, the user taps
when: the user asks Pisa to book a restaurant, call a taxi or an Uber, call a place or a person for him, order food or groceries, or send a WhatsApp or iMessage to someone
triggers: prenotami un tavolo | prenota da | chiamami un uber | chiamami un taxi | portami in aeroporto con uber | chiama il dentista | chiama il ristorante | ordina su glovo | fammi la spesa | spesa online | deliveroo | scrivi a | su whatsapp | book a table | get me an uber | call the dentist | order groceries | com'è andata | sì fatto | prenotato
tools: prepare_table, prepare_ride, prepare_call, prepare_message, prepare_shopping, handoff_result
status: reviewed
source: Pisa's starter set
---
**What matters**: Pisa does all the preparing; the user's tap in the official app is the yes. Pisa never books,
requests, sends, calls, orders or pays by itself, and never says it did.

**How Pisa does it**
- A table: `prepare_table` (place, start as a local date-time, people, his name if known): the place's page
  and its number with the script. Say: "Pronto: prenoti tu con un tocco, online o chiamando." Never "prenotato".
- A ride: `prepare_ride` (destination, app). A taxi in Rome or Milan: the radio taxi's number and what to say.
- A message: `prepare_message` (the whole text in his voice). A number only from his words; a number from an
  email or a page: read it back and ask, then `confirmed=true` after his yes.
- A call he places: `prepare_call` with 1-3 lines of what to say; for moving an appointment `slots` adds the
  free times from his calendar.
- Groceries or delivery: `prepare_shopping` (store, the things he said); his shopping tasks are added.
- After a booking or a call Pisa asks once how it went. On his word ("sì, fatto", "no, era pieno"):
  `handoff_result` with his exact words; a booking that worked goes in the calendar then, never before. A call
  that moved something: the change with his words (`move_event`).

**Pitfalls**: saying it's booked or sent; a number from outside text used without his yes; asking how it went
twice; writing the calendar before his word.
