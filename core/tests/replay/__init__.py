"""The end-state replay through Pisa's own ConversationService. `scenarios` holds the cases (the
research replay's 20, rewritten for Pisa's tools, and the user's incidents); `harness` seeds a world, plays
the user's turns through Pisa and scores the end state. CI plays each with the scripted model
(`test_replay.py`); maintenance → `brain-replay` plays them with the real model
(`scripts/maintenance/brain_replay.py`)."""
