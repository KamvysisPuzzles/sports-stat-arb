from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from exchange_scanner.backtest import backtest_summary, run_backtest
from exchange_scanner.cli import MATCHBOOK_TARGET_BOOKMAKERS


def test_run_backtest_settles_value_bets_against_results(tmp_path: Path) -> None:
    historical_odds = tmp_path / "odds.json"
    historical_odds.write_text(
        json.dumps(
            {
                "fetched_at": "2026-08-12T21:55:00Z",
                "payload": [
                    {
                        "id": "event-1",
                        "sport_key": "soccer_epl",
                        "home_team": "Arsenal",
                        "away_team": "Chelsea",
                        "commence_time": "2026-08-14T15:00:00Z",
                        "bookmakers": [
                            {
                                "key": "matchbook",
                                "title": "Matchbook",
                                "last_update": "2026-08-12T21:55:00Z",
                                "markets": [
                                    {
                                        "key": "h2h",
                                        "outcomes": [
                                            {"name": "Arsenal", "price": 2.3},
                                            {"name": "Chelsea", "price": 1.8},
                                        ],
                                    }
                                ],
                            },
                            {
                                "key": "pinnacle",
                                "title": "Pinnacle",
                                "last_update": "2026-08-12T21:55:00Z",
                                "markets": [
                                    {
                                        "key": "h2h",
                                        "outcomes": [
                                            {"name": "Arsenal", "price": 2.0},
                                            {"name": "Chelsea", "price": 2.0},
                                        ],
                                    }
                                ],
                            },
                            {
                                "key": "betfair",
                                "title": "Betfair",
                                "last_update": "2026-08-12T21:55:00Z",
                                "markets": [
                                    {
                                        "key": "h2h",
                                        "outcomes": [
                                            {"name": "Arsenal", "price": 2.0},
                                            {"name": "Chelsea", "price": 2.0},
                                        ],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        )
    )
    closing_odds = tmp_path / "closing.json"
    closing_odds.write_text(
        json.dumps(
            {
                "fetched_at": "2026-08-13T21:55:00Z",
                "payload": [
                    {
                        "id": "event-1",
                        "sport_key": "soccer_epl",
                        "home_team": "Arsenal",
                        "away_team": "Chelsea",
                        "commence_time": "2026-08-14T15:00:00Z",
                        "bookmakers": [
                            {
                                "key": "matchbook",
                                "title": "Matchbook",
                                "last_update": "2026-08-13T21:55:00Z",
                                "markets": [
                                    {
                                        "key": "h2h",
                                        "outcomes": [
                                            {"name": "Arsenal", "price": 2.0},
                                            {"name": "Chelsea", "price": 2.0},
                                        ],
                                    }
                                ],
                            },
                            {
                                "key": "pinnacle",
                                "title": "Pinnacle",
                                "last_update": "2026-08-13T21:55:00Z",
                                "markets": [
                                    {
                                        "key": "h2h",
                                        "outcomes": [
                                            {"name": "Arsenal", "price": 2.0},
                                            {"name": "Chelsea", "price": 2.0},
                                        ],
                                    }
                                ],
                            },
                            {
                                "key": "betfair",
                                "title": "Betfair",
                                "last_update": "2026-08-13T21:55:00Z",
                                "markets": [
                                    {
                                        "key": "h2h",
                                        "outcomes": [
                                            {"name": "Arsenal", "price": 2.0},
                                            {"name": "Chelsea", "price": 2.0},
                                        ],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        )
    )
    results = tmp_path / "results.csv"
    with results.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["event_id", "market", "winner"])
        writer.writeheader()
        writer.writerow({"event_id": "event-1", "market": "h2h", "winner": "Arsenal"})

    bets = run_backtest(
        historical_odds_path=tmp_path,
        results_path=results,
        target_bookmakers=MATCHBOOK_TARGET_BOOKMAKERS,
        reference_bookmakers=None,
        markets={"h2h"},
        min_edge=0.05,
        max_age_seconds=300,
        min_reference_books=2,
        include_started=False,
        max_event_days=2,
        unique_events=True,
        stake=10,
        daily_decision_time="22:00",
        allow_rebet_same_event=False,
        allow_target_bookmakers_as_references=True,
    )

    assert len(bets) == 1
    assert bets[0].signal.event_id == "event-1"
    assert bets[0].won is True
    assert bets[0].profit == pytest.approx(13)
    assert bets[0].closing_target_odds == 2.0
    assert bets[0].target_clv == pytest.approx(0.15)
    assert bets[0].closing_fair_edge == pytest.approx(0.15)
    summary = backtest_summary(bets)
    assert summary["roi"] == pytest.approx(1.3)
    assert summary["beat_closing_line_rate"] == 1
    assert summary["positive_closing_fair_edge_rate"] == 1
