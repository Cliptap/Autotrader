from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import metrics
import reconcile as r


def plan(**overrides) -> dict:
    data = {
        "id": "p-1",
        "venue": "ccxt",
        "symbol": "BTC/USDT",
        "market": "crypto",
        "side": "long",
        "qty": 0.01,
        "status": "open",
        "closed_at": None,
    }
    data.update(overrides)
    return data


def paper(**overrides) -> dict:
    data = {
        "symbol": "BTC",
        "market": "crypto",
        "quantity": 0.01,
        "source": "self",
        "side": "long",
    }
    data.update(overrides)
    return data


def trade(symbol: str, closed_at: datetime, side: str = "long") -> metrics.Trade:
    return metrics.Trade(
        id="t-1", closed_at=closed_at, side=side, qty=1, entry=100, exit=101, symbol=symbol
    )


def kinds(findings: list[r.Finding]) -> set[str]:
    return {finding.kind for finding in findings}


# ─── plans vs venue ───────────────────────────────────
def test_clean_book_has_no_findings():
    findings = r.reconcile(
        [plan()],
        [paper()],
        {"ccxt": [{"symbol": "BTC", "qty": 0.01}]},
        [],
    )
    assert findings == []


def test_missing_position_at_venue_is_an_error():
    findings = r.reconcile([plan()], [paper()], {"ccxt": [{"symbol": "BTC", "qty": 0.0}]}, [])
    assert kinds(findings) == {"plan_missing_at_venue"}
    assert findings[0].severity == "error"


def test_short_position_is_a_warning():
    findings = r.reconcile([plan()], [paper()], {"ccxt": [{"symbol": "BTC", "qty": 0.005}]}, [])
    assert kinds(findings) == {"plan_qty_short"}
    assert findings[0].severity == "warn"


def test_extra_position_is_informational():
    findings = r.reconcile([plan()], [paper()], {"ccxt": [{"symbol": "BTC", "qty": 0.02}]}, [])
    assert kinds(findings) == {"plan_qty_extra"}
    assert findings[0].severity == "info"


def test_unknown_quantity_when_venue_not_read():
    findings = r.reconcile([plan()], [paper()], {}, [])
    assert kinds(findings) == {"plan_qty_unknown"}


def test_tolerance_absorbs_rounding():
    findings = r.reconcile(
        [plan()], [paper()], {"ccxt": [{"symbol": "BTC", "qty": 0.01004}]}, [], tolerance=0.005
    )
    assert findings == []


def test_closed_plans_are_ignored_for_venue_checks():
    findings = r.reconcile(
        [plan(status="closed", closed_at="2026-11-01T00:00:00+00:00")],
        [],
        {"ccxt": [{"symbol": "BTC", "qty": 0.0}]},
        [trade("BTC/USDT", datetime(2026, 11, 1, tzinfo=timezone.utc))],
    )
    assert findings == []


# ─── venue positions without plan ─────────────────────
def test_unmanaged_alpaca_position_is_flagged():
    findings = r.reconcile(
        [plan(venue="alpaca", symbol="AAPL", market="us-stock", qty=1)],
        [paper(symbol="AAPL", market="us-stock", quantity=1)],
        {
            "alpaca": [
                {"symbol": "AAPL", "qty": 1},
                {"symbol": "NVDA", "qty": 3},
            ]
        },
        [],
    )
    assert kinds(findings) == {"venue_unmanaged"}
    assert "NVDA" in findings[0].subject


def test_ccxt_balances_are_not_treated_as_positions():
    findings = r.reconcile(
        [plan()],
        [paper()],
        {"ccxt": [{"symbol": "BTC", "qty": 0.01}, {"symbol": "ETH", "qty": 5}]},
        [],
    )
    assert findings == []


# ─── paper ────────────────────────────────────────────
def test_self_position_without_plan_is_reported():
    findings = r.reconcile([], [paper(symbol="ETH")], {}, [])
    assert kinds(findings) == {"paper_without_plan"}


def test_copied_positions_do_not_need_plans():
    findings = r.reconcile([], [paper(source="copied:3120")], {}, [])
    assert findings == []


def test_plan_without_paper_position_is_a_warning():
    findings = r.reconcile(
        [plan()],
        [paper(symbol="ETH")],
        {"ccxt": [{"symbol": "BTC", "qty": 0.01}]},
        [],
    )
    assert "plan_without_paper" in kinds(findings)


# ─── journal ──────────────────────────────────────────
def test_closed_plan_without_journal_row_is_reported():
    closed = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    findings = r.reconcile(
        [plan(status="closed", closed_at=closed.isoformat())],
        [],
        {},
        [],
    )
    assert kinds(findings) == {"journal_missing"}


def test_closed_plan_matches_journal_within_window():
    closed = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    findings = r.reconcile(
        [plan(status="closed", closed_at=closed.isoformat())],
        [],
        {},
        [trade("BTC/USDT", closed + timedelta(hours=3))],
    )
    assert findings == []


def test_journal_row_for_another_symbol_does_not_match():
    closed = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    findings = r.reconcile(
        [plan(status="closed", closed_at=closed.isoformat())],
        [],
        {},
        [trade("ETH/USDT", closed)],
    )
    assert kinds(findings) == {"journal_missing"}


# ─── rendering ────────────────────────────────────────
def test_markdown_reports_counts_and_rows():
    findings = [
        r.Finding("plan_missing_at_venue", "error", "ccxt:BTC", "sin posición"),
        r.Finding("paper_without_plan", "info", "crypto:ETH", "sin plan"),
    ]
    text = r.render_markdown(findings, datetime(2026, 11, 26, tzinfo=timezone.utc))
    assert "# Reconciliación" in text
    assert "| 1 | 0 | 1 |" in text
    assert "plan_missing_at_venue" in text


def test_markdown_for_clean_book():
    text = r.render_markdown([], datetime(2026, 11, 26, tzinfo=timezone.utc))
    assert "Sin diferencias" in text


def test_text_summary_counts_severities():
    findings = [
        r.Finding("a", "error", "x", "d"),
        r.Finding("b", "warn", "y", "d"),
        r.Finding("c", "warn", "z", "d"),
    ]
    text = r.render_text(findings)
    assert "1 errores" in text
    assert "2 advertencias" in text
