from __future__ import annotations

import json

import pytest

import realtime as rt


# ─── symbols and urls ─────────────────────────────────
def test_binance_native_lowercases_and_strips_separator():
    assert rt.binance_native("BTC/USDT") == "btcusdt"
    assert rt.binance_native("eth/usdt") == "ethusdt"


def test_split_native_uses_known_quotes():
    assert rt.split_native("BTCUSDT") == "BTC/USDT"
    assert rt.split_native("ETHUSDC") == "ETH/USDC"
    assert rt.split_native("SOLFDUSD") == "SOL/FDUSD"
    assert rt.split_native("WEIRD") == "WEIRD"


def test_stream_url_defaults_to_testnet():
    url = rt.stream_url(["BTC/USDT", "ETH/USDT"], testnet=True)
    assert url.startswith("wss://stream.testnet.binance.vision/stream?streams=")
    assert "btcusdt@bookTicker" in url
    assert "ethusdt@bookTicker" in url


def test_stream_url_live_host():
    assert rt.stream_url(["BTC/USDT"], testnet=False).startswith(
        "wss://stream.binance.com:9443/stream?streams="
    )


# ─── binance parsing ──────────────────────────────────
def test_parse_raw_book_ticker():
    payload = {
        "u": 103785698130,
        "s": "BTCUSDT",
        "b": "60604.77000000",
        "B": "9.95426000",
        "a": "60604.78000000",
        "A": "0.94408000",
    }
    event = rt.parse_book_ticker(payload)
    assert event == {
        "source": "binance",
        "type": "book_ticker",
        "symbol": "BTC/USDT",
        "native": "BTCUSDT",
        "bid": 60604.77,
        "ask": 60604.78,
        "bid_qty": 9.95426,
        "ask_qty": 0.94408,
        "exchange_ts": 103785698130,
    }


def test_parse_combined_stream_wrapper():
    payload = {"stream": "btcusdt@bookTicker", "data": {"s": "BTCUSDT", "b": "1", "a": "2"}}
    event = rt.parse_book_ticker(payload)
    assert event["symbol"] == "BTC/USDT"
    assert event["bid"] == 1.0
    assert event["bid_qty"] == 0.0


def test_parse_book_ticker_from_string():
    raw = json.dumps({"s": "ETHUSDT", "b": "1700", "a": "1700.5"})
    assert rt.parse_book_ticker(raw)["symbol"] == "ETH/USDT"


def test_parse_book_ticker_rejects_noise():
    assert rt.parse_book_ticker({"e": "ping"}) is None
    assert rt.parse_book_ticker("no json") is None
    assert rt.parse_book_ticker({"s": "BTCUSDT", "b": "x", "a": "2"}) is None


# ─── ai-trader parsing ────────────────────────────────
def test_parse_ai_trader_message():
    raw = json.dumps({"type": "reply", "data": {"symbol": "BTC", "content": "hola"}})
    event = rt.parse_ai_trader(raw)
    assert event["source"] == "ai-trader"
    assert event["type"] == "reply"
    assert event["symbol"] == "BTC"
    assert event["payload"]["data"]["content"] == "hola"


def test_parse_ai_trader_redacts_tokens():
    raw = json.dumps({"type": "auth", "token": "super-secret", "data": {}})
    event = rt.parse_ai_trader(raw)
    assert event["payload"]["token"] == "<redacted>"


def test_parse_ai_trader_tolerates_plain_text():
    event = rt.parse_ai_trader("hola")
    assert event["type"] == "text"
    assert event["payload"] == "hola"


def test_ai_trader_url_includes_token_and_agent(monkeypatch):
    values = {"CLIPTAP_AI_TOKEN": "abc", "CLIPTAP_AGENT_ID": "42"}
    monkeypatch.setattr(rt, "env", lambda name, default="": values.get(name, default))
    url = rt.ai_trader_url()
    assert url == "wss://ai4trade.ai/ws/notify/42?token=abc"


def test_ai_trader_url_requires_token(monkeypatch):
    monkeypatch.setattr(rt, "env", lambda name, default="": default)
    with pytest.raises(SystemExit):
        rt.ai_trader_url()
