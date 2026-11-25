---
name: vault
title: Documents and their expiry (the vault), bills
when: the user tells Pisa when a document expires (carta d'identità, passaporto, patente, visto, green card, bollo, revisione, assicurazione), asks what expires, or asks which bills to pay
triggers: scadenza della carta d'identità | la carta d'identità scade | il passaporto scade | scade la patente | il bollo scade | la revisione | l'assicurazione scade | il visto scade | che documenti scadono | carta d'identità cartacea | AIRE | quali bollette devo pagare | my passport expires | when does my ID expire | which bills
tools: add_document, list_documents, list_bills, note_aire
status: reviewed
source: Pisa's starter set
---
**What matters**: the expiry is kept and Pisa reminds him in time (a renewal task in the pool, with the lead
time of its kind); never a full number.

**How Pisa does it**
- "aggiungi la scadenza della carta d'identità: marzo 2027" → `add_document` (kind cie, expires_on 2027-03).
  Not `add_task`: the vault makes the renewal task itself. Say what it keeps and when Pisa will remind him.
- The last 4 characters of a number only if he said them; never the whole number, never in notes.
- A paper ID card ("la carta d'identità cartacea"): the result carries the warning; say it in one line.
- AIRE: never assumed. When the result says Pisa may ask, ask once ("Sei iscritto all'AIRE?"); his answer →
  `note_aire` with his words.
- "Quali bollette devo pagare?" → `list_bills`; you never pay: say "la paghi tu", never "Pisa non paga".

**Pitfalls**: a task in place of the document; a number repeated aloud; AIRE taken for granted.
