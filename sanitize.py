"""Secret redaction shared by the collector and any script that persists API data.

An API response can echo credentials (the AI-Trader agent endpoint returns the
token). Anything written to disk goes through here first.
"""

from __future__ import annotations

from typing import Any

SENSITIVE_KEYS = {"token", "api_key", "apikey", "secret", "password", "authorization"}

REDACTED = "<redacted>"


def scrub_secrets(value: Any) -> Any:
    """Return ``value`` with every sensitive key redacted, recursively."""
    if isinstance(value, dict):
        return {
            key: REDACTED if str(key).lower() in SENSITIVE_KEYS else scrub_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_secrets(item) for item in value)
    return value
