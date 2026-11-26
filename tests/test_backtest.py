from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import backtest as bt

START = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def at(hours: float) -> str:
    return (START + timedelta(hours=hours)).isoformat()


def snapshot(hours: float, prices: dict[str, float], signals: list[dict] | None = None, macro: int = 0) -> dict:
    return {
        "collected_at": at(hours),
        "prices": {
            f"{symbol}/crypto": {"symbol": symbol, "market": "crypto", "price": price}
            for symbol, price in prices.items()
        },
        "signal_feed": {"signals": signals or []},
        "macro_signals": {"bullish_count": macro},
    }


def signal(signal_id: int, agent: int, symbol: str = "BTC", market: str = "crypto") -> dict:
    return {
        "id": signal_id,
        "agent_id": agent,
        "symbol": symbol,
        "market": market,
        "side": "buy",
    }


def params(**overrides) -> bt.Params:
    base = bt.Params(consensus=2, stop_pct=2.0, take_pct=4.0, max_hold_hours=72.0)
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# ─── entries ──────────────────────────────────────────
def test_no_signals_no_trades():
    result = bt.run_backtest([snapshot(0, {"BTC": 100})], params())
    assert result.metrics.closed == 0


def test_single_agent_does_not_trigger_consensus():
    result = bt.run_backtest([snapshot(0, {"BTC": 100}, [signal(1, 10)])], params())
    assert result.metrics.closed == 0


def test_two_agents_open_one_position():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10), signal(2, 11)]),
        snapshot(1, {"BTC": 100}),
    ]
    result = bt.run_backtest(snaps, params(max_hold_hours=1))
    assert len(result.trades) == 1
    assert result.trades[0].symbol == "BTC"


def test_duplicate_signal_ids_are_counted_once():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10)]),
        snapshot(1, {"BTC": 100}, [signal(1, 10)]),
        snapshot(2, {"BTC": 100}, [signal(2, 11)]),
    ]
    result = bt.run_backtest(snaps, params())
    # At t=2 both signals are visible: one entry, not two.
    assert len(result.trades) == 1


# ─── exits ────────────────────────────────────────────
def test_target_exit_is_profitable():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10), signal(2, 11)]),
        snapshot(1, {"BTC": 106}),
    ]
    result = bt.run_backtest(snaps, params(stop_pct=2, take_pct=4))
    trade = result.trades[0]
    assert "target" in trade.notes
    assert trade.pnl > 0
    assert trade.exit == pytest.approx(100 * 1.0005 * 1.04 * 0.9995, rel=1e-4)


def test_stop_exit_is_a_loss_and_fills_at_stop():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10), signal(2, 11)]),
        snapshot(1, {"BTC": 95}),
    ]
    result = bt.run_backtest(snaps, params(stop_pct=2, take_pct=4))
    trade = result.trades[0]
    assert "stop" in trade.notes
    assert trade.pnl < 0
    stop_price = 100 * 1.0005 * 0.98
    assert trade.exit == pytest.approx(stop_price * 0.9995, rel=1e-4)


def test_time_exit_when_nothing_is_hit():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10), signal(2, 11)]),
        snapshot(1, {"BTC": 100}),
        snapshot(3, {"BTC": 100.5}),
    ]
    result = bt.run_backtest(snaps, params(max_hold_hours=2))
    trade = result.trades[0]
    assert "time" in trade.notes


def test_open_position_is_closed_at_the_end():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10), signal(2, 11)]),
        snapshot(1, {"BTC": 100.2}),
    ]
    result = bt.run_backtest(snaps, params(max_hold_hours=999, stop_pct=50, take_pct=50))
    assert len(result.trades) == 1
    assert "open_at_end" in result.trades[0].notes
    assert any("al final del rango" in warning for warning in bt.warnings_for(result))


def test_short_range_is_flagged_as_insufficient():
    result = bt.run_backtest([snapshot(0, {"BTC": 100})], params())
    assert any("insuficiente" in warning for warning in bt.warnings_for(result))


# ─── filters ──────────────────────────────────────────
def test_macro_filter_blocks_entries():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10), signal(2, 11)], macro=1),
        snapshot(1, {"BTC": 100}, macro=1),
    ]
    result = bt.run_backtest(snaps, params(macro_min=3))
    assert result.metrics.closed == 0


def test_macro_filter_allows_when_bullish():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10), signal(2, 11)], macro=4),
        snapshot(1, {"BTC": 100}),
    ]
    result = bt.run_backtest(snaps, params(macro_min=3, max_hold_hours=1))
    assert result.metrics.closed == 1


def test_signals_outside_the_window_do_not_count():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10)]),
        snapshot(8, {"BTC": 100}, [signal(2, 11)]),
        snapshot(9, {"BTC": 100}),
    ]
    result = bt.run_backtest(snaps, params(window_hours=6))
    assert result.metrics.closed == 0


def test_symbol_without_price_is_skipped():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10, symbol="SOL"), signal(2, 11, symbol="SOL")]),
        snapshot(1, {"BTC": 100}),
    ]
    result = bt.run_backtest(snaps, params())
    assert result.metrics.closed == 0


# ─── exit before re-entry ─────────────────────────────
def test_cooldown_blocks_immediate_reentry_after_stop():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10), signal(2, 11)]),
        snapshot(1, {"BTC": 90}, [signal(3, 12)]),
        snapshot(2, {"BTC": 90}),
    ]
    result = bt.run_backtest(snaps, params(stop_pct=2, take_pct=4, max_hold_hours=1))
    assert len(result.trades) == 1
    assert "stop" in result.trades[0].notes


def test_reentry_is_allowed_once_the_cooldown_expires():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10), signal(2, 11)]),
        snapshot(1, {"BTC": 90}, [signal(3, 12)]),
        snapshot(2, {"BTC": 90}),
    ]
    result = bt.run_backtest(
        snaps,
        params(stop_pct=2, take_pct=4, max_hold_hours=999, reentry_cooldown_hours=0),
    )
    assert len(result.trades) == 2
    assert "stop" in result.trades[0].notes
    assert "open_at_end" in result.trades[1].notes
    assert result.trades[0].closed_at <= result.trades[1].opened_at


# ─── determinism and sweep ────────────────────────────
def test_backtest_is_deterministic():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10), signal(2, 11)]),
        snapshot(1, {"BTC": 106}),
        snapshot(2, {"BTC": 101}, [signal(3, 12)]),
        snapshot(3, {"BTC": 99}),
    ]
    first = bt.run_backtest(snaps, params())
    second = bt.run_backtest(snaps, params())
    assert [t.pnl for t in first.trades] == [t.pnl for t in second.trades] == pytest.approx(
        [t.pnl for t in first.trades]
    )


def test_sweep_covers_the_grid():
    snaps = [
        snapshot(0, {"BTC": 100}, [signal(1, 10)]),
        snapshot(1, {"BTC": 100}, [signal(2, 11)]),
        snapshot(2, {"BTC": 100}),
    ]
    rows = bt.sweep_rows(snaps, params())
    assert len(rows) == 6
    assert {(row["consensus"], row["macro_min"]) for row in rows} == {
        (1, 0), (1, 3), (2, 0), (2, 3), (3, 0), (3, 3)
    }
    # Consensus 2 gets in; consensus 3 does not.
    assert next(row for row in rows if row["consensus"] == 2 and row["macro_min"] == 0)["trades"] >= 1
    assert next(row for row in rows if row["consensus"] == 3 and row["macro_min"] == 0)["trades"] == 0


# ─── loading ──────────────────────────────────────────
def test_load_snapshots_sorts_and_skips_broken_files(tmp_path: Path):
    day = tmp_path / "2026-11-01"
    day.mkdir()
    (day / "snapshot_120000.json").write_text(json.dumps(snapshot(1, {"BTC": 101})), encoding="utf-8")
    (day / "snapshot_110000.json").write_text(json.dumps(snapshot(0, {"BTC": 100})), encoding="utf-8")
    (day / "snapshot_130000.json").write_text("{not json", encoding="utf-8")

    loaded = bt.load_snapshots(tmp_path)
    assert len(loaded) == 2
    assert loaded[0]["collected_at"] == at(0)
    assert loaded[1]["collected_at"] == at(1)


def test_missing_directory_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        bt.load_snapshots(tmp_path / "nope")
