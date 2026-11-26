from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import retention as r

NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)


def snapshot(price: float, hour: int = 10, minute: int = 0, day: str = "2026-11-01") -> dict:
    return {
        "collected_at": f"{day}T{hour:02d}:{minute:02d}:00+00:00",
        "prices": {"BTC/crypto": {"symbol": "BTC", "market": "crypto", "price": price}},
    }


def write_snapshot(root: Path, day: str, name: str, payload: dict) -> Path:
    directory = root / day
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_recent_days_are_kept(tmp_path):
    write_snapshot(tmp_path, "2026-11-25", "snapshot_100000.json", snapshot(100, day="2026-11-25"))
    plan = r.plan_retention(tmp_path, now=NOW, raw_days=7)
    assert plan.actions == []
    assert plan.kept_days == ["2026-11-25"]


def test_old_snapshots_are_grouped_by_hour(tmp_path):
    write_snapshot(tmp_path, "2026-11-01", "snapshot_100000.json", snapshot(100))
    write_snapshot(tmp_path, "2026-11-01", "snapshot_100500.json", snapshot(110, minute=5))
    write_snapshot(tmp_path, "2026-11-01", "snapshot_110000.json", snapshot(105, hour=11))

    plan = r.plan_retention(tmp_path, now=NOW, raw_days=7)
    hourly = plan.hourly
    assert len(hourly) == 2
    assert hourly[0].sources == 2
    assert hourly[0].output.name == "2026-11-01T10.json"
    assert hourly[1].sources == 1


def test_apply_builds_ohlc_and_removes_raw(tmp_path):
    write_snapshot(tmp_path, "2026-11-01", "snapshot_100000.json", snapshot(100))
    write_snapshot(tmp_path, "2026-11-01", "snapshot_100500.json", snapshot(95, minute=5))
    write_snapshot(tmp_path, "2026-11-01", "snapshot_101000.json", snapshot(108, minute=10))

    plan = r.plan_retention(tmp_path, now=NOW, raw_days=7)
    result = r.apply_plan(plan)

    assert result == {"written": 1, "removed": 3}
    document = json.loads((tmp_path / "hourly" / "2026-11-01T10.json").read_text())
    btc = document["symbols"]["BTC/crypto"]
    assert btc == {"open": 100, "high": 108, "low": 95, "close": 108, "samples": 3}
    assert document["sources"] == 3
    assert not any((tmp_path / "2026-11-01").glob("snapshot_*.json"))


def test_old_hourly_becomes_daily(tmp_path):
    hourly_dir = tmp_path / "hourly"
    hourly_dir.mkdir()
    for hour in (10, 11):
        payload = {
            "period": f"2026-01-01T{hour:02d}:00:00+00:00",
            "kind": "hourly",
            "sources": 12,
            "symbols": {
                "BTC/crypto": {
                    "open": 100 + hour,
                    "high": 105 + hour,
                    "low": 99 + hour,
                    "close": 103 + hour,
                    "samples": 12,
                }
            },
        }
        (hourly_dir / f"2026-01-01T{hour:02d}.json").write_text(json.dumps(payload), encoding="utf-8")

    plan = r.plan_retention(tmp_path, now=NOW, raw_days=7, hourly_days=90)
    assert len(plan.daily) == 1
    r.apply_plan(plan)

    document = json.loads((tmp_path / "daily" / "2026-01-01.json").read_text())
    btc = document["symbols"]["BTC/crypto"]
    assert btc["open"] == 110
    assert btc["close"] == 114
    assert btc["high"] == 116
    assert btc["low"] == 109
    assert document["sources"] == 24
    assert list(hourly_dir.glob("*.json")) == []


def test_daily_merge_keeps_earliest_open(tmp_path):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-01-01.json").write_text(
        json.dumps(
            {
                "period": "2026-01-01T00:00:00+00:00",
                "kind": "daily",
                "sources": 12,
                "symbols": {"BTC/crypto": {"open": 90, "high": 100, "low": 88, "close": 98, "samples": 12}},
            }
        ),
        encoding="utf-8",
    )
    hourly_dir = tmp_path / "hourly"
    hourly_dir.mkdir()
    (hourly_dir / "2026-01-01T23.json").write_text(
        json.dumps(
            {
                "period": "2026-01-01T23:00:00+00:00",
                "kind": "hourly",
                "sources": 12,
                "symbols": {"BTC/crypto": {"open": 98, "high": 120, "low": 97, "close": 119, "samples": 12}},
            }
        ),
        encoding="utf-8",
    )

    plan = r.plan_retention(tmp_path, now=NOW, raw_days=7, hourly_days=90)
    r.apply_plan(plan)

    btc = json.loads((daily_dir / "2026-01-01.json").read_text())["symbols"]["BTC/crypto"]
    assert btc == {"open": 90, "high": 120, "low": 88, "close": 119, "samples": 24}


def test_hourly_summary_keeps_signal_counts(tmp_path):
    first = snapshot(100)
    first["signal_feed"] = {
        "signals": [
            {"symbol": "BTC", "market": "crypto", "side": "buy"},
            {"symbol": "BTC", "market": "crypto", "side": "short"},
        ]
    }
    second = snapshot(101, minute=5)
    second["signal_feed"] = {"signals": [{"symbol": "BTC", "market": "crypto", "side": "buy"}]}
    write_snapshot(tmp_path, "2026-11-01", "snapshot_100000.json", first)
    write_snapshot(tmp_path, "2026-11-01", "snapshot_100500.json", second)

    r.apply_plan(r.plan_retention(tmp_path, now=NOW, raw_days=7))
    document = json.loads((tmp_path / "hourly" / "2026-11-01T10.json").read_text())
    assert document["signals"]["BTC/crypto"] == {"buy": 2, "sell": 1}


def test_daily_merge_adds_signal_counts(tmp_path):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-01-01.json").write_text(
        json.dumps(
            {
                "period": "2026-01-01T00:00:00+00:00",
                "kind": "daily",
                "sources": 12,
                "symbols": {},
                "signals": {"BTC/crypto": {"buy": 2, "sell": 1}},
            }
        ),
        encoding="utf-8",
    )
    hourly_dir = tmp_path / "hourly"
    hourly_dir.mkdir()
    (hourly_dir / "2026-01-01T23.json").write_text(
        json.dumps(
            {
                "period": "2026-01-01T23:00:00+00:00",
                "kind": "hourly",
                "sources": 12,
                "symbols": {},
                "signals": {"BTC/crypto": {"buy": 3, "sell": 0}},
            }
        ),
        encoding="utf-8",
    )

    r.apply_plan(r.plan_retention(tmp_path, now=NOW, raw_days=7, hourly_days=90))
    document = json.loads((daily_dir / "2026-01-01.json").read_text())
    assert document["signals"]["BTC/crypto"] == {"buy": 5, "sell": 1}


def test_planning_does_not_touch_files(tmp_path):
    path = write_snapshot(tmp_path, "2026-11-01", "snapshot_100000.json", snapshot(100))
    r.plan_retention(tmp_path, now=NOW, raw_days=7)
    assert path.exists()


def test_apply_is_idempotent(tmp_path):
    write_snapshot(tmp_path, "2026-11-01", "snapshot_100000.json", snapshot(100))
    plan = r.plan_retention(tmp_path, now=NOW, raw_days=7)
    r.apply_plan(plan)
    assert r.plan_retention(tmp_path, now=NOW, raw_days=7).actions == []


def test_missing_directory_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        r.plan_retention(tmp_path / "nope", now=NOW)


def test_merge_series_without_existing():
    merged = r.merge_series(None, {"open": 10, "high": 12, "low": 9, "close": 11, "samples": 3})
    assert merged == {"open": 10, "high": 12, "low": 9, "close": 11, "samples": 3}
