from __future__ import annotations

from datetime import datetime, timezone

import pytest

from exchange_scanner.paper import (
    list_trades,
    log_signals,
    paper_summary,
    settle_results,
    update_closing_values,
)
from exchange_scanner.the_odds_api import ValueSignal


def signal(
    *,
    target_odds: float = 2.3,
    reference_fair_odds: float = 2.0,
    edge: float = 0.15,
) -> ValueSignal:
    return ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time=datetime(2026, 8, 14, 15, tzinfo=timezone.utc),
        market_key="h2h",
        outcome_name="Arsenal",
        target_bookmaker="Betway",
        target_odds=target_odds,
        reference_fair_odds=reference_fair_odds,
        reference_probability=1 / reference_fair_odds,
        edge=edge,
        reference_bookmakers=("Betfair", "Pinnacle"),
    )


def test_log_signals_dedupes_same_event_market(tmp_path) -> None:
    db_path = tmp_path / "paper.sqlite3"

    assert log_signals(db_path, [signal()], stake=10) == 1
    assert log_signals(db_path, [signal()], stake=10) == 0

    trades = list_trades(db_path)
    assert len(trades) == 1
    assert trades[0].event_id == "event-1"
    assert trades[0].stake == 10


def test_update_closing_values_tracks_clv_and_closing_edge(tmp_path) -> None:
    db_path = tmp_path / "paper.sqlite3"
    log_signals(db_path, [signal()], stake=10)

    updated = update_closing_values(
        db_path,
        [
            signal(
                target_odds=2.0,
                reference_fair_odds=2.0,
                edge=0,
            )
        ],
    )

    assert updated == 1
    trade = list_trades(db_path)[0]
    assert trade.closing_target_odds == 2.0
    assert trade.target_clv == pytest.approx(0.15)
    assert trade.closing_edge == pytest.approx(0.15)
    summary = paper_summary([trade])
    assert summary["beat_closing_line_rate"] == 1
    assert summary["positive_closing_edge_rate"] == 1


def test_settle_results_marks_trade_profit(tmp_path) -> None:
    db_path = tmp_path / "paper.sqlite3"
    log_signals(db_path, [signal()], stake=10)

    settled = settle_results(db_path, {"event-1": "Arsenal"})

    assert settled == 1
    trade = list_trades(db_path)[0]
    assert trade.status == "settled"
    assert trade.result == "Arsenal"
    assert trade.profit == pytest.approx(13)
    summary = paper_summary([trade])
    assert summary["settled"] == 1
    assert summary["settled_roi"] == pytest.approx(1.3)
