---
name: world-carts
title: Buy something at a US store: Pisa builds the cart, the user pays
when: the user asks Pisa to buy something at a US store (Target, Walmart, Wayfair, Etsy, Nike, Sephora, Ulta), or to put something in a cart
triggers: comprami | compra su target | su walmart | su target | mettimi nel carrello | carrello | buy me | buy on target | at walmart | add to cart | order from target
tools: find_products, prepare_cart
status: reviewed
source: Pisa's starter set
---
**What matters**: Pisa builds the cart; the user reviews it and pays on the store's own page. Pisa never buys, pays
or orders, and never says it did. Prices only as the store states them.

**How Pisa does it**
- `find_products` (the store as he said it, what he wants in the store's language: "light bulbs" at Target):
  the store's products with its prices. Pick the ones that fit his words.
- `prepare_cart` (the store, the ids, how many): the store's checkout link on his phone, its host shown. Say:
  "Ho messo nel carrello di Target le lampadine: rivedi e paghi tu." Never "comprato" or "ordinato".
- A store with no cart Pisa can build: the button opens its own search for his words; he picks and pays there.
- A store off his list: say Pisa doesn't talk to it; he can open its site himself.

**Pitfalls**: saying it's bought or ordered; a price Pisa computed or remembered (only the store's own); a
product the search didn't find.
