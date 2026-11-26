from __future__ import annotations

import pytest

from exchange_rules import (
    is_multiple,
    min_orderable_qty,
    normalize_amount,
    normalize_price,
    rules_from_market,
    validate_order,
)


def binance_market() -> dict:
    return {
        "symbol": "BTC/USDT",
        "base": "BTC",
        "quote": "USDT",
        "type": "spot",
        "limits": {
            "amount": {"min": 1e-05, "max": 9000.0},
            "price": {"min": 0.01, "max": 1000000.0},
            "cost": {"min": 5.0, "max": 9000000.0},
        },
        "precision": {"amount": 1e-05, "price": 0.01},
        "taker": 0.001,
        "maker": 0.001,
    }


# ─── rules ────────────────────────────────────────────
def test_rules_from_binance_like_market():
    rules = rules_from_market(binance_market())
    assert rules.symbol == "BTC/USDT"
    assert rules.type == "spot"
    assert rules.min_amount == pytest.approx(1e-05)
    assert rules.amount_step == pytest.approx(1e-05)
    assert rules.min_cost == 5.0
    assert rules.price_tick == pytest.approx(0.01)
    assert rules.taker_fee == pytest.approx(0.001)


def test_precision_as_decimals_becomes_a_step():
    market = {"symbol": "ETH/USDT", "precision": {"amount": 3, "price": 2}}
    rules = rules_from_market(market)
    assert rules.amount_step == pytest.approx(0.001)
    assert rules.price_tick == pytest.approx(0.01)


def test_missing_limits_are_none():
    rules = rules_from_market({"symbol": "X/Y"})
    assert rules.min_amount is None
    assert rules.amount_step is None
    assert rules.price_tick is None
    assert rules.taker_fee is None


def test_explicit_step_wins_over_precision():
    market = {
        "symbol": "SOL/USDT",
        "limits": {"amount": {"step": 0.1}},
        "precision": {"amount": 4},
    }
    assert rules_from_market(market).amount_step == pytest.approx(0.1)


# ─── amounts ──────────────────────────────────────────
def test_normalize_amount_floors_to_step():
    rules = rules_from_market(binance_market())
    assert normalize_amount(rules, 0.000219) == pytest.approx(0.00021)
    assert normalize_amount(rules, 0.0002) == pytest.approx(0.0002)


def test_amount_below_exchange_minimum_is_zero():
    rules = rules_from_market(binance_market())
    assert normalize_amount(rules, 0.000001) == 0.0


def test_amount_above_maximum_is_clamped_and_floored():
    rules = rules_from_market(binance_market())
    assert normalize_amount(rules, 1e9) == pytest.approx(9000.0)


def test_normalize_amount_without_rules():
    rules = rules_from_market({"symbol": "X/Y"})
    assert normalize_amount(rules, 1.23456) == pytest.approx(1.23456)


# ─── prices ───────────────────────────────────────────
def test_normalize_price_rounds_to_tick():
    rules = rules_from_market(binance_market())
    assert normalize_price(rules, 60686.785) == pytest.approx(60686.79)
    assert normalize_price(rules, 60686.784) == pytest.approx(60686.78)


def test_normalize_price_without_tick_is_unchanged():
    rules = rules_from_market({"symbol": "X/Y"})
    assert normalize_price(rules, 1.23456789) == 1.23456789


# ─── multiples ────────────────────────────────────────
def test_is_multiple_tolerates_float_noise():
    assert is_multiple(0.0002, 1e-05)
    assert is_multiple(0.30000000000000004, 0.1)
    assert not is_multiple(0.00021, 0.001)


# ─── validation ───────────────────────────────────────
def test_valid_order_has_no_issues():
    rules = rules_from_market(binance_market())
    assert validate_order(rules, 0.0002, 60686.7, min_notional_usd=10) == []


def test_step_violation_is_reported():
    rules = rules_from_market(binance_market())
    issues = validate_order(rules, 0.000219, 60686.7)
    assert any("step" in issue for issue in issues)


def test_min_amount_violation_is_reported():
    rules = rules_from_market(binance_market())
    issues = validate_order(rules, 0.000001, 60686.7)
    assert any("mínimo" in issue for issue in issues)


def test_min_notional_violation_is_reported_with_operable_quantity():
    rules = rules_from_market(binance_market())
    issues = validate_order(rules, 0.0001, 60686.7, min_notional_usd=10)
    assert any("notional" in issue for issue in issues)
    assert any("unidades" in issue for issue in issues)


def test_price_missing_with_min_cost_is_reported():
    rules = rules_from_market(binance_market())
    issues = validate_order(rules, 0.001, None)
    assert any("precio" in issue for issue in issues)


def test_below_min_cost_uses_exchange_value():
    rules = rules_from_market(binance_market())
    # 0.00005 * 60000 = $3, below the exchange's $5 minimum
    issues = validate_order(rules, 0.00005, 60000)
    assert any("$5.00" in issue for issue in issues)


# ─── operable quantity ────────────────────────────────
def test_min_orderable_qty_respects_usd_floor():
    rules = rules_from_market(binance_market())
    qty = min_orderable_qty(rules, price=60000, min_notional_usd=10)
    assert qty == pytest.approx(0.00017)  # 10/60000 = 0.0001667 -> ceil to 1e-05
    assert qty * 60000 >= 10


def test_min_orderable_qty_uses_exchange_cost_when_higher():
    rules = rules_from_market(binance_market())
    qty = min_orderable_qty(rules, price=60000, min_notional_usd=0)
    assert qty == pytest.approx(0.00009)  # 5/60000 -> ceil
