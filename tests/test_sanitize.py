from __future__ import annotations

from sanitize import REDACTED, scrub_secrets


def test_redacts_token_at_top_level():
    cleaned = scrub_secrets({"id": 1, "token": "secret-value", "name": "bot"})
    assert cleaned == {"id": 1, "token": REDACTED, "name": "bot"}


def test_redacts_nested_keys_case_insensitively():
    cleaned = scrub_secrets({"agent": {"API_KEY": "x", "password": "y", "cash": 10}})
    assert cleaned["agent"] == {"API_KEY": REDACTED, "password": REDACTED, "cash": 10}


def test_redacts_inside_lists_and_tuples():
    cleaned = scrub_secrets([{"secret": "a"}, ({"authorization": "b"},)])
    assert cleaned[0]["secret"] == REDACTED
    assert cleaned[1][0]["authorization"] == REDACTED


def test_leaves_non_sensitive_payload_intact():
    payload = {"positions": [{"symbol": "BTC", "qty": 0.1}], "cash": 100}
    assert scrub_secrets(payload) == payload


def test_scalar_passthrough():
    assert scrub_secrets("text") == "text"
    assert scrub_secrets(42) == 42
    assert scrub_secrets(None) is None
