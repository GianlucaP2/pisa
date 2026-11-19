---
name: tasks-bulk
title: Bulk task changes (done, delete, move several)
when: the user closes, deletes or moves several tasks at once ("ho fatto tutto", "togli quelle della spesa")
triggers: ho fatto tutto | fatti tutti | segna fatto | ho comprato tutto | togli tutte | cancella quelle | done with all | mark them done | clear the list
tools: get_tasks, tasks_bulk
status: reviewed
source: Pisa's starter set
---
**What matters**: exactly the tasks the user meant change; the rest stay open.

**How Pisa does it**
- Items he names one by one ("latte, pane e uova presi"): every one in ONE round, `mark_done` each, or one
  `tasks_bulk` (action done, filter named, titles as he said them): done at once, no question, undoable.
- A group ("tutti", "quelle di Amazon", "le scadute"): `tasks_bulk` with the filter; it gives the count: say it
  and ask once; his yes does them all.
- Say the count and the leftover if any: "Fatte tre; resta ritirare le camicie." A result's "NOT changed" list
  is said too ("la bolletta era già chiusa").
- Unsure about one item → do the sure ones and ask about that one in the same reply.

**Pitfalls**: "ho fatto tutto" right after Pisa listed tasks means those listed, not every task Pisa knows.
