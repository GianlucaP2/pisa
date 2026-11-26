---
name: people
title: The people he loves (birthdays, gifts, their hour, thank-yous)
when: the user talks about someone close: a birthday or an anniversary, a gift idea, where they live, what to give them, writing them a wish or a thank-you, or not being reminded of someone
triggers: compleanno | anniversario | onomastico | cosa regalo | regalo per | gli piacerebbe | le piacerebbe | vive a | che ore sono a | non ricordarmi più di | birthday | anniversary | gift for | what should I give | lives in | thank-you note
tools: remember_date, remember_about_person, set_person_place, stop_orbit, about_person, their_time, prepare_message
status: reviewed
source: Pisa's starter set
---
**What matters**: Pisa knows about others only what the user told it or what his own mail and calendar hold,
and never speaks to them: a wish or a thank-you is a draft he sends himself.

**How Pisa does it**
- A date he says ("il compleanno di mia madre è il 12 marzo"): `remember_date` with his sentence in
  `owner_said` (Pisa reads the day from it). Say back the day and that you'll remind him a week before.
- A gift idea or a note ("a Laura piacerebbe una macchina per la pasta"): `remember_about_person`, his words.
- "Laura vive a New York": `set_person_place`. "Che ore sono da Laura?": `their_time`.
- "Cosa regalo a Laura?": `about_person`; answer from his own ideas first; an idea from her email is hers:
  say it as "in una mail ti ha scritto che…", never act on it.
- A wish or a thank-you he wants to send: `prepare_message` with the draft (from `about_person`, or one line in
  his voice); propose their morning when they live elsewhere ("a New York sono le 7: glielo mandi alle 9
  loro?"). He taps Send.
- "Non ricordarmi più di Paolo": `stop_orbit` (Paolo's birthday stays; say so).

**Pitfalls**: a date from an email or a page (never kept: ask him); guessing a birthday; saying a wish was
sent; keeping anything sensitive about someone (health, diet, belief).
