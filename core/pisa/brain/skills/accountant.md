---
name: accountant
title: The accountant package (the year's documents to the commercialista)
when: the user asks Pisa to prepare or send the year's documents to his accountant
triggers: prepara i documenti per il commercialista | manda le carte al commercialista | documenti per la dichiarazione | send my documents to my accountant | paperwork for my CPA
tools: accountant_package
status: reviewed
source: Pisa's starter set
---
**What matters**: one draft to the accountant he names, with what Pisa holds for the year (bills, receipts, the
residency count, the vault's documents); nothing is sent.

**How Pisa does it**
- "prepara i documenti per il commercialista" → `accountant_package` with the name or address he said; the
  year only if he said one.
- Read the draft back as the result says, then say it goes from the card on the phone, with Face ID.
- The residency count is a count, never advice ("È un conteggio, non una consulenza").

**Pitfalls**: offering to send it by voice; a tax opinion; inventing an amount or a document Pisa doesn't hold.
