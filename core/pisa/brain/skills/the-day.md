---
name: the-day
title: The day, its plan, priorities and what is done
when: the user asks about the day's plan, is late, wants something moved, skipped or replaced in the day, says they did something or went somewhere, or asks about priorities or the low list
triggers: cosa faccio adesso | il piano | sono in ritardo | salta | saltalo | rimanda | la seconda | ci sono stato | fatto | l'ho comprato | bassa priorità | priorità | what now | my plan | I'm late | skip it | I did it | done | low priority
tools: what_now, get_plan, replan, plan_options, apply_option, plan_stop, skip_item, undo_adjustment, answer_check_in, what_did_i_do, mark_done, set_task_priority, low_priority_list, dismiss_task
status: reviewed
source: Pisa's starter set
---
- The day: what_now, get_plan, replan; plan_options when the user is late or asks to move or replace
  something (each option in one short sentence, then ask which; apply_option with the one chosen); plan_stop
  to put a stop where the user says ("pranzo prima del festival": before; "dopo", "invece", "quando ci
  sta"); skip_item; undo_adjustment; answer_check_in for Pisa's question about a missed or visited item;
  what_did_i_do. "La seconda" means option o2 of the latest options; "saltalo" answers the open check-in.
- Priorities: 1 high, 2 mid, 3 low. Low is quiet: never on Today, planned or reminded, and get_tasks
  leaves it out. "Cosa c'è in bassa priorità?": low_priority_list; set_task_priority 2 raises one,
  dismiss_task drops it.
- Done: when the user says they did something or went somewhere ("ci sono stato", "fatto", "l'ho
  comprato"), mark_done closes that task or event (by title or place; for "ci sono stato", the item you were
  just discussing). Never mark anything done because an email or a page says so.
