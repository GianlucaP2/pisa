"""Writes fixtures/pairing/vectors.json: pairing, sessions, the relay signature and the sealed push.

Every input is fixed (keys derived from constants, RFC 6979 deterministic ECDSA, a fixed HPKE ephemeral key), so
the file is reproducible byte for byte and a test checks it against this module. All keys here are TEST KEYS,
published on purpose so any implementation can recompute and open everything; never use them for anything else.
Run: python -m pisa_conformance.reference.pairing_vectors
"""
from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from ..paths import FIXTURES
from . import pairing_bytes as P

OUT = FIXTURES / "pairing" / "vectors.json"


def _p256(scalar: int) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(scalar, ec.SECP256R1())


def _spki(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _x25519(seed: bytes) -> tuple[bytes, bytes]:
    k = X25519PrivateKey.from_private_bytes(seed)
    return seed, P._raw(k.public_key())


TLS_KEY = _p256(0x5EED_0001_7E57_0000_0000_0000_0000_0000_0000_0000_0000_0000_0000_0000_0000_0001)
NEXT_TLS_KEY = _p256(0x5EED_0001_7E57_0000_0000_0000_0000_0000_0000_0000_0000_0000_0000_0000_0000_0002)
DEVICE_KEY = _p256(0x5EED_0002_7E57_0000_0000_0000_0000_0000_0000_0000_0000_0000_0000_0000_0000_0003)
TOKEN = bytes.fromhex("8f1e2d3c4b5a69788796a5b4c3d2e1f0")
CLIENT_NONCE = P.b64url(bytes.fromhex("00112233445566778899aabbccddeeff"))
SERVER_NONCE = P.b64url(bytes.fromhex("ffeeddccbbaa99887766554433221100"))
ADDRS = ["192.0.2.10:8443", "[2001:db8::10]:8443", "pisa.example:443", "relay.example.net:443/r?h=a,b"]
EXP = 1763460600
DEVICE_ID = "00000000-0000-4000-8000-0000000000d1"
RECIPIENT_SK = bytes.fromhex("7e57" * 16)
EPHEMERAL_SKS = [bytes.fromhex("e1" * 32), bytes.fromhex("e2" * 32)]
HANDLE = "h-7Qm2Xc9LkP4tVb"
RELAY_SECRET = bytes.fromhex("5ec2e7" + "00" * 29)


SENT_AT = "2026-11-18T10:00:00Z"
NOW = int(__import__("datetime").datetime.fromisoformat(SENT_AT.replace("Z", "+00:00")).timestamp()) + 60  # one minute after SENT_AT


def _nse_cases() -> list[dict[str, Any]]:
    """What the Notification Service Extension shows, in order, from one seen-set."""
    def pt(nid: str, sent: str) -> dict[str, Any]:
        return {"v": 2, "notice": dict(_notice("x"), noticeId=nid), "sentAt": sent}
    seq = [("first delivery", pt("44444444-4444-4444-8444-444444444444", SENT_AT), NOW),
           ("the same sealed blob replayed", pt("44444444-4444-4444-8444-444444444444", SENT_AT), NOW + 30),
           ("another notice", pt("55555555-5555-4555-8555-555555555555", SENT_AT), NOW + 60),
           ("an old blob replayed 8 days later (a noticeId long forgotten)", pt("66666666-6666-4666-8666-666666666666", "2026-11-10T09:00:00Z"), NOW),
           ("just inside the window", pt("77777777-7777-4777-8777-777777777777", "2026-11-11T10:02:00Z"), NOW),
           ("dated more than 5 minutes ahead of the phone's clock", pt("88888888-8888-4888-8888-888888888888", "2026-11-18T11:00:00Z"), NOW)]
    gate, out = P.NoticeGate(), []
    for name, plaintext, now in seq:
        out.append({"name": name, "plaintext": plaintext, "now": now, "result": gate.accept(plaintext, now)})
    return out


APP_SECRET = bytes.fromhex("a99" + "0" * 61)
ACCOUNT_KEY = bytes(range(32))


def _app_api() -> dict[str, Any]:
    """The app's own signed requests (app_secret), the register answer, and appAccountToken."""
    def req(method: str, path: str, body_obj: dict[str, Any], ts: int) -> dict[str, Any]:
        body = json.dumps(body_obj, separators=(",", ":")).encode()
        return {"method": method, "path": path, "timestamp": ts, "body": body.decode(),
                "headers": P.relay_app_headers(APP_SECRET, method, path, ts, body)}
    activity = req("POST", "/v1/register/activity", {"handle": HANDLE, "token": "a1" * 32, "kind": "start"}, 1763460005)
    delete_body = json.dumps({"handle": HANDLE}, separators=(",", ":")).encode()
    return {
        "appSecretHex": APP_SECRET.hex(),
        "label": "pisa-relay-app/1",
        "registerAnswer": {"handle": P.b64url(bytes(range(16))), "secret": P.b64url(RELAY_SECRET), "app_secret": P.b64url(APP_SECRET)},
        "givenToTheUsersServer": ["handle", "secret", "public_key"],
        "requests": [
            activity,
            req("POST", "/v1/register/devices", {"handle": HANDLE}, 1763460006),
            req("POST", "/v1/register/devices/remove", {"handle": HANDLE, "device": "dGhpc0lzQW5JZDEy"}, 1763460007),
            req("DELETE", "/v1/register", {"handle": HANDLE}, 1763460008),
        ],
        "deleteByTheServer": {"body": delete_body.decode(), "headers": P.relay_headers(RELAY_SECRET, 1763460009, delete_body),
                              "why": "DELETE /v1/register also takes the server's secret with §9's construction (unpair, revoke, "
                                     "leaving relay mode: E1 R13)"},
        "notAcceptedOnOtherRoutes": {"why": "the same body signed for another route, or with the server's construction, fails",
                                     "activityMacForAnotherPath": P.relay_app_mac(APP_SECRET, "POST", "/v1/register/devices", 1763460005,
                                                                            activity["body"].encode()),
                                     "activityWithServerConstruction": P.relay_signature(RELAY_SECRET, 1763460005, activity["body"].encode())},
        "devicesAnswer": {"devices": [{"id": "dGhpc0lzQW5JZDEy", "created": "2026-11-18", "last_used": "2026-11-20",
                                       "environment": "production", "this_device": True},
                                      {"id": "b3RoZXJEZXZpY2Ux", "created": "2026-10-02", "last_used": None,
                                       "environment": "production", "this_device": False}]},
        "removalRule": [dict(c, allowed=P.may_remove_device(**{k: v for k, v in c.items() if k != "name"})) for c in (
            {"name": "the signer proved the account key", "signer_proved_account_key": True, "target_is_signer": False,
             "target_idle_days": 0, "signer_registered_first": False},
            {"name": "the signer removes itself", "signer_proved_account_key": False, "target_is_signer": True,
             "target_idle_days": 0, "signer_registered_first": False},
            {"name": "the target has been idle 30 days", "signer_proved_account_key": False, "target_is_signer": False,
             "target_idle_days": 30, "signer_registered_first": False},
            {"name": "registered first, no key, target in use: refused (E1 N3b)", "signer_proved_account_key": False,
             "target_is_signer": False, "target_idle_days": 3, "signer_registered_first": True},
            {"name": "a later copy, no key, target in use: refused", "signer_proved_account_key": False, "target_is_signer": False,
             "target_idle_days": 29, "signer_registered_first": False})],
        "appAccountToken": {"accountKeyHex": ACCOUNT_KEY.hex(), "label": "pisa-relay/app-account-token",
                            "token": P.app_account_token(ACCOUNT_KEY), "published": "55ba834e-40eb-8f96-88a3-4ac1fd42cf59"},
    }


def _notice(text: str) -> dict[str, Any]:
    return {"noticeId": "44444444-4444-4444-8444-444444444444", "kind": "reminder", "priority": "normal", "lang": "en-US",
            "text": text, "privacy": "health", "genericText": "A reminder from Pisa", "wrist": "Reminder"}


APNS = {"category": "notice", "threadId": "pisa-notices", "interruption": "active"}


def build() -> dict[str, Any]:
    pin = P.spki_pin(_spki(TLS_KEY))
    device_key = base64.b64encode(_spki(DEVICE_KEY)).decode()
    tr = P.transcript(pin, device_key, CLIENT_NONCE)
    sas = P.sas(tr, SERVER_NONCE)
    link = P.qr_link(ADDRS, pin, TOKEN, EXP)
    session_nonce = P.b64url(bytes(range(32)))
    session_sig = base64.b64encode(DEVICE_KEY.sign(P.session_text(DEVICE_ID, session_nonce),
                                                   ec.ECDSA(hashes.SHA256(), deterministic_signing=True))).decode()
    body = json.dumps({"handle": HANDLE, "type": "alert", "collapse_id": "n-1", "sealed": "..."}, separators=(",", ":")).encode()
    _, recipient_pk = _x25519(RECIPIENT_SK)
    short = P.push_plaintext(_notice("Take the evening medicine"), APNS, sent_at=SENT_AT)
    long = P.push_plaintext(_notice("A longer note. " * 40), APNS, sent_at=SENT_AT)
    pushes = []
    for name, pt, ske in (("fits 512", short, EPHEMERAL_SKS[0]), ("needs 2048", long, EPHEMERAL_SKS[1])):
        sealed = P.seal_push(recipient_pk, HANDLE, pt, ske)
        pushes.append({"name": name, "plaintext": pt.decode("utf-8"), "plaintextLength": len(pt), "ephemeralPrivateKeyHex": ske.hex(),
                       "sealed": sealed, "sealedLength": len(P.unb64url(sealed))})
    return {
        "description": "PISA contract 2 pairing, session, relay and sealed-push vectors (CONTRACT-2 §3.4, §4.3, §9). TEST KEYS ONLY. "
                       "Strings are UTF-8; b64url is base64url without padding; the SPKI pin is standard base64 with padding.",
        "pairing": {
            "inputs": {"tlsSpkiDerBase64": base64.b64encode(_spki(TLS_KEY)).decode(), "tokenHex": TOKEN.hex(), "addrs": ADDRS, "exp": EXP,
                       "deviceKey": device_key, "clientNonce": CLIENT_NONCE, "serverNonce": SERVER_NONCE},
            "spkiPin": pin,
            "nextSpkiPin": P.spki_pin(_spki(NEXT_TLS_KEY)),
            "token": P.token_text(TOKEN),
            "qr": link,
            "typedVariants": [P.token_text(TOKEN).lower(), "-".join(P.token_text(TOKEN)[i:i + 5] for i in range(0, 26, 5)),
                              " ".join(P.token_text(TOKEN)[i:i + 4] for i in range(0, 26, 4))],
            "transcript": tr.decode("utf-8"),
            "appMac": P.app_mac(TOKEN, tr),
            "serverMac": P.server_mac(TOKEN, tr, SERVER_NONCE),
            "commitment": P.sas_commitment(SERVER_NONCE),
            "revealMismatch": {"commitment": P.sas_commitment(SERVER_NONCE), "revealedServerNonce": P.b64url(bytes(16)),
                               "mustAbort": True, "why": "the revealed serverNonce does not hash to the commitment the "
                               "server sent first: the app shows no digits, aborts with a plain error, and the token stays spent"},
            "sas": sas,
            "sasDisplay": P.sas_display(sas),
            "rejects": {"typed": ["ABCDEFGHIJKLMNOPQRSTUVWXY1", "TOO-SHORT"],
                        "macWithTheNextPin": P.app_mac(TOKEN, P.transcript(P.spki_pin(_spki(NEXT_TLS_KEY)), device_key, CLIENT_NONCE))},
        },
        "offers": {
            "valid": [{}, {"name": "Tablet"}],
            "refused": [{"why": "E1 P1: a signer key is host-only (the CLI on the server); the app never asks for one",
                         "body": {"name": "Tablet", "signer": True}, "status": 400, "error": "validation_failed"},
                        {"why": "an unknown field", "body": {"name": "Tablet", "admin": True}, "status": 400, "error": "validation_failed"}],
            "offerId": "offer_" + P.b64url(bytes(range(16))),
            "onlyTheOfferingDevice": {"otherDeviceStatus": 404, "routes": ["GET", "DELETE", "POST confirm"]},
            "pairedViaOffer": {"paired_via": "app", "mayRegisterFirstApprovalKey": False},
            "confirmRefused": {"status": 409, "error": "pairing_code_invalid"},
        },
        "session": {"deviceId": DEVICE_ID, "nonce": session_nonce, "text": P.session_text(DEVICE_ID, session_nonce).decode(),
                    "devicePublicKeySpkiBase64": device_key, "signatureDerBase64": session_sig,
                    "signature": "ECDSA P-256 SHA-256, DER, base64 (RFC 6979 deterministic here; a random k is equally valid)",
                    "expiry": {"live": {"closeCode": 4003, "closeReason": "session_expired", "controlFrame": None},
                               "rest": {"status": 401, "code": "session_expired"}}},
        "relay": {"secretHex": RELAY_SECRET.hex(), "keyHex": P.relay_key(RELAY_SECRET).hex(), "timestamp": 1763460000,
                  "body": body.decode(), "headers": P.relay_headers(RELAY_SECRET, 1763460000, body), "windowSeconds": 300,
                  "answers": [{"status": st, "type": ty, "action": P.relay_answer_action(st, ty)}
                              for st, ty in ((202, "alert"), (400, "alert"), (401, "alert"), (401, "liveactivity"), (404, "alert"),
                                             (410, "background"), (413, "alert"), (422, "liveactivity"), (422, "alert"), (429, "alert"),
                                             (500, "alert"), (502, "liveactivity"), (503, "liveactivity"))],
                  "registerAnswers": [dict({"status": st, "action": P.register_answer_action(st, err)}, **({"error": err} if err else {}))
                                      for st, err in ((201, None), (200, None), (204, None), (400, "malformed"), (401, None),
                                                      (403, "subscription_not_verified"), (403, "account_not_verified"),
                                                      (409, "too_many_devices"), (413, "too_large"), (429, "too_many_registrations"),
                                                      (404, "unknown_device"), (403, "not_allowed"), (500, None))],
                  "appApi": _app_api(),
                  "liveActivityRelayMode": P.live_activity_body(HANDLE, "a1" * 32, "update", 1763460010),
                  "liveActivityApsRelayMode": P.live_activity_aps(P.live_activity_body(HANDLE, "a1" * 32, "update", 1763460010)["activity"]),
                  "liveActivityStartRelayMode": {
                      "body": P.live_activity_body(HANDLE, "b2" * 32, "start", 1763460011, attributesType="NextStopAttributes"),
                      "aps": P.live_activity_aps(P.live_activity_body(HANDLE, "b2" * 32, "start", 1763460011,
                                                                      attributesType="NextStopAttributes")["activity"])},
                  "liveActivityStartRefused": [
                      {"why": "a push-to-start needs attributesType",
                       "activity": {"token": "b2" * 32, "event": "start", "priority": 5, "timestamp": 1763460011}},
                      {"why": "relay-mode attributes are {}: nothing about the user's day",
                       "activity": {"token": "b2" * 32, "event": "start", "priority": 5, "timestamp": 1763460011,
                                    "attributesType": "NextStopAttributes", "attributes": {"stop": "Dentist"}}}],
                  "liveActivityRelayModeRefused": [
                      {"why": f"{field} would tell the relay about the user's day (E1 R5, gap 2)",
                       "activity": {"token": "a1" * 32, "event": "update", "priority": 5, "timestamp": 1763460010, field: value}}
                      for field, value in (("staleDate", 1763463600), ("progress", 0.5), ("time", 1763462000))],
                  "limits": P.RELAY_LIMITS,
                  "exactRepeat": {"first": 202, "repeat": 202, "sentToApple": 1,
                                  "why": "the same signature (same second, same body) is the same request: answered 202, sent once"}},
        "push": {"suite": {"kem_id": P.KEM_ID, "kdf_id": P.KDF_ID, "aead_id": P.AEAD_ID}, "info": P.PUSH_INFO.decode(), "aad": HANDLE,
                 "recipientPrivateKeyHex": RECIPIENT_SK.hex(), "recipientPublicKeyHex": recipient_pk.hex(), "cases": pushes,
                 "nse": {"windowSeconds": P.NSE_WINDOW_S, "futureSeconds": P.NSE_FUTURE_S, "seenMax": P.NSE_SEEN_MAX,
                         "cases": _nse_cases()}},
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
