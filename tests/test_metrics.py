from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import metrics as m


def write_journal(path: Path, rows: list[dict]) -> Path:
    columns = [
        "id",
        "opened_at",
        "closed_at",
        "symbol",
        "market",
        "side",
        "qty",
        "entry",
        "exit",
        "fees",
        "stop",
        "target",
        "risk_pct",
        "thesis",
        "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    return path


def base_row(**overrides) -> dict:
    row = {
        "id": "t-1",
        "opened_at": "2026-11-01T10:00:00+00:00",
        "closed_at": "2026-11-02T10:00:00+00:00",
        "symbol": "BTC/USDT",
        "market": "crypto",
        "side": "long",
        "qty": "1",
        "entry": "100",
        "exit": "110",
        "fees": "0",
        "stop": "95",
        "target": "120",
    }
    row.update(overrides)
    return row


def series(count: int, wins: int, win_pnl: float, loss_pnl: float) -> list[dict]:
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        won = index < wins
        pnl = win_pnl if won else loss_pnl
        entry = 1000.0
        exit_price = entry + pnl
        day = start + timedelta(days=index)
        rows.append(
            base_row(
                id=f"t-{index}",
                opened_at=(day - timedelta(hours=6)).isoformat(),
                closed_at=day.isoformat(),
                qty="1",
                entry=str(entry),
                exit=str(exit_price),
                stop=str(entry - 50),
                fees="0",
            )
        )
    return rows


# ─── journal parsing ──────────────────────────────────
def test_init_creates_an_empty_journal(tmp_path):
    path = tmp_path / "nested" / "trades.csv"
    assert m.cmd_init(argparse.Namespace(file=str(path))) == 0
    assert path.is_file()
    assert m.load_trades(path) == []
    assert m.cmd_init(argparse.Namespace(file=str(path))) == 0


def test_load_trades_sorts_and_parses(tmp_path):
    path = write_journal(
        tmp_path / "trades.csv",
        [
            base_row(id="b", closed_at="2026-11-05T00:00:00Z"),
            base_row(id="a", closed_at="2026-11-01T00:00:00Z"),
        ],
    )
    trades = m.load_trades(path)
    assert [t.id for t in trades] == ["a", "b"]
    assert trades[0].closed_at.tzinfo is not None


def test_missing_columns_is_an_error(tmp_path):
    path = tmp_path / "trades.csv"
    path.write_text("id,closed_at\nx,2026-11-01\n", encoding="utf-8")
    with pytest.raises(ValueError, match="faltan columnas"):
        m.load_trades(path)


def test_non_numeric_value_is_an_error(tmp_path):
    path = write_journal(tmp_path / "trades.csv", [base_row(entry="abc")])
    with pytest.raises(ValueError, match="no es numérico"):
        m.load_trades(path)


def test_missing_journal_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        m.load_trades(tmp_path / "nope.csv")


# ─── trade math ───────────────────────────────────────
def test_long_and_short_pnl_with_fees():
    long = m.Trade(id="1", closed_at=datetime.now(timezone.utc), side="long", qty=2, entry=100, exit=110, fees=1)
    short = m.Trade(id="2", closed_at=datetime.now(timezone.utc), side="short", qty=2, entry=100, exit=90, fees=1)
    assert long.pnl == pytest.approx(19)
    assert short.pnl == pytest.approx(19)
    assert long.return_pct == pytest.approx(9.5)


def test_r_multiple_uses_stop_distance():
    trade = m.Trade(
        id="1", closed_at=datetime.now(timezone.utc), side="long",
        qty=2, entry=100, exit=110, stop=95,
    )
    assert trade.r_multiple == pytest.approx(2.0)


def test_duration_hours():
    opened = datetime(2026, 11, 1, tzinfo=timezone.utc)
    trade = m.Trade(
        id="1", closed_at=opened + timedelta(hours=36), side="long",
        qty=1, entry=100, exit=101, opened_at=opened,
    )
    assert trade.duration_hours == pytest.approx(36)


# ─── aggregate metrics ────────────────────────────────
def test_win_rate_and_profit_factor():
    trades = [
        m.Trade(id="1", closed_at=datetime(2026, 11, 1, tzinfo=timezone.utc), side="long", qty=1, entry=100, exit=120),
        m.Trade(id="2", closed_at=datetime(2026, 11, 2, tzinfo=timezone.utc), side="long", qty=1, entry=100, exit=110),
        m.Trade(id="3", closed_at=datetime(2026, 11, 3, tzinfo=timezone.utc), side="long", qty=1, entry=100, exit=90),
    ]
    metrics = m.compute_metrics(trades, start_equity=1000)
    assert metrics.closed == 3
    assert metrics.win_rate == pytest.approx(66.666, rel=1e-3)
    assert metrics.gross_profit == pytest.approx(30)
    assert metrics.gross_loss == pytest.approx(10)
    assert metrics.profit_factor == pytest.approx(3.0)


def test_profit_factor_is_none_without_losses():
    trades = [m.Trade(id="1", closed_at=datetime.now(timezone.utc), side="long", qty=1, entry=100, exit=110)]
    metrics = m.compute_metrics(trades, start_equity=1000)
    assert metrics.profit_factor is None


def test_max_drawdown_tracks_peak_to_trough():
    trades = [
        m.Trade(id="1", closed_at=datetime(2026, 11, 1, tzinfo=timezone.utc), side="long", qty=1, entry=1000, exit=1200),
        m.Trade(id="2", closed_at=datetime(2026, 11, 2, tzinfo=timezone.utc), side="long", qty=1, entry=1000, exit=800),
    ]
    metrics = m.compute_metrics(trades, start_equity=1000)
    # equity: 1000 -> 1200 -> 1000, peak 1200, drawdown 16.67%
    assert metrics.max_drawdown_pct == pytest.approx(16.666, rel=1e-3)
    assert metrics.end_equity == pytest.approx(1000)


def test_sharpe_needs_two_days():
    trades = [m.Trade(id="1", closed_at=datetime.now(timezone.utc), side="long", qty=1, entry=100, exit=110)]
    assert m.compute_metrics(trades).sharpe is None


# ─── verdicts ─────────────────────────────────────────
def test_verdict_insufficient_sample(tmp_path):
    path = write_journal(tmp_path / "trades.csv", series(10, 8, 100, -50))
    metrics = m.compute_metrics(m.load_trades(path))
    decision, reasons = m.verdict(metrics)
    assert decision == "insufficient_sample"
    assert "10/50" in reasons[0]


def test_verdict_promote_on_strong_sample(tmp_path):
    path = write_journal(tmp_path / "trades.csv", series(60, 55, 100, -50))
    metrics = m.compute_metrics(m.load_trades(path), start_equity=100_000)
    decision, reasons = m.verdict(metrics)
    assert decision == "promote", reasons
    assert metrics.sharpe is not None and metrics.sharpe > 1


def test_verdict_degrade_on_losing_sample(tmp_path):
    path = write_journal(tmp_path / "trades.csv", series(60, 20, 100, -100))
    metrics = m.compute_metrics(m.load_trades(path), start_equity=10_000)
    decision, _ = m.verdict(metrics)
    assert decision == "degrade"


def test_verdict_hold_when_mixed(tmp_path):
    path = write_journal(tmp_path / "trades.csv", series(60, 35, 100, -100))
    metrics = m.compute_metrics(m.load_trades(path), start_equity=100_000)
    decision, _ = m.verdict(metrics)
    # 58% win rate, PF 1.16: not enough to promote, not bad enough to degrade
    assert decision == "hold"


# ─── dashboard ────────────────────────────────────────
def test_dashboard_offline_renders_sections():
    metrics = m.compute_metrics([], start_equity=100_000)
    decision, reasons = m.verdict(metrics)
    text = m.render_dashboard(metrics, decision, reasons, None, [])
    assert "# Dashboard" in text
    assert "## Métricas (journal)" in text
    assert "## Planes de riesgo" in text
    assert "insufficient_sample" in text
    assert "Sin acceso a AI-Trader" in text


def test_dashboard_values_positions_and_equity():
    metrics = m.compute_metrics([], start_equity=100_000)
    decision, reasons = m.verdict(metrics)
    account = {
        "me": {"id": 7, "name": "bot", "points": 10},
        "positions": {
            "cash": 1000.0,
            "positions": [
                {
                    "symbol": "BTC",
                    "market": "crypto",
                    "side": "long",
                    "quantity": 0.1,
                    "entry_price": 100.0,
                    "source": "self",
                }
            ],
        },
    }
    text = m.render_dashboard(
        metrics,
        decision,
        reasons,
        account,
        [{"id": "p-1", "symbol": "BTC/USDT", "side": "long", "qty": 0.1, "stop": 95.0, "status": "open"}],
        prices={("BTC", "crypto"): 110.0},
    )
    assert "| Equity estimada | $1,001.00 |" in text
    assert "| BTC | crypto | long |" in text
    assert "| p-1 | BTC/USDT |" in text


def test_dashboard_reports_missing_prices():
    metrics = m.compute_metrics([], start_equity=100_000)
    account = {
        "me": {"id": 7, "name": "bot", "points": 10},
        "positions": {
            "cash": 500.0,
            "positions": [
                {
                    "symbol": "GRIFFAIN",
                    "market": "crypto",
                    "side": "long",
                    "quantity": 1.0,
                    "entry_price": 0.01,
                    "source": "copied:1",
                }
            ],
        },
    }
    text = m.render_dashboard(metrics, "hold", [], account, [], prices={("GRIFFAIN", "crypto"): None})
    assert "sin precio" in text
    assert "1 posiciones sin precio disponible" in text
