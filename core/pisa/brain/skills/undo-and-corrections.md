---
name: undo-and-corrections
title: Undo and corrections
when: the user says to undo, put it back, or corrects what Pisa just did ("no, alle 9", "anzi")
triggers: annulla | rimettilo com'era | torna indietro | anzi no | no alle | ho sbagliato | undo | put it back | revert | no, at | actually
tools: undo_last_change, move_event, answer_proposal
status: reviewed
source: Pisa's starter set
---
**What matters**: the user's last word wins; the state ends where they want it, with no extra question.

**How Pisa does it**
- "Annulla" / "rimettilo com'era" right after a change → `undo_last_change`; say what is back from its `stored`
  ("Rimessa alle 6.").
- "No, alle 9" right after Pisa said a time → change that same item to 9 (move it, or re-create a proposal);
  don't ask.
- A correction to a still-pending proposal → withdraw it and do the corrected one.

**Pitfalls**: undoing the wrong change (undo is Pisa's last change only; for an older one, move it explicitly);
treating "annulla la riunione" (cancel an event) as undo.
