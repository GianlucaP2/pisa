# SPDX-License-Identifier: AGPL-3.0-only
"""The relay's settings, from `RELAY_*` environment variables. Nothing real lives in the repository."""

import base64
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RELAY_")

    database: str = "relay.sqlite3"
    storage_key: SecretStr  # 32 random bytes, base64url: seals the HMAC keys, keys the subscription refs
    bundle_id: str  # the app's bundle id: the APNs topic, and what a StoreKit transaction must name
    product_ids: str  # the subscription's product ids, comma-separated
    storekit_environments: str = "Production"  # "Production,Sandbox" while testing with TestFlight
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_key_path: Path | None = None  # the vendor's .p8 (PKCS#8 PEM), e.g. a mounted secret
    push_per_minute: int = 60
    push_per_day: int = 2000
    handles_per_subscription: int = 10  # at this, the least recently used idle handle makes room
    evict_idle_days: int = 30  # a handle used (or registered) within this many days is never evicted
    register_per_subscription_hour: int = 5
    transaction_max_age_s: int = 7 * 86_400  # a registration's signed transaction must be this recent
    expiry_grace_s: int = 86_400  # pushes still pass this long after the subscription's expiry
    metrics_token: SecretStr | None = None  # /metrics answers only with this bearer; off when unset
    live_activity_attributes_type: str = "NextStopAttributes"  # the app's ActivityAttributes type

    @field_validator("storage_key")
    @classmethod
    def _key_is_32_bytes(cls, value: SecretStr) -> SecretStr:
        try:
            size = len(b64url_decode(value.get_secret_value()))
        except ValueError:
            size = 0
        if size != 32:
            raise ValueError("RELAY_STORAGE_KEY must be 32 random bytes, base64url")
        return value

    def storage_key_bytes(self) -> bytes:
        return b64url_decode(self.storage_key.get_secret_value())

    def products(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.product_ids.split(",") if p.strip())

    def environments(self) -> frozenset[str]:
        return frozenset(e.strip() for e in self.storekit_environments.split(",") if e.strip())
