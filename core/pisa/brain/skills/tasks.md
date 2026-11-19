---
name: tasks
title: Tasks (to-dos without a fixed time)
when: the user names something to do, buy or remember without a fixed time, or asks what is on the list
triggers: devo | ricordati che devo | aggiungi alla lista | da fare | comprare | ritirare | la lista della spesa | to-do | add to my list | I need to | buy | pick up | trovami un momento | trovagli uno spazio | fammelo fare | spostalo alle | find me time | fit it in
tools: add_task, get_tasks, complete_task, plan_task, set_task_start
status: reviewed
source: Pisa's starter set
---
**What matters**: each thing named becomes one task in the user's words; nothing with a time becomes a task
(that's an event or a reminder).

**How Pisa does it**
- One `add_task` per item, in one round ("comprare il latte e ritirare le camicie" = 2 tasks), each citing
  its words.
- A due day without a time ("entro venerdì") → `due`. A time ("alle 18") → a reminder or an event: pick the
  one the words mean ("ricordami alle 18" = reminder; "alle 18 ho" = event).
- Say it short, from each result's "Added: …" (the task as stored): "Messi in lista: latte e camicie."

**A task at a time**: Pisa never times a task by itself; his words do:
- "Trova(mi) un momento / uno spazio per X", "mettilo oggi quando posso", "fammelo fare oggi", "find me time
  for X": Pisa picks the time → `plan_task` (found with `search_tools` ["plan_task"]), then say the time it got.
- "Spostalo alle 15", "lo chiamo domani alle 10" on a task that exists: his time to do it → `set_task_start` (a
  block in the plan), never `reschedule_task` (a deadline: "entro le 19").

**Pitfalls**: answering "va bene" without the tool when he asked for a time; merging two items into one; adding the same task twice (check `get_tasks` when the user
says "ce l'ho già?").
