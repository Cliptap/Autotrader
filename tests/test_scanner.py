from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import scanner


def signal(symbol: str, side: str, agent: int = 1, market: str = "crypto") -> dict:
    return {
        "id": f"{agent}-{symbol}-{side}",
        "agent_id": agent,
        "symbol": symbol,
        "side": side,
        "market": market,
    }


def payload(*signals: dict) -> dict:
    return {"signals": list(signals)}


# ─── signal parsing ───────────────────────────────────
def test_normalize_side_maps_short_and_cover_to_sell():
    assert scanner.normalize_side("short") == "sell"
    assert scanner.normalize_side("cover") == "sell"
    assert scanner.normalize_side("buy") == "buy"


def test_collect_agent_actions_labels_the_agent():
    actions = scanner.collect_agent_actions(
        payload(signal("BTC", "buy"), signal("ETH", "sell")),
        "Hermes",
        {"BTC", "ETH"},
    )
    assert actions["BTC"]["buy"] == ["Hermes"]
    assert actions["ETH"]["sell"] == ["Hermes"]


def test_collect_agent_actions_ignores_untracked_symbols():
    actions = scanner.collect_agent_actions(payload(signal("ZZZ", "buy")), "X", {"BTC"})
    assert actions == {}


def test_collect_feed_actions_counts_per_side():
    actions = scanner.collect_feed_actions(
        payload(signal("BTC", "buy", 1), signal("BTC", "buy", 2), signal("BTC", "short", 3)),
        {"BTC"},
    )
    assert actions["BTC"] == {"buy": 2, "sell": 1}


# ─── consensus ────────────────────────────────────────
def test_consensus_below_threshold_is_ignored():
    actions = {"BTC": {"buy": ["Ava"], "sell": []}}
    assert scanner.evaluate_agent_alerts(actions, False, 2) == []


def test_consensus_alert_contains_agents_and_macro_flag():
    actions = {"BTC": {"buy": ["Ava", "Hermes"], "sell": []}}
    alerts = scanner.evaluate_agent_alerts(actions, True, 2, prices=lambda symbol: 61000)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.key == "consensus:BTC"
    assert "2 agentes comprando" in alert.message
    assert "MACRO BULLISH" in alert.message
    assert alert.priority == "default"


def test_consensus_with_three_agents_is_high_priority():
    actions = {"ETH": {"buy": ["A", "B", "C"], "sell": []}}
    alerts = scanner.evaluate_agent_alerts(actions, False, 2)
    assert alerts[0].priority == "high"


def test_frenzy_alert_triggers_at_threshold():
    feed = {"BTC": {"buy": 5, "sell": 0}, "ETH": {"buy": 4, "sell": 0}}
    alerts = scanner.evaluate_feed_alerts(feed, 5)
    assert [alert.symbol for alert in alerts] == ["BTC"]


# ─── stocks ───────────────────────────────────────────
STOCK_CONTENT = """
Portfolio update:
- AAPL: **BUY** (Score: 8.5, Community: 12B)
- NVDA: **BUY** (Score: 7.0, Community: 20B)
- MSFT: **SELL** (Score: 9.0, Community: 15B)
"""


def test_parse_stock_signals_filters_by_score_and_community():
    found = scanner.parse_stock_signals(STOCK_CONTENT, ["AAPL", "NVDA", "MSFT"], 8, 5)
    assert found == [("AAPL", 8.5, 12)]


def test_stock_alerts_only_read_us_stock_market():
    signals = payload(
        {"id": 1, "market": "crypto", "content": STOCK_CONTENT},
        {"id": 2, "market": "us-stock", "content": STOCK_CONTENT},
    )
    alerts = scanner.evaluate_stock_alerts(signals, ["AAPL"], 8, 5, prices=lambda s, m: 220)
    assert len(alerts) == 1
    assert alerts[0].key == "stock:AAPL"
    assert "220" in alerts[0].message


# ─── market hours ─────────────────────────────────────
def test_market_open_in_winter_uses_est():
    # 2026-01-15 15:00 UTC = 10:00 EST
    assert scanner.is_us_market_open(datetime(2026, 1, 15, 15, tzinfo=timezone.utc))


def test_market_open_in_summer_uses_edt():
    # 2026-07-15 13:30 UTC = 09:30 EDT
    assert scanner.is_us_market_open(datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc))
    # 13:00 UTC = 09:00 EDT, todavía cerrado
    assert not scanner.is_us_market_open(datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc))


def test_market_closed_on_weekends():
    # 2026-01-17 is a Saturday
    assert not scanner.is_us_market_open(datetime(2026, 1, 17, 15, tzinfo=timezone.utc))


# ─── price moves ──────────────────────────────────────
NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)


def price_event(minutes_ago: float, bid: float, symbol: str = "BTC/USDT", **overrides) -> dict:
    event = {
        "ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "source": "binance",
        "type": "book_ticker",
        "symbol": symbol,
        "bid": bid,
    }
    event.update(overrides)
    return event


def test_upward_move_triggers_a_high_priority_alert():
    events = [price_event(10, 100.0), price_event(1, 102.5)]
    alerts = scanner.price_moves(events, window_minutes=15, threshold_pct=1.0, now=NOW)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.key == "move:BTC/USDT"
    assert alert.kind == "move"
    assert "+2.50%" in alert.title
    assert alert.priority == "high"


def test_downward_move_is_negative():
    events = [price_event(10, 100.0), price_event(1, 98.0)]
    alerts = scanner.price_moves(events, window_minutes=15, threshold_pct=1.0, now=NOW)
    assert "-2.00%" in alerts[0].title
    assert "▼" in alerts[0].title


def test_move_below_threshold_is_ignored():
    events = [price_event(10, 100.0), price_event(1, 100.4)]
    assert scanner.price_moves(events, window_minutes=15, threshold_pct=1.0, now=NOW) == []


def test_events_outside_the_window_are_ignored():
    events = [price_event(40, 100.0), price_event(30, 110.0)]
    assert scanner.price_moves(events, window_minutes=15, threshold_pct=1.0, now=NOW) == []


def test_single_point_is_not_a_move():
    assert scanner.price_moves([price_event(1, 100.0)], now=NOW) == []


def test_non_binance_or_non_ticker_events_are_ignored():
    events = [
        price_event(10, 100.0, source="ai-trader"),
        price_event(1, 110.0, source="ai-trader"),
        price_event(10, 100.0, type="trade"),
        price_event(1, 110.0, type="trade"),
    ]
    assert scanner.price_moves(events, now=NOW) == []


def test_moderate_move_is_default_priority():
    events = [price_event(10, 100.0), price_event(1, 101.5)]
    alerts = scanner.price_moves(events, window_minutes=15, threshold_pct=1.0, now=NOW)
    assert alerts[0].priority == "default"


# ─── dedup state ──────────────────────────────────────
def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = {"active": ["consensus:BTC"], "alerts": {"consensus:BTC": {"last_alert": "2026-11-26T12:00:00+00:00", "count": 2}}}
    scanner.save_alert_state(state, path)
    assert scanner.load_alert_state(path) == state


def test_missing_state_is_empty(tmp_path):
    assert scanner.load_alert_state(tmp_path / "nope.json") == {"active": [], "alerts": {}}


def test_corrupt_state_falls_back_to_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    assert scanner.load_alert_state(path) == {"active": [], "alerts": {}}


# ─── dispatch ─────────────────────────────────────────
class Recorder:
    def __init__(self):
        self.pushes = []
        self.logs = []

    def notify(self, title, message, priority="default"):
        self.pushes.append((title, message, priority))

    def log(self, entry):
        self.logs.append(entry)


def make_alert(symbol: str = "BTC") -> scanner.Alert:
    return scanner.Alert(
        key=f"consensus:{symbol}",
        symbol=symbol,
        kind="consensus",
        title=f"BTC comprando",
        message="2 agentes",
    )


def test_first_alert_is_sent_and_state_updated():
    recorder = Recorder()
    state = {"active": [], "alerts": {}}
    summary = scanner.dispatch([make_alert()], state, notify=recorder.notify, log=recorder.log)
    assert summary == {"sent": 1, "suppressed": 0, "breaks": 0}
    assert len(recorder.pushes) == 1
    assert state["active"] == ["consensus:BTC"]
    assert state["alerts"]["consensus:BTC"]["count"] == 1


def test_repeated_alert_inside_cooldown_is_suppressed():
    recorder = Recorder()
    now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
    state = {"active": [], "alerts": {}}
    scanner.dispatch([make_alert()], state, now=now, notify=recorder.notify, log=recorder.log)

    scanner.dispatch(
        [make_alert()],
        state,
        now=now + timedelta(minutes=10),
        notify=recorder.notify,
        log=recorder.log,
    )
    assert len(recorder.pushes) == 1
    assert state["alerts"]["consensus:BTC"]["count"] == 1


def test_alert_fires_again_after_cooldown():
    recorder = Recorder()
    now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
    state = {"active": [], "alerts": {}}
    scanner.dispatch([make_alert()], state, now=now, notify=recorder.notify, log=recorder.log)
    scanner.dispatch(
        [make_alert()],
        state,
        now=now + timedelta(minutes=61),
        notify=recorder.notify,
        log=recorder.log,
    )
    assert len(recorder.pushes) == 2
    assert state["alerts"]["consensus:BTC"]["count"] == 2


def test_consensus_break_notifies_once_and_clears_state():
    recorder = Recorder()
    now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
    state = {"active": [], "alerts": {}}
    scanner.dispatch([make_alert()], state, now=now, notify=recorder.notify, log=recorder.log)

    summary = scanner.dispatch([], state, now=now + timedelta(minutes=5), notify=recorder.notify, log=recorder.log)
    assert summary["breaks"] == 1
    assert "Consenso terminado" in recorder.pushes[-1][0]
    assert state["active"] == []
    assert "consensus:BTC" not in state["alerts"]

    # A second empty scan must not repeat the break notice.
    recorder.pushes.clear()
    scanner.dispatch([], state, now=now + timedelta(minutes=10), notify=recorder.notify, log=recorder.log)
    assert recorder.pushes == []


def test_frenzy_and_consensus_have_independent_cooldowns():
    recorder = Recorder()
    state = {"active": [], "alerts": {}}
    frenzy = scanner.Alert("frenzy:BTC", "BTC", "frenzy", "FRENESI", "5 agentes")
    scanner.dispatch([make_alert(), frenzy], state, notify=recorder.notify, log=recorder.log)
    assert len(recorder.pushes) == 2
    assert state["alerts"]["frenzy:BTC"]["count"] == 1
