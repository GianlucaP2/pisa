---
name: places-plan-it
title: Places, and "Plan it" (a place into the day)
when: the user looks for a place (to eat, a shop, a service) or wants to go somewhere and fit it into the day
triggers: trovami un posto | dove posso | un ristorante | un bar vicino | farmacia aperta | mettilo in calendario | organizzalo | plan it | find me a place | somewhere to eat | near here | open now
tools: map_search, show_plan, create_event
status: reviewed
source: Pisa's starter set
---
**What matters**: the best fit for what they asked (open then, close enough, their tastes), checked, and —
when asked — in the day with the travel time.

**How Pisa does it**
- `map_search` near where they are or will be; pick the best one or two (open at that time, minutes away).
- Show the card on the phone (`show_plan` place) and say the best one in a sentence.
- "Mettilo in calendario all'una" / "Plan it" → `create_event` titled with what and where ("Pranzo da
  Trattoria Rossi"), the place, at the time said (or the next free slot that fits travel, as a proposal).
- Several good options → at most three spoken, the rest on the phone.

**Pitfalls**: inventing hours or distances; claiming it is on the phone when the result didn't say shown.
