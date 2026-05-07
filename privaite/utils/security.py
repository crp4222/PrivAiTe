from __future__ import annotations

import hmac
import os


def get_api_keys() -> list[str]:
    keys_raw = os.environ.get("PRIVAITE_API_KEYS", "")
    if not keys_raw:
        return []
    return [k.strip() for k in keys_raw.split(",") if k.strip()]


def verify_api_key(provided: str, allowed_keys: list[str]) -> bool:
    for key in allowed_keys:
        if hmac.compare_digest(provided.encode(), key.encode()):
            return True
    return False
