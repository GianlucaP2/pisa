---
name: places-of-my-life
title: His fog of war, the Map of the Life, and tasks only one country allows
when: the user asks where he has never been nearby, for the map of his season or life, or says a task can only be done in Italy or the US (or anywhere)
triggers: nebbia | nebbia di guerra | dove non sono mai stato | zone che non conosco | mappa della mia vita | mappa della vita | la mia stagione | fog of war | where I've never been | map of my life | solo in Italia | solo negli USA | quando torno in Italia | quando torno negli Stati Uniti | when I'm back in the US | si può fare ovunque
tools: show_fog_of_war, show_map_of_life, set_task_country
status: reviewed
source: Pisa's starter set
---
**Fog of war**: "dove non sono mai stato qui intorno?" → `show_fog_of_war`: the sheet is on the phone; say one
line from its result (how much he knows, the side he knows least). Never a list read aloud.

**The Map of the Life**: "mostrami la mappa della mia vita" → `show_map_of_life` (`current`; "la stagione
scorsa" → `last`). Numbers are code's: say only what the result says.

**Country-bound tasks**: "questo lo posso fare solo in Italia" about an open task → `set_task_country` (IT);
"si può fare ovunque" → null. A new task said with it ("rinnovare la patente solo in Italia"): `add_task`, and once
it is made `set_task_country`. Only his own words name the country, never an email.
