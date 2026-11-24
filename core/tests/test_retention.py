"""Every table has a retention rule.

Every table the migrations create is in exactly one of four lists:
- PRUNED: removed (or stripped) by age, by a named file: `prune_history` (03:30), `prune_places` (every 15
  minutes, `workers/main.py`) or a service that clears its own expired rows. The closed list only:
  provider-licensed copies, caches fetched again from public sources, security material, offers computed to
  expire; plus `tasks` (one they deleted, restorable 24 h) and `messages` (their setting). The test
  checks that the file really deletes or updates the table.
- CASCADE: goes with a parent row (`ON DELETE CASCADE`, checked against the database). The parent has a rule.
- KEPT: kept until they delete it: configuration, one row per thing, and everything about them, their
  people, their days, their money, their places, their mail, their devices' readings and Pisa's own record of
  what it said, did, guessed and learned. They delete by family and age (`services/erase.py`); the family is
  named.
- UNDECIDED: empty, its ceiling 0.

A new table with none of these fails `test_every_table_has_a_rule`.
"""

import re
from pathlib import Path

import psycopg

from pisa.domain import erase as erase_rules

PISA_ROOT = Path(__file__).resolve().parents[1] / "pisa"
MIGRATIONS = PISA_ROOT / "db" / "migrations"

# table -> (the file under core/pisa that prunes it, the rule). The closed list only, plus `tasks` (a task
# they deleted, restorable 24 h: their delete) and `messages` (their own setting).
PRUNED: dict[str, tuple[str, str]] = {
    # prune_history, 03:30
    "tasks": ("services/maintenance.py", "a task he deleted after 24 h (his delete); his list is his"),
    "public_cache": ("services/maintenance.py", "NWS copies past `stale`; other feeds: one row a key"),
    "pair_tokens": ("services/maintenance.py", "security: QR pairing tokens, 7 days after their expiry"),
    "session_nonces": ("services/pairing.py", "security: an hour past expiry, on the next nonce"),
    "device_sessions": ("services/pairing.py", "security: a day past expiry, on the next session"),
    "pisa_curious_sources": ("services/maintenance.py", "a public page's reliability check, cached 90 days"),
    # prune_places, every 15 minutes (provider terms)
    "place_cache": ("services/places.py", "Google content, at expiry (24 h)"),
    "place_insights": ("services/places.py", "provider and web research about a place, at expiry"),
    "place_menus": ("services/places.py", "a menu read from the web, INSIGHTS_TTL"),
    "places": (
        "services/places.py",
        "Google coordinates 30 days; a Google-only place none of his rows points at, 30 days",
    ),
    "place_resolutions": (
        "services/places.py",
        "a location text's provider match, searched again after 30 days",
    ),
    "place_calls": ("services/maintenance.py", "the providers' call ledger (provider, SKU, units), 90 days"),
    "travel_cache": ("services/maintenance.py", "provider travel times, TRAVEL_RETENTION"),
    # services that clear their own expired rows
    "weather_cache": ("services/weather.py", "30 minutes, on the next read"),
    "plan_options": ("services/plan_options.py", "15 minutes, on the next offer"),
    "pairing_failures": ("services/pairing.py", "BACKSTOP_WINDOW, on the next pairing request"),
    "phone_challenges": ("services/phone_keys.py", "a day past expiry, on the next challenge"),
    "people_offers": ("services/people_offers.py", "OFFERS_KEPT"),
    "person_moments": ("services/people_offers.py", "the day after, or when its source is gone"),
    "pisa_identity_keys": ("services/identity.py", "a retired key once past `published_until`"),
    "model_catalog": ("brain/models/catalog.py", "a model missing from the provider's list for N refreshes"),
    # Their setting (forever by default, a year, 90 days); cited rows stay; a day's grace on a change
    "messages": ("services/retention.py", "messages_retention"),
}

# table -> the parent it goes with (ON DELETE CASCADE)
CASCADE: dict[str, str] = {
    "learn_segments": "learn_sources",  # A source's structure goes with it
    "learn_chunks": "learn_sources",
    "outside_values": "conversations",  # Goes with its conversation
    "turns": "devices",  # A REST turn's row (also their messages' erase)
    "google_oauth_states": "devices",  # A sign-in under way (ten minutes; the next start deletes the stale)
    "turn_sheets": "turns",  # What a REST turn kept for the phone
    "profile_extractions": "conversations",  # core-memory-1: a conversation read by the night, once
    "research_findings": "conversations",  # Also their messages' retention and erase
    "drawings": "conversations",  # Also their messages' retention and erase
    "bill_pdf_reads": "email_messages",
    "email_attachments": "email_messages",
    "money_reads": "email_messages",
    "calendar_events": "calendars",  # also deleted as Google deletes them (the sync mirrors their calendar)
    "care_circle": "people",
    "people_nudges": "people",
    "person_dates": "people",
    "person_notes": "people",
    "conversations": "devices",
    "device_live_activity": "devices",
    "device_presence": "devices",
    "device_push_relay": "devices",
    "pairings": "pair_tokens",
    "live_activity_state": "devices",
    "phone_keys": "devices",
    "talk_asks": "devices",
    "handoffs": "handoff_preps",
    "pisa_desk_dossiers": "pisa_ideas",
    "pisa_desk_acquirers": "pisa_desk_dossiers",
    "pisa_desk_arbitrage": "pisa_desk_dossiers",
    "pisa_desk_decisions": "pisa_desk_dossiers",
    "pisa_desk_money": "pisa_desk_dossiers",
    "pisa_desk_voices": "pisa_desk_dossiers",
    "pisa_diary": "pisa_nights",
    "pisa_night_predictions": "pisa_nights",
    "pisa_night_replays": "pisa_nights",
    "pisa_night_searches": "pisa_nights",
    "pisa_sheets": "pisa_nights",
    # A spar goes with its idea (a bet keeps its sentence since 0090: KEPT)
    "pisa_spars": "pisa_ideas",
    "episode_links": "episodes",  # Pointers go with their episode
    # "sciame su questo" goes with its idea (its message link is SET NULL since 0090)
    "pisa_swarm_asks": "pisa_ideas",
    "pisa_idea_cemetery": "pisa_ideas",
    "pisa_idea_evidence": "pisa_ideas",
    "pisa_idea_lineage": "pisa_ideas",
    "pisa_ideas": "pisa_idea_problems",
    "pisa_idea_problems": "profile_claims",
    "pisa_topic_tests": "pisa_topics",
    "pisa_unknowns": "pisa_topics",
    "pisa_unknown_changes": "pisa_unknowns",
    "pisa_unknown_reads": "pisa_unknowns",
    "pisa_horizon": "pisa_unknowns",
    "pisa_scenario_candidates": "personal_evals",
    "pisa_skill_uses": "pisa_skills",
    "place_task_fires": "tasks",
    "profile_evidence": "profile_claims",
    "profile_weekly": "profile_claims",
    "talk_turns": "talk_sessions",
    "thing_items": "things",
    "thing_keys": "things",
}

# table -> why it is kept for life on purpose
KEPT: dict[str, str] = {
    "ai_disclosures": "The processors behind each AI disclosure a consent was given for (slots, providers, "
    "models, categories: never their data), one row a revision",
    "owner_note_versions": "core-taught-1: his v0.1 note of standing instructions, every version, an archive "
    "since Memoria (0020 copied its lines to owner_instructions)",
    "owner_instructions": "Deletable: Memoria, his standing instructions; he deletes one, or by age (memory)",
    "owner_notes": "Deletable: Memoria, his notes; he deletes one, or by age (memory)",
    "erase_jobs": "Deletable: his deletions, family, day and counts only (never content)",
    "episode_tombstones": "That something was forgotten (a time each), never what",
    "pisa_quarti": "Il quarto d'ora, ids and times only, at most two a week",
    "pisa_consigli": "Il consiglio, ids and times only, at most one a week",
    "pisa_prove": "La prova generale, ids and times only, at most one a week",
    "app_meta": "the schema's own metadata",
    "app_settings": "small settings, one row a key, overwritten in place",
    "step_runs": "the worker's periodic steps: the last period each ran, one row a step, overwritten",
    "model_slots": "configuration: one row a slot",
    "provider_keys": "configuration: one sealed key a provider",
    "model_servers": "configuration: one row an OpenAI-compatible server the user added",
    "model_capabilities": "configuration: one row a probed or overridden model",
    "place_providers": "configuration: one row a place provider",
    "server_secrets": "configuration: one sealed secret a kind",
    "connector_credentials": "configuration: his Google Cloud clients",
    "connector_accounts": "the connected account; removed when he disconnects",
    "calendars": "the mirror of his Google calendar list",
    "devices": "his paired devices; removed when he unpairs (their rows cascade)",
    "owner_context": "one row: where he is now",
    "autonomy_levels": "his setting, one row an action type",
    "autonomy_unlocks": "one row an action type promoted, the record of earned autonomy",
    "skill_vectors": "one row a skill, rebuilt when the skill or the model changes",
    "pisa_skills": "his skills: he deletes them",
    "pisa_views": "his views, at most 50: he deletes them",
    "pisa_stops": "his stops' history (the ledger shows each); a few rows a year",
    "people": "his people: he deletes them (their rows cascade)",
    "place_notes": "his notes on places: he deletes them",
    "vault_documents": "his documents with an expiry: he deletes them",
    "subscription_marks": "his word on a subscription, one row a subscription",
    "ucp_merchants": "his merchant allowlist",
    "profile_claims": "what Pisa knows about him, under half-lives; he forgets a claim",
    "profile_extract_nights": "core-memory-1: one row a night, counts and euros only (the morning's count)",
    "profile_retirements": "core-memory-1: Pisa's own corrections of his profile, a few rows, restorable",
    "profile_portrait": "One row, his portrait, rewritten at night from his claims",
    "profile_off_limits": "what Pisa must not learn and the tombstones of forgotten claims, on purpose",
    "ritual_ideas_used": "outlives the asks' 90 days on purpose: never the same idea twice",
    "pisa_cue_quiet": "one row a cue kind (2 at most): quiet until he raises it, on purpose",
    "pisa_his_states": 'his own words he asked Pisa to keep ("sì, ricordalo"): his until he deletes them',
    "pisa_character": "Pisa's traits, one row a trait of code's fixed list, each his to approve",
    "pisa_opinions": "one row an opinion Pisa gave once: outlives everything on purpose, never said twice",
    # What Pisa learned from their words stays when their retention removes the message;
    # the row keeps its own text and loses the pointer (ON DELETE SET NULL); a device's deletion takes it
    "pisa_topics": "the topics he named (his words): kept, the message pointer nulled by his retention",
    "pisa_idea_fragments": "his idea fragments (his words): kept, the message pointer "
    "nulled by his retention",
    "pisa_bets": "his bets (his sentence, `claim`): kept, the message pointer nulled by his retention (0090)",
    # Kept until they delete (Settings → delete by family and age: `services/erase.py`)
    # their days and Pisa's record of them (were PRUNED)
    "briefings": "Deletable: Pisa's record of what it said (his deletion: records)",
    "notifications": "Deletable: Pisa's record of what it said (records)",
    "reminders_sent": "Deletable: Pisa's record of what it reminded (records)",
    "plans": "Deletable: his days' plans (records)",
    "check_ins": "Deletable: Pisa's check-ins and his answers (records)",
    "handoff_preps": "Deletable: what Pisa prepared for his tap (records; handoffs cascade)",
    "understand_corrections": 'Deletable: his "heard X, meant Y" (records)',
    "done_by_presence": "Deletable: what Pisa closed by itself (records)",
    "email_drafts": "Deletable: Pisa's drafts (mail)",
    "approvals": "Deletable: what he approved or refused (records)",
    "pisa_decisions": "Deletable: Pisa's decision log (records)",
    "personal_evals": "Deletable: his corrections (records; scenario candidates cascade)",
    "pisa_owner_reports": "Deletable: the feedback loop's numbers (records)",
    "pisa_replay_runs": "Deletable: the replay's runs (records)",
    "pisa_hidden_text": "Deletable: the quarantine's hidden-text counts a day (numbers only, records)",
    "talk_sessions": "Deletable: his talk sessions, the timeline no longer nulled (messages; turns cascade)",
    "owner_feedback": "Deletable: his notes for the architect (records)",
    "device_diagnostics": "Deletable: his devices' readings (records)",
    "device_app_events": "Deletable: his devices' readings (records)",
    "device_metrics": "Deletable: his devices' readings (records)",
    "attend_candidates": "Deletable: what Pisa could have said unasked (records)",
    "guardian_items": "Every hold and release of Pisa's unprompted words, ids and numbers (records)",
    "pisa_promises": "Deletable: Pisa's own promises (records)",
    "ritual_asks": "Deletable: Pisa's questions (records)",
    "ritual_sheets": "Deletable: the rituals' sheets (records)",
    "pisa_cue_asks": "Deletable: Pisa's cue-questions, counts only (records)",
    "flight_claims": "Deletable: his money (money)",
    "refunds_promised": "Deletable: his money (money)",
    "bill_checks": "Deletable: his money (money)",
    "owner_promises": "Deletable: his promises (money)",
    "supply_tasks": "Deletable: his supplies (money)",
    "purchases": "Deletable: his purchases (money)",
    "device_locations": "Deletable: his GPS points, kept on his word (places)",
    "device_visits": "Deletable: the iPhone's visits (places)",
    "stays": "Deletable: where he was (places; health-sensitive ones only on his question)",
    "email_messages": "Deletable: Pisa's copies of his mail, bodies and gists too (mail)",
    "email_correspondents": "Deletable: who he writes to (mail)",
    "email_rule_log": "Deletable: the email rules' undo log (mail)",
    "mailed_invites": "Deletable: the UIDs of the invitations in their mail (mail)",
    "llm_calls": "Deletable: the cost ledger (records); the budget reads its months by `created_at`",
    "task_bulk": "Deletable: his bulk changes, the ledger's lines (records)",
    "errand_suggestions": "Deletable: Pisa's errand suggestions (records)",
    "walk_recaps": "Deletable: his walks (records)",
    "morning_thoughts": "Deletable: Pisa's morning thoughts (records)",
    "pisa_nights": "Deletable: the night's records (memory; its children cascade)",
    "pisa_meter": "Deletable: the night's meter (memory)",
    "pisa_sent_mail": "Deletable: what Pisa sent for him (mail)",
    # were UNDECIDED
    "episodes": "Deletable: his days as episodes; he forgets one, or deletes by age (memory)",
    "pisa_explicit": "Deletable: his +1/-1; a -1 outlives its episode (memory)",
    "feedback_events": "Deletable: his changes to Pisa's decisions (records)",
    "recommendation_outcomes": "Deletable: what happened to Pisa's recommendations (records)",
    "money_seen": "Deletable: one row an email recognised (money)",
    "hunches": "Deletable: Pisa's guesses about him (memory)",
    "plan_adjustments": "Deletable: his changes to a day's plan (records)",
    "things": "Deletable: orders, bookings, trips (money; items and keys cascade)",
    "trips": "Deletable: his trips (places)",
    "residency_days": "Deletable: one row a day, the residency counter (places)",
    "pisa_desk_nights": "Deletable: the desk's page count (memory)",
    "pisa_desk_scores": "Deletable: the journal's scores (memory)",
    "pisa_desk_windows": "Deletable: the window calendar (memory)",
    "pisa_idea_deltas": "Deletable: the night's deltas (memory)",
    "pisa_idea_map": "Deletable: the stepping-stone map (memory)",
    # The registry, their health and sport days, their bank, the admin radar
    "connectors": "configuration: one row a connector (code's list), his switch and his sealed credential",
    "health_days": "Deletable: his health and sport days, derived numbers only (health)",
    "bank_accounts": "Deletable: the accounts his bank's consent shows, last 4 digits only (money)",
    "bank_transactions": "Deletable: his bank's movements, read-only (money)",
    "obligations": "Deletable: the admin radar's dates, archived never deleted by Pisa (records)",
    "intentions": "Deletable: his intentions and what he waits for, archived never deleted by Pisa (records)",
    "dossier_items": "Deletable: what Pisa said each morning and what it put on his phone, "
    "his thumbs (records)",
    "day_rehearsals": "Deletable: the night's rehearsal of his day, only its conflicts (records)",
    "plan_actuals": "Deletable: his days' planned against actual minutes (records)",
    "plan_bands": "Deletable: his spread per kind, made from his days, recomputed each night (records)",
    # Their voice, kept until they delete it (Settings, or the `voice` family)
    "voice_recordings": "His enrolment recordings, sealed, kept to rebuild the prints (voice)",
    "voice_profiles": "His voiceprints per route, sealed (voice)",
    "voice_consents": "His consent to record his voice, with the text's version (voice)",
    "voice_cohort": "Public voices' embeddings (FLEURS), rebuilt by voice-cohort, nothing of his",
    "voice_babble": "The bench's public babble (FLEURS), rebuilt by voice-cohort, nothing of his",
    # Their sources and Pisa's answers from them, kept until they delete them (the `learn` family)
    "learn_sources": "His papers, pages and files, with their state and licence (learn)",
    "learn_answer_log": "Each answer from his sources: claims, verdicts, latency (learn)",
    # A security mark that must outlive the sync window
    "event_outside": "Each event or series id marked outside (ids, a time)",
}

# table -> kept today, no rule decided yet (a decision for the user or the operator). May only shrink.
UNDECIDED: dict[str, str] = {}
UNDECIDED_CEILING = 0  # all ruled: kept until they delete; never raise it


CREATE = r"(?i)\bcreate\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?(\w+)"
RENAME = r"(?i)\balter\s+table\s+\w+\s+rename\s+to\b"


def migration_tables() -> set[str]:
    """Every table the migrations leave: created, less dropped."""
    created: set[str] = set()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text())
        for name in re.findall(CREATE, text):
            created.add(name.lower())
        for name in re.findall(r"(?i)\bdrop\s+table\s+(?:if\s+exists\s+)?(\w+)", text):
            created.discard(name.lower())
        assert not re.search(RENAME, text), f"{path.name}: teach me renames"
    return created


RULES = (PRUNED, CASCADE, KEPT, UNDECIDED)


def test_every_table_has_a_rule() -> None:
    tables = migration_tables()
    assert len(tables) > 100  # the parse found the schema
    ruled = [t for rule in RULES for t in rule]
    assert len(ruled) == len(set(ruled)), "a table is in two lists"
    missing = sorted(tables - set(ruled))
    assert not missing, f"no retention rule (PRUNED, CASCADE, KEPT or UNDECIDED in this file): {missing}"
    assert not sorted(set(ruled) - tables), "a listed table no longer exists"


def test_reasons_are_given() -> None:
    for rule in (KEPT, UNDECIDED):
        assert all(reason.strip() for reason in rule.values())
    assert all(why.strip() for _, why in PRUNED.values())
    assert "messages" not in UNDECIDED  # Their setting
    assert len(UNDECIDED) <= UNDECIDED_CEILING


def test_pruned_tables_are_pruned_where_named() -> None:
    for table, (file, _) in PRUNED.items():
        source = (PISA_ROOT / file).read_text()
        assert re.search(rf"(?:DELETE FROM|UPDATE)\s+{table}\b", source), f"{table}: no prune in {file}"


def test_prunes_are_scheduled() -> None:
    worker = (PISA_ROOT / "workers" / "main.py").read_text()
    for job in ("prune_history", "prune_places"):
        assert f"maintenance.{job}(" in worker
    for gone in ("prune_locations", "prune_email"):  # Their locations and mail are kept
        assert gone not in worker
    assert "from pisa.services.places import prune" in (PISA_ROOT / "services" / "maintenance.py").read_text()


def test_every_kept_table_of_his_is_in_a_deletion_family() -> None:
    """What Pisa keeps of his, he can delete: a KEPT reason naming a family lists the table there."""
    families = {p.table: family for family, parts in erase_rules.FAMILIES.items() for p in parts}
    tables = migration_tables()
    assert set(families) <= tables
    for table, reason in KEPT.items():
        named = re.search(r"\((?:[^)]*; )?(\w+)(?:;[^)]*)?\)$", reason)
        if reason.startswith("Deletable") and named and named.group(1) in erase_rules.FAMILIES:
            assert families.get(table) == named.group(1), table
    for table in families:
        assert table == "messages" or table in KEPT or table in CASCADE, f"{table}: deletable, not KEPT"


def test_cascades_are_real(db: psycopg.Connection) -> None:
    rows = db.execute(
        "SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint "
        "WHERE contype = 'f' AND confdeltype = 'c'"
    ).fetchall()
    cascades = {(child, parent) for child, parent in rows}
    for child, parent in CASCADE.items():
        assert (child, parent) in cascades, f"{child} does not cascade from {parent}"
        assert any(parent in rule for rule in RULES), f"{parent} has no rule"


def test_the_parse_matches_the_database(db: psycopg.Connection) -> None:
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_type = 'BASE TABLE' AND table_name <> 'schema_migrations'"
    ).fetchall()
    assert {r[0] for r in rows} == migration_tables()
