from __future__ import annotations

import json
from dataclasses import asdict
from datetime import timedelta

import pytest

import risk_engine as re


def make_plan(**overrides) -> re.Plan:
    values = dict(
        id="p-test-1",
        venue="ccxt",
        symbol="BTC/USDT",
        market="crypto",
        side="long",
        qty=0.01,
        entry=100.0,
        stop=95.0,
        target=112.0,
    )
    values.update(overrides)
    return re.Plan(**values)


class FakeVenue:
    """Minimal venue double: no network, deterministic answers."""

    name = "ccxt"

    def __init__(self, price: float, qty: float | None = None, status: dict | None = None):
        self.price_value = price
        self.qty = qty
        self.status = status
        self.closes: list[tuple[str, str]] = []
        self.status_calls = 0

    @property
    def sandbox(self) -> bool:  # keep the double "live" so gates are exercised
        return False

    def price(self, symbol: str) -> float:
        return self.price_value

    def position_qty(self, symbol: str) -> float | None:
        return self.qty

    def close(self, plan: re.Plan, client_id: str) -> dict:
        self.closes.append((plan.symbol, client_id))
        return {"id": "order-1", "status": "new", "price": None}

    def order_status(self, plan: re.Plan, client_id: str) -> dict | None:
        self.status_calls += 1
        return self.status


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(re, "STATE_DIR", tmp_path)
    monkeypatch.setattr(re, "PLANS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(re, "LOG_FILE", tmp_path / "risk-log.jsonl")
    monkeypatch.setattr(re, "HALT_FILE", tmp_path / "HALT")
    return tmp_path


# ─── validation ───────────────────────────────────────
def test_plan_without_stop_is_rejected():
    with pytest.raises(ValueError):
        re.validate_plan(make_plan(stop=0))


def test_long_stop_must_be_below_entry():
    with pytest.raises(ValueError):
        re.validate_plan(make_plan(side="long", stop=101))


def test_short_stop_must_be_above_entry():
    with pytest.raises(ValueError):
        re.validate_plan(make_plan(side="short", stop=99, target=90))


def test_target_must_be_on_the_profitable_side():
    with pytest.raises(ValueError):
        re.validate_plan(make_plan(side="long", target=90))


def test_valid_plan_passes():
    re.validate_plan(make_plan())


def test_risk_usd_uses_stop_distance():
    assert re.risk_usd(make_plan(qty=2, entry=100, stop=95)) == pytest.approx(10)


# ─── pure helpers ─────────────────────────────────────
def test_stop_breach_long_and_short():
    assert re.stop_breached(make_plan(side="long", stop=95), 94.9)
    assert not re.stop_breached(make_plan(side="long", stop=95), 95.1)
    assert re.stop_breached(make_plan(side="short", stop=105), 105.1)
    assert not re.stop_breached(make_plan(side="short", stop=105), 104.9)


def test_target_reached():
    assert re.target_reached(make_plan(side="long", target=112), 112.5)
    assert not re.target_reached(make_plan(side="long", target=112), 111)
    assert re.target_reached(make_plan(side="short", entry=100, stop=105, target=90), 89)


def test_close_side_inverts_position():
    assert re.close_side(make_plan(side="long")) == "sell"
    assert re.close_side(make_plan(side="short")) == "buy"


def test_client_order_id_is_stable_and_short():
    first = re.close_client_id(make_plan())
    second = re.close_client_id(make_plan())
    assert first == second
    assert first.startswith("rk-")
    assert len(first) <= 36


# ─── store ────────────────────────────────────────────
def test_plan_store_roundtrip():
    plans = [make_plan(), make_plan(id="p-test-2", symbol="ETH/USDT")]
    re.save_plans(plans)
    loaded = re.load_plans()
    assert [p.id for p in loaded] == ["p-test-1", "p-test-2"]
    assert json.loads(re.PLANS_FILE.read_text())["positions"][0]["stop"] == 95.0


# ─── engine ───────────────────────────────────────────
def test_stop_sends_one_close_and_marks_closing():
    plan = make_plan()
    venue = FakeVenue(price=94.0)
    outcome = re.process_plan(plan, venue, allow_live=True)
    assert outcome == "sent"
    assert plan.status == "closing"
    assert len(venue.closes) == 1
    assert venue.closes[0][0] == "BTC/USDT"


def test_restart_does_not_send_a_second_close():
    plan = make_plan()
    venue = FakeVenue(price=94.0)
    re.process_plan(plan, venue, allow_live=True)

    # Simulate a restart: rebuild the plan from disk, fresh venue instance.
    restarted = re.Plan(**asdict(plan))
    second_venue = FakeVenue(price=94.0, status=None)
    outcome = re.process_plan(restarted, second_venue, allow_live=True)

    assert outcome == "wait"
    assert second_venue.closes == []


def test_open_close_order_waits():
    plan = make_plan(status="closing", close_client_id="rk-x", close_sent_at=re.iso())
    venue = FakeVenue(price=90.0, status={"id": "order-1", "status": "new"})
    assert re.process_plan(plan, venue, allow_live=True) == "wait"
    assert venue.closes == []


def test_filled_close_finalizes_the_plan():
    plan = make_plan(
        status="closing",
        close_client_id="rk-x",
        close_sent_at=re.iso(),
        last_error="blocked: dry-run",
    )
    venue = FakeVenue(price=90.0, status={"id": "order-1", "status": "filled", "price": 93.8})
    assert re.process_plan(plan, venue, allow_live=True) == "closed"
    assert plan.status == "closed"
    assert plan.exit_price == 93.8
    assert plan.last_error is None


def test_missing_close_order_is_retried_after_grace():
    old = (re.utcnow() - timedelta(seconds=120)).isoformat()
    plan = make_plan(status="closing", close_client_id="rk-x", close_sent_at=old)
    venue = FakeVenue(price=90.0, status=None)
    assert re.process_plan(plan, venue, allow_live=True) == "retry"
    assert plan.status == "open"
    assert plan.attempts == 1
    assert venue.closes == []


def test_missing_close_order_fails_after_three_attempts():
    old = (re.utcnow() - timedelta(seconds=120)).isoformat()
    plan = make_plan(status="closing", close_client_id="rk-x", close_sent_at=old, attempts=2)
    venue = FakeVenue(price=90.0, status=None)
    assert re.process_plan(plan, venue, allow_live=True) == "failed"
    assert plan.status == "failed"
    assert venue.closes == []


def test_live_venue_is_blocked_without_arming():
    plan = make_plan()
    venue = FakeVenue(price=94.0)
    assert re.process_plan(plan, venue, allow_live=False) == "blocked"
    assert venue.closes == []
    assert plan.status == "open"


def test_dry_run_never_sends():
    plan = make_plan()
    venue = FakeVenue(price=94.0)
    assert re.process_plan(plan, venue, allow_live=True, dry_run=True) == "blocked"
    assert venue.closes == []


def test_orphan_plan_is_not_closed():
    plan = make_plan()
    venue = FakeVenue(price=94.0, qty=0.0)
    assert re.process_plan(plan, venue, allow_live=True) == "orphan"
    assert plan.status == "orphan"
    assert venue.closes == []


def test_target_closes_only_when_enabled():
    plan = make_plan()
    venue = FakeVenue(price=120.0)
    assert re.process_plan(plan, venue, allow_live=True, close_on_target=False) == "ok"
    assert venue.closes == []

    plan = make_plan()
    venue = FakeVenue(price=120.0)
    assert re.process_plan(plan, venue, allow_live=True, close_on_target=True) == "sent"
    assert len(venue.closes) == 1


def test_price_error_keeps_the_plan_alive():
    class BrokenVenue(FakeVenue):
        def price(self, symbol: str) -> float:
            raise RuntimeError("network down")

    plan = make_plan()
    venue = BrokenVenue(price=0)
    assert re.process_plan(plan, venue, allow_live=True) == "price_error"
    assert plan.status == "open"
    assert "network down" in (plan.last_error or "")


def test_blocked_is_logged_once_per_window():
    plan = make_plan()
    venue = FakeVenue(price=94.0)
    re.process_plan(plan, venue, allow_live=False)
    first = plan.blocked_logged_at
    re.process_plan(plan, venue, allow_live=False)
    assert plan.blocked_logged_at == first


def test_permanent_error_fails_the_plan_without_retry():
    class RejectingVenue(FakeVenue):
        def close(self, plan: re.Plan, client_id: str) -> dict:
            raise re.PermanentVenueError("cantidad por debajo del mínimo del exchange")

    plan = make_plan()
    venue = RejectingVenue(price=94.0)
    assert re.process_plan(plan, venue, allow_live=True) == "failed"
    assert plan.status == "failed"
    assert "mínimo" in (plan.last_error or "")
    assert venue.closes == []


def test_run_cycle_persists_outcomes():
    re.save_plans([make_plan()])
    counts = re.run_cycle(
        allow_live=True,
        dry_run=False,
        close_on_target=True,
        verbose=False,
        venues={"ccxt": FakeVenue(price=94.0)},
    )
    assert counts == {"sent": 1}
    assert re.load_plans()[0].status == "closing"
