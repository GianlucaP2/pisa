"""The push contract both push paths keep (CONTRACT-2 §9; Apple's APNs documentation): what a push token looks
like, the APNs environments by name, which APNs answers mean a device token is gone, and how long a provider
token (the JWT) is used. Pure: no I/O. What the relay answers the server is `push_seal.RELAY_ANSWERS`.

The relay (`relay/pisa_relay`) is a separate package and keeps its own copy of these rules;
`relay/tests/test_push_contract.py` reads this file and checks the copy against it. Keep the values plain
literals.
"""

# --- tokens -------------------------------------------------------------------------------------------------
# In hex, as the app sends them (each byte of the token's `Data` as "%02x"); any case, stored lowercase.
# An APNs device token is 32 bytes today and Apple says it may grow: 32 to 100 bytes. A Live Activity token
# (a running activity's update token, or the push-to-start token) is longer: 32 to 256 bytes. The relay has
# always taken these ranges; the server took shorter ones, which the relay would have refused.
DEVICE_TOKEN_HEX = (64, 200)
ACTIVITY_TOKEN_HEX = (64, 512)


def hex_token(token: str, limits: tuple[int, int]) -> str | None:
    """`token` trimmed and lowercased, or None when it is not hex with a length within `limits`."""
    token = token.strip().lower()
    low, high = limits
    if not (low <= len(token) <= high) or any(c not in "0123456789abcdef" for c in token):
        return None
    return token


# --- environments -------------------------------------------------------------------------------------------
# The wire name (CONTRACT-2 `PushDirect.environment`, the relay's `POST /v1/register`) → the name the server
# stores on the device (`devices.apns_environment`, Xcode's `aps-environment`).
APNS_ENVIRONMENTS = {"production": "production", "sandbox": "development"}
# The APNs host for each wire name.
APNS_HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}

# --- APNs answers -------------------------------------------------------------------------------------------
# The answers that mean the device token is gone, by status: 410 with `Unregistered` (no longer active for the
# topic) or `ExpiredToken`, and 400 `BadDeviceToken` (not a token for this environment). The sender drops
# it; any other answer is a failure, logged, and the token is kept.
APNS_TOKEN_GONE = {410: ("Unregistered", "ExpiredToken"), 400: ("BadDeviceToken",)}


def apns_token_gone(status: int, reason: str) -> bool:
    return reason in APNS_TOKEN_GONE.get(status, ())


# --- the provider token -------------------------------------------------------------------------------------
# Apple refuses a provider token older than an hour and one refreshed more often than every 20 minutes:
# each sender makes a new one after 50 minutes.
APNS_JWT_RENEW_S = 3000
