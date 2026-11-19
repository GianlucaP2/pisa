"""The proposed confirm-on-phone pairing (a paired phone adds a new device): its shape in the OpenAPI delta, and a
walk through it with the reference bytes: the offering phone derives the same SAS as the new device."""
import json

import jsonschema

from conftest import V2
from pisa_conformance.reference import pairing_bytes as P

DELTA = json.loads((V2 / "openapi-v2-delta.json").read_text())
SCHEMAS = DELTA["components"]["schemas"]
V = json.loads((V2 / "fixtures" / "pairing" / "vectors.json").read_text())


def schema_of(name):
    return {"$defs": SCHEMAS, **json.loads(json.dumps(SCHEMAS[name]).replace("#/components/schemas/", "#/$defs/"))}


def test_routes_are_proposed_and_signed_where_they_grant():
    offers = DELTA["paths"]["/v1/pair/offers"]["post"]
    confirm = DELTA["paths"]["/v1/pair/offers/{offerId}/confirm"]["post"]
    assert "x-pisa-shape" not in offers and "x-pisa-shape" not in confirm  # settled (CONTRACT-SHAPES §6.1)
    assert offers["x-pisa-signed-op"]["op"] == "pairing_offer" and confirm["x-pisa-signed-op"]["op"] == "pairing_confirm"
    assert "<pairingId>:" in confirm["x-pisa-signed-op"]["target"]
    assert "x-pisa-signed-op" not in DELTA["paths"]["/v1/pair/offers/{offerId}"]["delete"]  # refusing grants nothing
    assert DELTA["x-pisa-replaced"]["POST /v1/devices/pairing-code"] == "POST /v1/pair/offers"


def test_a_walk_through_the_offer():
    pa, i = V["pairing"], V["pairing"]["inputs"]
    offer = {"offerId": V["offers"]["offerId"], "state": "awaiting_confirmation", "qr": pa["qr"], "token": pa["token"], "expiresAt": "2026-11-18T10:10:00Z",
             "pairingId": "pair-1", "newDevice": {"name": "Tablet", "platform": "ios"},
             "transcript": {"spki": pa["spkiPin"], "deviceKey": i["deviceKey"], "clientNonce": i["clientNonce"],
                            "commitment": pa["commitment"], "serverNonce": i["serverNonce"]}}
    assert jsonschema.Draft202012Validator(schema_of("PairingOffer")).is_valid(offer)
    t = offer["transcript"]
    assert P.reveal_matches(t["commitment"], t["serverNonce"])  # the offering phone checks it too, before showing digits
    phone_sas = P.sas(P.transcript(t["spki"], t["deviceKey"], t["clientNonce"]), t["serverNonce"])
    assert phone_sas == pa["sas"]  # the offering phone and the new device show the same six digits
    confirm = {"pairingId": offer["pairingId"], "sas": phone_sas}
    assert jsonschema.Draft202012Validator(schema_of("PairingConfirm")).is_valid(confirm)
    assert not jsonschema.Draft202012Validator(schema_of("PairingConfirm")).is_valid({"pairingId": "pair-1", "sas": "533 711"})


def test_offers_have_no_signer_and_only_the_offering_device_acts():
    o = V["offers"]
    v = jsonschema.Draft202012Validator(schema_of("PairingOfferRequest"))
    for body in o["valid"]:
        assert v.is_valid(body)
    for bad in o["refused"]:
        assert not v.is_valid(bad["body"]) and bad["status"] == 400 and bad["error"] == "validation_failed"
    assert "signer" not in SCHEMAS["PairingOfferRequest"]["properties"]
    post = DELTA["paths"]["/v1/pair/offers"]["post"]
    assert "400" in post["responses"] and "signer" in post["responses"]["400"]["description"]
    assert "for any other device it is 404" in DELTA["paths"]["/v1/pair/offers/{offerId}"]["get"]["description"]
    assert o["pairedViaOffer"] == {"paired_via": "app", "mayRegisterFirstApprovalKey": False}
    assert jsonschema.Draft202012Validator(schema_of("PairingOffer")).is_valid(
        {"offerId": o["offerId"], "state": "open", "expiresAt": "2026-11-18T10:10:00Z"})
    assert not jsonschema.Draft202012Validator(schema_of("PairingOffer")).is_valid(
        {"offerId": "off-1", "state": "open", "expiresAt": "2026-11-18T10:10:00Z"})


def test_session_nonce_names_the_device_and_pairing_failures_never_retire_a_token():
    nonce = DELTA["paths"]["/v1/devices/session/nonce"]["get"]
    params = {p["name"]: p for p in nonce["parameters"] if "name" in p}
    assert params["deviceId"]["in"] == "query" and params["deviceId"]["required"] is False
    assert "5 outstanding per device" in nonce["description"] and "10 000" in nonce["description"]
    pair = DELTA["paths"]["/v1/pair"]["post"]["description"]
    assert "never retires a token" in pair and "address backstop" in pair and "128 bits" in pair
    assert "per pairing token" not in pair and "5 in 10 minutes" not in pair
