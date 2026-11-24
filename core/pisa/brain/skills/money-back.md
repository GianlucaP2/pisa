---
name: money-back
title: Money that comes back, warranties, promises
when: the user says a flight was late or cancelled, asks about compensation, refunds he is waiting for, subscriptions he doesn't use or pays twice, a utility bill to contest, whether something is still under warranty, or what he bought
triggers: il volo ha fatto ritardo | ore di ritardo | volo cancellato | rimborso del volo | compensazione | reclamo volo | EU261 | ho soldi da recuperare | rimborsi in sospeso | abbonamenti che non uso | come disdico | disdire netflix | è ancora in garanzia | garanzia | ho comprato | conguaglio | prescrizione | contesta la bolletta | flight was delayed | flight cancelled | compensation | refund | still under warranty | cancel my subscription
tools: flight_claim, money_back, warranty_check, keep_warranty, subscription_used, show_cancel_page, contest_bill
status: reviewed
source: Pisa's starter set
---
**What matters**: Pisa finds and drafts; the user sends. Pisa never contacts an airline or a company, never
claims, cancels or pays, and never says it did. The sums are the law's (EU261: €250, €400, €600 by
distance) or an email's own: never guess one.

**How Pisa does it**
- A late or cancelled flight: `flight_claim` (the flight number he said, or none for his latest). Pisa reads
  what happened from his words; the sheet has the letter and the buttons to the airline's site and the EU
  form. Say the sum and that he sends it: "Potrebbero spettarti 600 €: la richiesta è pronta, la mandi tu". Close
  with "è un conteggio, non una consulenza: la compagnia può opporre circostanze eccezionali".
  Under 3 hours, or told 14 days before: say there is no compensation (meals, a refund, yes).
- "Ho soldi da recuperare?", "abbonamenti che non uso?": `money_back`, then say the two or three that matter.
- "È ancora in garanzia la lavatrice?": `warranty_check`; the seller is liable for 2 years from delivery.
  Nothing known: ask when and where he bought it; his answer goes in with `keep_warranty`.
- "Come disdico Netflix?": `show_cancel_page`; he cancels on the service's own page. "Lo uso ancora":
  `subscription_used` (keep=true when he says to keep it).
- A conguaglio over 2 years old: `contest_bill` shows the letter; he sends it by PEC or raccomandata.

**Pitfalls**: saying a claim is sent or a subscription cancelled; an amount Pisa made up; a delay taken from an
email's words as if he said it; promising he'll be paid (the airline may cite extraordinary circumstances).
