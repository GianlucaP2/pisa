import json

from pisa_conformance.reference.all_events import pisa_events
from pisa_conformance.reference.consent import check_run, disclosure
from pisa_conformance.validate import EXT_ID, errors

PROCS = [{"slot": "conversation", "provider": {"id": "example-cloud", "name": "Example Cloud AI", "kind": "cloud"},
          "model": "example-model-large", "dataCategories": ["conversation"]}]


def test_disclosure_validates_and_its_revision_tracks_the_model():
    d = disclosure(PROCS)
    assert errors(d, EXT_ID + "#/$defs/AIDisclosure") == []
    changed = json.loads(json.dumps(PROCS)); changed[0]["model"] = "example-model-small"
    assert disclosure(changed)["revision"] != d["revision"]


def test_stale_or_missing_consent_is_refused_and_health_needs_its_own():
    d = disclosure(PROCS)
    assert check_run({}, d, False) == "ai_consent_required"
    assert check_run({"consent": {"aiRevision": "0" * 32}}, d, False) == "ai_consent_required"
    assert check_run({"consent": {"aiRevision": d["revision"]}}, d, False) is None
    assert check_run({"consent": {"aiRevision": d["revision"]}}, d, True) == "ai_consent_required"
    assert check_run({"consent": {"aiRevision": d["revision"], "health": True}}, d, True) is None


def test_pairing_qr_carries_no_long_term_secret():
    qr = {"v": 1, "addrs": ["https://pisa-home.local:8443", "https://[2001:db8::1]:8443", "relay://relay.example.net/h-123"],
          "spki": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", "token": "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "exp": 1763460600}
    assert errors(qr, EXT_ID + "#/$defs/PairingQR") == []
    assert errors(dict(qr, deviceToken="x"), EXT_ID + "#/$defs/PairingQR") != []   # nothing else may ride in the QR


def test_health_notice_carries_its_privacy_class():
    n = {"noticeId": "44444444-4444-4444-8444-444444444444", "kind": "reminder", "priority": "normal", "lang": "en-US",
         "text": "Take the evening medicine", "privacy": "health", "genericText": "A reminder from Pisa"}
    assert errors(n, EXT_ID + "#/$defs/s_pisa_notice") == []
