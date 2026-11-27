from __future__ import annotations

import json

import events


def test_append_and_read_roundtrip(tmp_path):
    path = tmp_path / "events.jsonl"
    events.append_event("binance", "book_ticker", path=path, symbol="BTC/USDT", bid=100.0)
    events.append_event("ai-trader", "message", path=path, symbol="ETH")
    stored = events.read_events(path, limit=10)
    assert [event["source"] for event in stored] == ["binance", "ai-trader"]
    assert stored[0]["symbol"] == "BTC/USDT"
    assert stored[0]["ts"].endswith("+00:00")


def test_read_returns_last_n(tmp_path):
    path = tmp_path / "events.jsonl"
    for index in range(5):
        events.append_event("test", "tick", path=path, n=index)
    stored = events.read_events(path, limit=2)
    assert [event["n"] for event in stored] == [3, 4]


def test_read_missing_file_is_empty(tmp_path):
    assert events.read_events(tmp_path / "nope.jsonl") == []


def test_broken_lines_are_skipped(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"source": "a"}\nno es json\n{"source": "b"}\n', encoding="utf-8")
    stored = events.read_events(path)
    assert [event["source"] for event in stored] == ["a", "b"]


def test_rotation_keeps_one_previous_file(tmp_path):
    path = tmp_path / "events.jsonl"
    events.append_event("a", "tick", path=path, payload="x" * 100, max_bytes=50)
    events.append_event("b", "tick", path=path, payload="y" * 100, max_bytes=50)
    rotated = path.with_suffix(".jsonl.1")
    assert rotated.is_file()
    assert json.loads(rotated.read_text(encoding="utf-8").strip())["source"] == "a"
    assert [event["source"] for event in events.read_events(path)] == ["b"]


def test_stats_reports_counts_and_last(tmp_path):
    path = tmp_path / "events.jsonl"
    events.append_event("binance", "book_ticker", path=path, symbol="BTC/USDT")
    events.append_event("binance", "book_ticker", path=path, symbol="ETH/USDT")
    stats = events.file_stats(path)
    assert stats["exists"] is True
    assert stats["events"] == 2
    assert stats["last"]["symbol"] == "ETH/USDT"


def test_stats_without_file(tmp_path):
    stats = events.file_stats(tmp_path / "nope.jsonl")
    assert stats == {"exists": False, "events": 0, "bytes": 0, "last": None}
