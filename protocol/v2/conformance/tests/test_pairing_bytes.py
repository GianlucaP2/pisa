"""Pairing, sessions, the relay signature and the sealed push (CONTRACT-2 §3.4, §4.3, §9), from fixtures/pairing/vectors.json."""
import base64
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, hpke, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from conftest import V2
from pisa_conformance.reference import pairing_bytes as P
from pisa_conformance.reference import pairing_vectors
from pisa_conformance.validate import EXT_ID, errors

V = json.loads((V2 / "fixtures" / "pairing" / "vectors.json").read_text(encoding="utf-8"))
PA = V["pairing"]
IN = PA["inputs"]


def test_vectors_are_reproducible():
    assert V == json.loads(json.dumps(pairing_vectors.build(), ensure_ascii=False))


# ---------- HPKE: our own implementation against RFC 9180 and against cryptography's ----------

H = bytes.fromhex
RFC = {"info": "4f6465206f6e2061204772656369616e2055726e", "skEm": "f4ec9b33b792c372c1d2c2063507b684ef925b8c75a42dbcbf57d63ccd381600",
       "pkRm": "4310ee97d88cc1f088a5576c77ab0cf5c3ac797f3d95139c6c84b5429c59662a",
       "skRm": "8057991eef8f1f1af18f4a9491d16a1ce333f695d4db8e38da75975c4478e0fb",
       "enc": "1afa08d3dec047a643885163f1180476fa7ddb54c6a8029ea33f95796bf2ac4a",
       "shared_secret": "0bbe78490412b4bbea4812666f7916932b828bba79942424abb65244930d69a7",
       "key": "ad2744de8e17f4ebba575b3f5f5a8fa1f69c2a07f6e7500bc60ca6e3e3ec1c91", "base_nonce": "5c4d98150661b848853b547f",
       "pt": "4265617574792069732074727574682c20747275746820626561757479", "aad": "436f756e742d30",
       "ct": "1c5250d8034ec2b784ba2cfd69dbdb8af406cfe3ff938e131f0def8c8b60b4db21993c62ce81883d2dd1b51a28"}


def test_hpke_matches_rfc9180_a21():
    shared, enc = P.hpke_encap(H(RFC["pkRm"]), H(RFC["skEm"]))
    assert enc.hex() == RFC["enc"] and shared.hex() == RFC["shared_secret"]
    key, nonce = P.hpke_key_schedule(shared, H(RFC["info"]))
    assert key.hex() == RFC["key"] and nonce.hex() == RFC["base_nonce"]
    enc, ct = P.hpke_seal(H(RFC["pkRm"]), H(RFC["info"]), H(RFC["aad"]), H(RFC["pt"]), H(RFC["skEm"]))
    assert ct.hex() == RFC["ct"]
    assert P.hpke_open(H(RFC["skRm"]), enc, H(RFC["info"]), H(RFC["aad"]), ct) == H(RFC["pt"])


def test_hpke_interoperates_with_cryptography_both_ways():
    suite = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
    for n in (0, 1, 464, 2000):
        r = X25519PrivateKey.generate()
        sk = r.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        pt = os.urandom(n)
        enc, ct = P.hpke_seal(P._raw(r.public_key()), P.PUSH_INFO, b"", pt, os.urandom(32))
        assert suite.decrypt(enc + ct, r, info=P.PUSH_INFO) == pt  # cryptography's HPKE takes no aad: empty here
        blob = suite.encrypt(pt, r.public_key(), info=P.PUSH_INFO)
        assert P.hpke_open(sk, blob[:32], P.PUSH_INFO, b"", blob[32:]) == pt


# ---------- pairing ----------

def test_pin_is_padded_standard_base64_and_matches_the_endpoints_pattern():
    der = base64.b64decode(IN["tlsSpkiDerBase64"])
    assert PA["spkiPin"] == P.spki_pin(der) and len(PA["spkiPin"]) == 44 and PA["spkiPin"].endswith("=")
    assert errors({"revision": "r", "endpoints": [{"kind": "lan", "address": "192.0.2.10"}], "spki": [PA["spkiPin"], PA["nextSpkiPin"]]},
                  EXT_ID + "#/$defs/s_pisa_endpoints") == []


def test_qr_and_typed_fallback():
    assert PA["qr"] == P.qr_link(IN["addrs"], PA["spkiPin"], bytes.fromhex(IN["tokenHex"]), IN["exp"])
    parsed = P.parse_qr(PA["qr"])
    assert parsed == {"v": 1, "addrs": IN["addrs"], "spki": PA["spkiPin"], "token": PA["token"], "exp": IN["exp"]}
    assert errors(parsed, EXT_ID + "#/$defs/PairingQR") == []
    assert "%2Cb" in PA["qr"] and "%2B" in PA["qr"]  # an address's own comma, and the pin's + are encoded
    fully = PA["qr"].replace("192.0.2.10:8443", "192.0.2.10%3A8443")  # a reader takes any valid percent-encoding
    assert P.parse_qr(fully) == parsed
    for typed in PA["typedVariants"]:
        assert P.token_from_typed(typed).hex() == IN["tokenHex"]
    for bad in PA["rejects"]["typed"]:
        with pytest.raises(ValueError):
            P.token_from_typed(bad)


def test_transcript_macs_and_sas():
    tok = bytes.fromhex(IN["tokenHex"])
    tr = P.transcript(PA["spkiPin"], IN["deviceKey"], IN["clientNonce"])
    assert tr.decode() == PA["transcript"] == "\n".join(["pisa-pair/1", PA["spkiPin"], IN["deviceKey"], IN["clientNonce"]])
    assert P.app_mac(tok, tr) == PA["appMac"] and P.server_mac(tok, tr, IN["serverNonce"]) == PA["serverMac"]
    assert P.sas(tr, IN["serverNonce"]) == PA["sas"] and len(PA["sas"]) == 6 and PA["sasDisplay"] == f"{PA['sas'][:3]} {PA['sas'][3:]}"
    assert PA["rejects"]["macWithTheNextPin"] != PA["appMac"]  # the server tries its current pin, then the next one
    req = {"v": 1, "pairingId": "pair-1", "deviceKey": IN["deviceKey"], "name": "Test phone", "platform": "ios", "clientNonce": IN["clientNonce"], "mac": PA["appMac"]}
    assert errors(req, EXT_ID + "#/$defs/PairRequest") == []


def test_commitment_comes_first_and_a_bad_reveal_aborts():
    assert PA["commitment"] == P.sas_commitment(IN["serverNonce"]) and len(PA["commitment"]) == 43
    assert P.reveal_matches(PA["commitment"], IN["serverNonce"])
    bad = PA["revealMismatch"]
    assert bad["mustAbort"] and not P.reveal_matches(bad["commitment"], bad["revealedServerNonce"])
    assert errors({"pairingId": "pair-1", "commitment": PA["commitment"], "expiresAt": "2026-11-18T10:10:00Z"}, EXT_ID + "#/$defs/PairBegin") == []
    no_id = {"v": 1, "deviceKey": IN["deviceKey"], "name": "x", "platform": "ios", "clientNonce": IN["clientNonce"], "mac": PA["appMac"]}
    assert errors(no_id, EXT_ID + "#/$defs/PairRequest") != []  # a pair request names the begun pairing


def test_commitment_defeats_grinding_toward_the_app():
    """With C fixed before the app's clientNonce, the serverNonce is fixed too: no other nonce opens C, so the
    SAS the app shows cannot be steered after the fact."""
    import os
    c = P.sas_commitment(IN["serverNonce"])
    assert not any(P.reveal_matches(c, P.b64url(os.urandom(16))) for _ in range(2000))


def test_session_signature_and_expiry():
    s = V["session"]
    assert s["text"] == f"pisa-session/1\n{s['deviceId']}\n{s['nonce']}"
    key = serialization.load_der_public_key(base64.b64decode(s["devicePublicKeySpkiBase64"]))
    key.verify(base64.b64decode(s["signatureDerBase64"]), s["text"].encode(), ec.ECDSA(hashes.SHA256()))
    assert len(P.unb64url(s["nonce"])) == 32
    assert s["expiry"]["live"] == {"closeCode": 4003, "closeReason": "session_expired", "controlFrame": None}


def test_relay_signature():
    r = V["relay"]
    sec = bytes.fromhex(r["secretHex"])
    assert P.relay_key(sec).hex() == r["keyHex"]
    assert P.relay_headers(sec, r["timestamp"], r["body"].encode()) == r["headers"]
    assert P.relay_signature(sec, r["timestamp"] + 1, r["body"].encode()) != r["headers"]["X-Pisa-Relay-Signature"]
    assert P.relay_signature(sec, r["timestamp"], r["body"].encode() + b" ") != r["headers"]["X-Pisa-Relay-Signature"]


@pytest.mark.parametrize("case", V["push"]["cases"], ids=lambda c: c["name"])
def test_sealed_push(case):
    pu = V["push"]
    sk, pk = bytes.fromhex(pu["recipientPrivateKeyHex"]), bytes.fromhex(pu["recipientPublicKeyHex"])
    assert P.seal_push(pk, pu["aad"], case["plaintext"].encode(), bytes.fromhex(case["ephemeralPrivateKeyHex"])) == case["sealed"]
    opened = P.open_push(sk, pu["aad"], case["sealed"])
    assert opened == json.loads(case["plaintext"])
    assert errors(opened, EXT_ID + "#/$defs/PushPlaintext") == []
    assert len(P.unb64url(case["sealed"])) == case["sealedLength"] in (512, 2048)
    core = case["plaintext"].rstrip(" ")
    assert json.loads(core) == opened and " " not in core[-1:] and len(case["plaintext"].encode()) in (464, 2000)
    with pytest.raises(Exception):
        P.open_push(sk, "another-handle", case["sealed"])  # aad = the handle: a blob moved to another handle does not open
    blob = bytearray(P.unb64url(case["sealed"]))
    blob[40] ^= 1
    with pytest.raises(Exception):
        P.open_push(sk, pu["aad"], P.b64url(bytes(blob)))


def test_relay_answers_and_422_for_a_dead_activity_token():
    got = {(a["status"], a["type"]): a["action"] for a in V["relay"]["answers"]}
    assert got[(422, "liveactivity")] == "drop_activity_token"  # the handle stays registered
    assert got[(404, "alert")] == got[(410, "background")] == "delete_registration"  # the registration is gone
    assert got[(422, "alert")] == "log" and got[(503, "liveactivity")] == "log" and got[(202, "alert")] == "sent"
    assert got[(413, "alert")] == "log"  # too large: log, never retry, never delete the registration (E1 R18)
    for a in V["relay"]["answers"]:
        assert P.relay_answer_action(a["status"], a["type"]) == a["action"]


def test_sealed_push_carries_sent_at_in_utc():
    for c in V["push"]["cases"]:
        doc = json.loads(c["plaintext"])
        assert doc["sentAt"].endswith("Z")
        assert errors(doc, EXT_ID + "#/$defs/PushPlaintext") == []
        assert errors({k: v for k, v in doc.items() if k != "sentAt"}, EXT_ID + "#/$defs/PushPlaintext") != []
        assert errors(dict(doc, sentAt="2026-11-18T11:00:00+01:00"), EXT_ID + "#/$defs/PushPlaintext") != []


def test_the_extension_drops_replays_old_and_future_pushes():
    nse = V["push"]["nse"]
    assert nse["windowSeconds"] == 7 * 24 * 3600
    gate = P.NoticeGate()
    for c in nse["cases"]:
        assert gate.accept(c["plaintext"], c["now"]) == c["result"], c["name"]
    assert [c["result"] for c in nse["cases"]] == ["show", "repeat", "show", "stale", "show", "future"]


def test_the_seen_set_is_bounded():
    import datetime
    gate, base = P.NoticeGate(), 1_800_000_000
    sent = datetime.datetime.fromtimestamp(base, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(P.NSE_SEEN_MAX + 50):
        assert gate.accept({"sentAt": sent, "notice": {"noticeId": f"id-{i}"}}, base) == "show"
    assert len(gate.seen) == P.NSE_SEEN_MAX and "id-0" not in gate.seen


def test_relay_after_the_security_fixes():
    r = V["relay"]
    got = {(a["status"], a["type"]): a["action"] for a in r["answers"]}
    assert got[(401, "liveactivity")] == "log"  # a token the app did not register for this handle; Apple is not asked
    for st, ty in ((500, "alert"), (502, "liveactivity"), (503, "liveactivity")):
        assert got[(st, ty)] == "log"  # never "gone": the registration is kept
    assert r["exactRepeat"] == {**r["exactRepeat"], "first": 202, "repeat": 202, "sentToApple": 1}
    reg = {(a["status"], a.get("error")): a["action"] for a in r["registerAnswers"]}
    assert reg[(409, "too_many_devices")] == "open_devices_screen" and reg[(429, "too_many_registrations")] == "retry_later"
    assert reg[(201, None)] == "store_handle_and_secret" and reg[(403, "account_not_verified")] == "show_subscription_problem"
    assert reg[(403, "not_allowed")] == "explain_not_allowed" and reg[(404, "unknown_device")] == "refresh_devices"
    assert r["limits"]["handlesPerSubscription"] == 10 and r["limits"]["evictIdleDays"] == 30
    assert r["limits"]["registerPerSubscriptionHour"] == 5
    assert not [k for k in r["limits"] if "Global" in k or "PerMinute" in k]  # no global registration limit (E1 N1b)


def test_the_apps_own_relay_requests_are_signed_with_app_secret():
    api = V["relay"]["appApi"]
    app_secret = bytes.fromhex(api["appSecretHex"])
    assert set(api["registerAnswer"]) == {"handle", "secret", "app_secret"}
    assert "app_secret" not in api["givenToTheUsersServer"]  # it never leaves the phone
    routes = set()
    for q in api["requests"]:
        routes.add((q["method"], q["path"]))
        assert P.relay_app_headers(app_secret, q["method"], q["path"], q["timestamp"], q["body"].encode()) == q["headers"]
    assert routes == {("POST", "/v1/register/activity"), ("POST", "/v1/register/devices"), ("POST", "/v1/register/devices/remove"),
                      ("DELETE", "/v1/register")}
    activity = api["requests"][0]["headers"]["X-Pisa-Relay-Signature"]
    other = api["notAcceptedOnOtherRoutes"]
    assert activity != other["activityMacForAnotherPath"] and activity != other["activityWithServerConstruction"]
    d = api["deleteByTheServer"]  # DELETE /v1/register also takes the server's secret, with §9's construction
    sec = bytes.fromhex(V["relay"]["secretHex"])
    assert P.relay_headers(sec, int(d["headers"]["X-Pisa-Relay-Timestamp"]), d["body"].encode()) == d["headers"]
    with pytest.raises(ValueError):
        P.relay_app_mac(app_secret, "post", "/v1/register/devices", 1, b"{}")
    with pytest.raises(ValueError):
        P.relay_app_mac(app_secret, "POST", "/v1/register/devices?x=1", 1, b"{}")
    assert [x["this_device"] for x in api["devicesAnswer"]["devices"]].count(True) == 1


def test_app_account_token_matches_the_published_vector():
    t = V["relay"]["appApi"]["appAccountToken"]
    assert t["token"] == t["published"] == P.app_account_token(bytes.fromhex(t["accountKeyHex"]))
    import uuid
    u = uuid.UUID(t["token"])
    assert u.version == 8 and u.variant == uuid.RFC_4122


def test_live_activity_in_relay_mode_carries_no_progress_or_time():
    body = V["relay"]["liveActivityRelayMode"]
    assert body["type"] == "liveactivity" and "progress" not in body["activity"] and "time" not in body["activity"]
    with pytest.raises(ValueError):
        P.live_activity_body("h", "a1" * 32, "update", 1, progress=0.5)


def test_the_relay_sends_an_empty_content_state():
    aps = V["relay"]["liveActivityApsRelayMode"]
    assert aps["content-state"] == {} and aps["event"] == "update"
    assert aps == P.live_activity_aps(V["relay"]["liveActivityRelayMode"]["activity"])


def test_device_removal_ignores_the_order_of_registration():
    rules = V["relay"]["appApi"]["removalRule"]
    assert [r["allowed"] for r in rules] == [True, True, True, False, False]
    for r in rules:
        args = {k: v for k, v in r.items() if k not in ("name", "allowed")}
        assert P.may_remove_device(**args) == r["allowed"], r["name"]


def test_relay_mode_refuses_stale_date_progress_and_time():
    refused = V["relay"]["liveActivityRelayModeRefused"]
    assert {next(k for k in r["activity"] if k in ("staleDate", "progress", "time")) for r in refused} == {"staleDate", "progress", "time"}
    for r in refused:
        with pytest.raises(ValueError):
            P.live_activity_aps(r["activity"])
        extra = {k: v for k, v in r["activity"].items() if k not in ("token", "event", "priority", "timestamp")}
        with pytest.raises(ValueError):
            P.live_activity_body("h", r["activity"]["token"], "update", 1, **extra)
    assert "stale-date" not in V["relay"]["liveActivityApsRelayMode"]


def test_a_relay_mode_start_sends_empty_attributes_and_its_type():
    st = V["relay"]["liveActivityStartRelayMode"]
    aps = st["aps"]
    assert aps["event"] == "start" and aps["attributes"] == {} and aps["content-state"] == {}
    assert aps["attributes-type"] == "NextStopAttributes" and aps["input-push-token"] == 1
    assert aps == P.live_activity_aps(st["body"]["activity"])
    for bad in V["relay"]["liveActivityStartRefused"]:
        with pytest.raises(ValueError):
            P.live_activity_aps(bad["activity"])
    upd = V["relay"]["liveActivityApsRelayMode"]
    assert "attributes" not in upd and "attributes-type" not in upd  # only a start carries them
