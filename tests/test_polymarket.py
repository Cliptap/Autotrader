from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import polymarket as pm

NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)


def raw_market(**overrides) -> dict:
    data = {
        "id": "6747203",
        "question": "Chiefs vs. Bills",
        "slug": "nfl-kc-buf-2026-11-27",
        "conditionId": "0xabc",
        "outcomes": '["Chiefs", "Bills"]',
        "clobTokenIds": '["111", "222"]',
        "liquidity": "3036362.497",
        "volume24hr": "186173.82",
        "endDate": "2026-11-27T01:20:00Z",
        "closed": False,
        "lastTradePrice": 0.4,
        "bestBid": 0.39,
        "bestAsk": 0.4,
    }
    data.update(overrides)
    return data


# ─── parsing ──────────────────────────────────────────
def test_parse_market_reads_json_string_fields():
    market = pm.parse_market(raw_market())
    assert market.slug == "nfl-kc-buf-2026-11-27"
    assert [outcome.name for outcome in market.outcomes] == ["Chiefs", "Bills"]
    assert [outcome.token_id for outcome in market.outcomes] == ["111", "222"]
    assert market.liquidity == pytest.approx(3036362.497)
    assert market.volume_24h == pytest.approx(186173.82)
    assert market.end_date == datetime(2026, 11, 27, 1, 20, tzinfo=timezone.utc)


def test_parse_market_accepts_already_parsed_lists():
    market = pm.parse_market(raw_market(outcomes=["Yes", "No"], clobTokenIds=["1", "2"]))
    assert [outcome.name for outcome in market.outcomes] == ["Yes", "No"]


def test_parse_market_tolerates_broken_fields():
    market = pm.parse_market({"outcomes": "[not json", "liquidity": "n/a", "closed": False})
    assert market.outcomes == []
    assert market.liquidity == 0.0
    assert market.end_date is None


def test_token_for_is_case_insensitive():
    market = pm.parse_market(raw_market())
    assert market.token_for("chiefs").token_id == "111"
    assert market.token_for("Bills").token_id == "222"
    assert market.token_for("Draw") is None


# ─── filtering ────────────────────────────────────────
def test_filter_drops_closed_and_illiquid():
    markets = [
        pm.parse_market(raw_market(id="1", liquidity="100")),
        pm.parse_market(raw_market(id="2", liquidity="50000", slug="other")),
        pm.parse_market(raw_market(id="3", closed=True, liquidity="900000")),
    ]
    result = pm.filter_markets(markets, min_liquidity=1000)
    assert [market.id for market in result] == ["2"]


def test_filter_by_query_matches_question_and_slug():
    markets = [
        pm.parse_market(raw_market(id="1", question="Fed cuts rates?")),
        pm.parse_market(raw_market(id="2", question="Chiefs vs Bills")),
    ]
    assert [market.id for market in pm.filter_markets(markets, query="fed")] == ["1"]


def test_filter_by_hours_left():
    soon = pm.parse_market(raw_market(id="soon", endDate=(NOW + timedelta(hours=5)).isoformat()))
    later = pm.parse_market(raw_market(id="later", endDate=(NOW + timedelta(hours=500)).isoformat()))
    expired = pm.parse_market(raw_market(id="old", endDate=(NOW - timedelta(hours=1)).isoformat()))
    result = pm.filter_markets([soon, later, expired], max_hours=24, now=NOW)
    assert [market.id for market in result] == ["soon"]


def test_filter_sorts_by_liquidity_desc():
    markets = [
        pm.parse_market(raw_market(id="low", liquidity="1500")),
        pm.parse_market(raw_market(id="high", liquidity="900000")),
    ]
    assert [market.id for market in pm.filter_markets(markets)] == ["high", "low"]


# ─── order book ───────────────────────────────────────
def test_book_summary_uses_best_levels_regardless_of_order():
    book = {
        "bids": [
            {"price": "0.38", "size": "2000"},
            {"price": "0.39", "size": "1000"},
        ],
        "asks": [
            {"price": "0.41", "size": "100"},
            {"price": "0.40", "size": "500"},
        ],
    }
    summary = pm.book_summary(book)
    assert summary["best_bid"] == pytest.approx(0.39)
    assert summary["best_ask"] == pytest.approx(0.40)
    assert summary["mid"] == pytest.approx(0.395)
    assert summary["spread"] == pytest.approx(0.01)
    assert summary["spread_pct"] == pytest.approx(2.5316, rel=1e-3)
    assert summary["bid_depth_usd"] == pytest.approx(390 + 760)
    assert summary["ask_depth_usd"] == pytest.approx(200 + 41)


def test_book_summary_handles_one_sided_book():
    summary = pm.book_summary({"bids": [{"price": "0.2", "size": "10"}], "asks": []})
    assert summary["best_bid"] == pytest.approx(0.2)
    assert summary["best_ask"] is None
    assert summary["mid"] is None
    assert summary["spread"] is None


def test_book_summary_ignores_invalid_levels():
    summary = pm.book_summary(
        {"bids": [{"price": "0", "size": "5"}, {"price": "x", "size": "1"}], "asks": []}
    )
    assert summary["best_bid"] is None
    assert summary["bids"] == 0


# ─── edge and sizing ──────────────────────────────────
def test_edge_positive_when_model_beats_price():
    metrics = pm.edge(0.45, 0.40)
    assert metrics["ev_per_share"] == pytest.approx(0.05)
    assert metrics["roi"] == pytest.approx(0.125)
    assert metrics["kelly"] == pytest.approx(0.08333, rel=1e-4)


def test_edge_negative_when_price_is_rich():
    metrics = pm.edge(0.30, 0.40)
    assert metrics["ev_per_share"] == pytest.approx(-0.10)
    assert metrics["kelly"] < 0


def test_edge_rejects_impossible_prices():
    with pytest.raises(ValueError):
        pm.edge(0.5, 0)
    with pytest.raises(ValueError):
        pm.edge(1.2, 0.5)


def test_stake_is_capped_by_risk_limit():
    sizing = pm.stake(0.45, 0.40, equity=1000, max_pct=2.0)
    assert sizing["stake_usd"] == pytest.approx(20.0)
    assert sizing["capped"] is True
    assert sizing["stake_pct"] == pytest.approx(2.0)


def test_stake_uses_half_kelly_when_below_cap():
    sizing = pm.stake(0.62, 0.60, equity=100_000, max_pct=2.0)
    # kelly = 0.02/0.40 = 5%, half kelly = 2.5% but capped at 2%
    assert sizing["capped"] is True
    sizing = pm.stake(0.605, 0.60, equity=100_000, max_pct=2.0)
    # kelly = 0.005/0.4 = 1.25%, half = 0.625% -> $625, below the $2,000 cap
    assert sizing["stake_usd"] == pytest.approx(625.0)
    assert sizing["capped"] is False


def test_stake_is_zero_without_edge():
    sizing = pm.stake(0.35, 0.40, equity=1000)
    assert sizing["stake_usd"] == 0.0


# ─── paper payload ────────────────────────────────────
def test_paper_payload_matches_ai_trader_contract():
    market = pm.parse_market(raw_market())
    outcome = market.token_for("Chiefs")
    payload = pm.paper_payload(market, outcome, price=0.40, size_usd=20)
    assert payload["market"] == "polymarket"
    assert payload["action"] == "buy"
    assert payload["symbol"] == market.slug
    assert payload["outcome"] == "Chiefs"
    assert payload["token_id"] == "111"
    assert payload["quantity"] == pytest.approx(50.0)
    assert payload["price"] == pytest.approx(0.40)


def test_paper_payload_rejects_zero_price():
    market = pm.parse_market(raw_market())
    with pytest.raises(ValueError):
        pm.paper_payload(market, market.outcomes[0], price=0, size_usd=10)
