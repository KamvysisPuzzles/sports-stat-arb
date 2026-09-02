from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from exchange_scanner.tennis_lead_lag import (
    TENNIS_LEAD_LAG_STRATEGY,
    TennisLeadLagConfig,
    evaluate_and_record_tennis_lead_lag,
    find_tennis_lead_lag_signals,
)
from exchange_scanner.the_odds_api import OutcomePrice


class FakeTable:
    def __init__(self) -> None:
        self.items = {}

    def get_item(self, *, Key):
        item = self.items.get(Key["trade_id"])
        return {"Item": item} if item else {}

    def put_item(self, *, Item):
        self.items[Item["trade_id"]] = Item


def test_lead_lag_requires_confirmed_move_and_stale_target() -> None:
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    baseline = _baseline(now)

    signals = find_tennis_lead_lag_signals(
        _current_prices(now),
        baseline=baseline,
        now=now,
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.target_bookmaker == "Matchbook"
    assert signal.outcome_name == "Player A"
    assert signal.bet_side == "back"
    assert signal.strategy_name == TENNIS_LEAD_LAG_STRATEGY
    assert 0.01 <= signal.edge <= 0.03
    diagnostics = dict(signal.strategy_diagnostics)
    assert diagnostics["pinnacle_move_probability"] == pytest.approx(0.02)
    assert diagnostics["confirmation_count"] == 1
    assert diagnostics["target_lag_probability"] > 0.005


def test_lead_lag_rejects_unconfirmed_pinnacle_move() -> None:
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    prices = [price for price in _current_prices(now) if price.bookmaker_key != "betfair_ex_uk"]

    signals = find_tennis_lead_lag_signals(prices, baseline=_baseline(now), now=now)

    assert signals == []


def test_lead_lag_rejects_longshots_and_distant_matches() -> None:
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    longshot_prices = _current_prices(now, matchbook_a_odds=3.6, matchbook_b_odds=1.39)
    distant_prices = [
        _replace_commence_time(price, now + timedelta(hours=7))
        for price in _current_prices(now)
    ]

    assert find_tennis_lead_lag_signals(
        longshot_prices,
        baseline=_baseline(now),
        now=now,
    ) == []
    assert find_tennis_lead_lag_signals(
        distant_prices,
        baseline=_baseline(now),
        now=now,
    ) == []


def test_evaluation_records_history_then_uses_it_on_a_later_scan() -> None:
    table = FakeTable()
    first_scan = datetime(2026, 9, 2, 11, 54, tzinfo=timezone.utc)
    first = evaluate_and_record_tennis_lead_lag(
        table,
        _initial_prices(first_scan),
        now=first_scan,
    )

    assert first.signals == ()
    assert first.state_updates == 1
    assert len(table.items) == 1
    state = next(iter(table.items.values()))
    assert state["status"] == "control"

    second_scan = first_scan + timedelta(minutes=6)
    second = evaluate_and_record_tennis_lead_lag(
        table,
        _current_prices(second_scan),
        now=second_scan,
    )

    assert len(second.signals) == 1
    assert second.sports_with_history == 1
    assert second.state_updates == 1
    assert len(table.items) == 1


def test_tighter_edge_cap_can_reject_the_same_movement() -> None:
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)

    signals = find_tennis_lead_lag_signals(
        _current_prices(now),
        baseline=_baseline(now),
        now=now,
        config=TennisLeadLagConfig(max_edge=0.02),
    )

    assert signals == []


def _baseline(now: datetime) -> dict:
    return {
        "observed_at": (now - timedelta(minutes=6)).isoformat(),
        "probabilities": [
            ["tennis-event", "h2h", "pinnacle", "Player A", 0.50],
            ["tennis-event", "h2h", "pinnacle", "Player B", 0.50],
            ["tennis-event", "h2h", "betfair", "Player A", 0.50],
            ["tennis-event", "h2h", "betfair", "Player B", 0.50],
            ["tennis-event", "h2h", "matchbook", "Player A", 0.495],
            ["tennis-event", "h2h", "matchbook", "Player B", 0.505],
        ],
    }


def _initial_prices(now: datetime) -> list[OutcomePrice]:
    return [
        *_book_prices("pinnacle", "Pinnacle", 2.0, 2.0, now),
        *_book_prices("betfair_ex_uk", "Betfair", 2.0, 2.0, now, exchange=True),
        *_book_prices("matchbook", "Matchbook", 2.02, 1.98, now, exchange=True),
    ]


def _current_prices(
    now: datetime,
    *,
    matchbook_a_odds: float = 2.02,
    matchbook_b_odds: float = 1.98,
) -> list[OutcomePrice]:
    return [
        *_book_prices("pinnacle", "Pinnacle", 1 / 0.52, 1 / 0.48, now),
        *_book_prices("betfair_ex_uk", "Betfair", 1 / 0.508, 1 / 0.492, now, exchange=True),
        *_book_prices(
            "matchbook",
            "Matchbook",
            matchbook_a_odds,
            matchbook_b_odds,
            now,
            exchange=True,
        ),
    ]


def _book_prices(
    bookmaker_key: str,
    bookmaker_title: str,
    player_a_odds: float,
    player_b_odds: float,
    now: datetime,
    *,
    exchange: bool = False,
) -> list[OutcomePrice]:
    return [
        _price(
            bookmaker_key,
            bookmaker_title,
            "Player A",
            player_a_odds,
            now,
            exchange=exchange,
        ),
        _price(
            bookmaker_key,
            bookmaker_title,
            "Player B",
            player_b_odds,
            now,
            exchange=exchange,
        ),
    ]


def _price(
    bookmaker_key: str,
    bookmaker_title: str,
    outcome_name: str,
    odds: float,
    now: datetime,
    *,
    exchange: bool,
) -> OutcomePrice:
    return OutcomePrice(
        bookmaker_key=bookmaker_key,
        bookmaker_title=bookmaker_title,
        sport_key="tennis_atp_us_open",
        event_id="tennis-event",
        event_name="Player A v Player B",
        commence_time=now + timedelta(hours=3),
        market_key="h2h",
        market_name="Head to Head",
        outcome_name=outcome_name,
        point=None,
        odds=odds,
        last_update=now - timedelta(seconds=5),
        exchange_lay_odds=odds * 1.01 if exchange else None,
        exchange_spread_pct=0.01 if exchange else None,
    )


def _replace_commence_time(price: OutcomePrice, commence_time: datetime) -> OutcomePrice:
    return OutcomePrice(
        bookmaker_key=price.bookmaker_key,
        bookmaker_title=price.bookmaker_title,
        sport_key=price.sport_key,
        event_id=price.event_id,
        event_name=price.event_name,
        commence_time=commence_time,
        market_key=price.market_key,
        market_name=price.market_name,
        outcome_name=price.outcome_name,
        point=price.point,
        odds=price.odds,
        last_update=price.last_update,
        exchange_lay_odds=price.exchange_lay_odds,
        exchange_spread_pct=price.exchange_spread_pct,
    )
