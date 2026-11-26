"""Exchange-agnostic order normalization.

CCXT unifies symbols but every venue has its own minimums, quantity steps,
price ticks and fees. Hardcoding Binance numbers breaks the moment the exchange
changes. This module derives the rules from the market metadata CCXT returns
and normalizes orders against them.

Pure functions only: the caller passes the market dict, so everything here is
testable without network.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

PRICE_DECIMALS = Decimal("1e-9")


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _precision_step(label: Any) -> float | None:
    """CCXT exposes precision either as decimals (2) or as a step (0.01)."""
    if label is None:
        return None
    if isinstance(label, int):
        return 10 ** -label
    value = _float_or_none(label)
    if value is None or value <= 0:
        return None
    return value


@dataclass(frozen=True)
class MarketRules:
    symbol: str
    base: str
    quote: str
    type: str
    min_amount: float | None
    max_amount: float | None
    amount_step: float | None
    min_cost: float | None
    max_cost: float | None
    price_tick: float | None
    taker_fee: float | None
    maker_fee: float | None

    def summary(self) -> str:
        parts = [
            f"{self.symbol} ({self.type})",
            f"min amount={self.min_amount if self.min_amount is not None else '—'}",
            f"step={self.amount_step if self.amount_step is not None else '—'}",
            f"min cost={self.min_cost if self.min_cost is not None else '—'}",
            f"tick={self.price_tick if self.price_tick is not None else '—'}",
            f"taker={self.taker_fee if self.taker_fee is not None else '—'}",
        ]
        return " · ".join(parts)


def rules_from_market(market: dict[str, Any]) -> MarketRules:
    limits = market.get("limits") or {}
    amount_limits = limits.get("amount") or {}
    cost_limits = limits.get("cost") or {}
    price_limits = limits.get("price") or {}
    precision = market.get("precision") or {}

    step = _float_or_none(amount_limits.get("step")) or _precision_step(precision.get("amount"))
    tick = _precision_step(precision.get("price"))

    return MarketRules(
        symbol=str(market.get("symbol", "")),
        base=str(market.get("base", "")),
        quote=str(market.get("quote", "")),
        type=str(market.get("type") or "spot"),
        min_amount=_float_or_none(amount_limits.get("min")),
        max_amount=_float_or_none(amount_limits.get("max")),
        amount_step=step,
        min_cost=_float_or_none(cost_limits.get("min")),
        max_cost=_float_or_none(cost_limits.get("max")),
        price_tick=tick,
        taker_fee=_float_or_none(market.get("taker")),
        maker_fee=_float_or_none(market.get("maker")),
    )


def _step_decimal(rules: MarketRules) -> Decimal | None:
    if not rules.amount_step:
        return None
    step = _dec(rules.amount_step)
    return step if step > 0 else None


def is_multiple(value: float, step: float) -> bool:
    if not step:
        return True
    ratio = _dec(value) / _dec(step)
    return abs(ratio - ratio.to_integral_value(rounding=ROUND_HALF_UP)) < PRICE_DECIMALS


def normalize_amount(rules: MarketRules, qty: float) -> float:
    """Floor ``qty`` to the exchange step. Returns 0 when it cannot be ordered."""
    if qty <= 0:
        return 0.0
    amount = _dec(qty)
    step = _step_decimal(rules)
    if step is not None:
        amount = (amount / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if amount <= 0:
        return 0.0
    if rules.min_amount is not None and amount < _dec(rules.min_amount):
        return 0.0
    if rules.max_amount is not None and amount > _dec(rules.max_amount):
        amount = _dec(rules.max_amount)
        if step is not None:
            amount = (amount / step).to_integral_value(rounding=ROUND_FLOOR) * step
    return float(amount)


def normalize_price(rules: MarketRules, price: float) -> float:
    """Round ``price`` to the exchange tick."""
    tick = rules.price_tick
    if not tick or price <= 0:
        return price
    step = _dec(tick)
    if step <= 0:
        return price
    return float((_dec(price) / step).to_integral_value(rounding=ROUND_HALF_UP) * step)


def min_orderable_qty(rules: MarketRules, price: float, min_notional_usd: float = 0.0) -> float:
    """Smallest quantity that satisfies the exchange minimum and a USD floor."""
    floor = max(rules.min_cost or 0.0, min_notional_usd or 0.0)
    amount = Decimal(0)
    if floor and price and price > 0:
        amount = _dec(floor) / _dec(price)
    if rules.min_amount is not None:
        amount = max(amount, _dec(rules.min_amount))
    step = _step_decimal(rules)
    if step is not None:
        amount = (amount / step).to_integral_value(rounding=ROUND_CEILING) * step
    return float(amount)


def validate_order(
    rules: MarketRules,
    qty: float,
    price: float | None = None,
    min_notional_usd: float = 0.0,
) -> list[str]:
    """Return a list of human-readable problems. Empty means the order is valid."""
    issues: list[str] = []
    if qty <= 0:
        issues.append("cantidad debe ser mayor que cero")
        return issues

    if rules.amount_step and not is_multiple(qty, rules.amount_step):
        issues.append(f"cantidad {qty:g} no es múltiplo del step {rules.amount_step:g}")
    if rules.min_amount is not None and qty < rules.min_amount:
        issues.append(f"cantidad {qty:g} por debajo del mínimo {rules.min_amount:g}")
    if rules.max_amount is not None and qty > rules.max_amount:
        issues.append(f"cantidad {qty:g} por encima del máximo {rules.max_amount:g}")

    if price is None or price <= 0:
        if rules.min_cost:
            issues.append("falta un precio válido para validar el costo mínimo")
        return issues

    if rules.price_tick and not is_multiple(price, rules.price_tick):
        issues.append(f"precio {price:g} no es múltiplo del tick {rules.price_tick:g}")

    notional = qty * price
    required = max(rules.min_cost or 0.0, min_notional_usd or 0.0)
    if required and notional < required:
        issues.append(
            f"notional ${notional:,.2f} por debajo del mínimo ${required:,.2f} "
            f"(mínimo operativo: {min_orderable_qty(rules, price, min_notional_usd):g} unidades)"
        )
    if rules.max_cost is not None and notional > rules.max_cost:
        issues.append(f"notional ${notional:,.2f} por encima del máximo ${rules.max_cost:,.2f}")
    return issues
