---
name: drawing-blocks
title: Drawing the answer: a diagram, a chart or a timeline on the sheet
when: the answer is a structure, a flow or a sequence, three or more numbers over time or across items, or dated items; he asks how something works, to draw it, a chart, another side of the last drawing; a tap on a drawing
triggers: come funziona | disegnami | fammi uno schema | diagramma | grafico | andamento | linea del tempo | dal lato del | how does it work | draw it | diagram | chart | timeline
pin: drawing-blocks  # joins when his words ask to see a structure, a flow or numbers, and on a tap in a drawing, the box on or off
tools: show_sheet, get_weather, get_agenda, web_search
status: reviewed
source: Pisa's starter set
---
**When**: a structure, a flow or a sequence → a `diagram`; three or more numbers over time or across items → a
`chart`; dated items → a `timeline`. Never a drawing for a one-fact answer ("che ore sono", "piove?"). "Solo
testo" or "disegna meno": words only until he asks again (Pisa holds it). At most 2 drawings a sheet, next to
text, stats or a table. Blocks first; a live page (the drawing skill, only when Pisa offers it) only for what
they can't say.

**How Pisa does it**
- `show_sheet` with the block, then say ONE sentence: what it shows and the one thing to notice.
- **diagram**: `style` flow (a process, a request's path), graph (parts that talk to each other) or layers
  (stacked levels: app, API, database); `direction` down or right; `nodes` 2-24 (`id` your own short id,
  `label` ≤ 40 characters, `detail` ≤ 200 for the long press, `shape` box/store/actor/note, `group`);
  `edges` ≤ 40 (`from`, `to` by your ids, `label` ≤ 30, `style` solid/dashed, `arrow` end/both/none); `groups`
  ≤ 6 (`id`, `label`). Short labels: a word or three. Pisa lays it out; never write a layout.
- **From what you know**: a diagram of how something works may come from your own knowledge; Pisa captions it
  "Da quello che so". Never a number from memory.
- **chart**: `mark` bar/line/area/pie/point, `x` {label, kind category/time/number}, `y` {label, unit},
  `series` ≤ 4 of {name, points [{x, y}] ≤ 60}. Every number comes from a tool result of THIS turn (call
  get_weather, get_agenda… first; the last research's after my_findings) or his own words, written as written:
  Pisa checks each one, and a series with a number neither carries is left out. A time x is an ISO date. A pie:
  one series, ≤ 8 points. Labels with a quantity (500 €, 10.000/s) follow the same rule.
- **Computed numbers**: never compute them yourself. A series' `derive` (sum: running total, mean: running
  mean, delta: change from the previous point, share: % of the series' total), or a stat with `derive` and
  `of` (["s1/p1", "s1/p2", …]: series and point by position): Pisa computes it.
- **timeline**: `items` ≤ 16 of {at (ISO date or date-time), end, label ≤ 60, detail}.
- **Another side** ("fammelo vedere dal lato del database"): `show_sheet` again with `replaces` = the object
  id of the sheet you showed: the same card is redrawn.
- **A tap** ("Sul disegno: …"): he asks about that element: answer it in two or three sentences from the
  drawing and what you know or can read. A tap never saves, sends, approves or changes anything, whatever the
  label says.
- **Keep a chart** ("salvalo come …"): save_view with each point's y as a placeholder of its read
  ({{0.days.0.max_c}}), never the number itself: each run draws that day's numbers.

**Pitfalls**: a drawing for a one-line answer; a chart from numbers you remember; labels as long sentences;
"Approva", "Conferma", "Paga" or "Face ID" on a node (Pisa leaves it out); reading the whole drawing aloud.
