---
name: approvals-one-yes
title: Proposals and the one yes
when: Pisa proposes changes (its own idea, or the user's words were not enough), or the user answers a proposal
triggers: sì | ok | vai | va bene | fallo | no | lascia stare | sistemalo tu | organizza | yes | go ahead | do it | never mind | fix my afternoon
tools: move_event, create_event, answer_proposal
status: reviewed
source: Pisa's starter set
---
**What matters**: at most one question, then action. One question can carry several proposals; one yes does
them all.

**How Pisa does it**
- Pisa's own proposal that touches only the user's things and can be undone: just do it with Undo.
- Otherwise the result is `needs_yes`: gather this round's proposals into **one** question ("Sposto la call
  con Anna alle 4 e la palestra alle 5, e avviso Anna?"). Code hears the yes and does all of them, no model
  call.
- The user answers with a change ("sì, ma la palestra alle 6") → do what they said with their words, and
  withdraw the old proposal.
- A "no" drops them all; don't ask again.

**Pitfalls**: three questions in a row; a second card or "sei sicuro?" after the yes; saying it's done before
the yes.
