from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from exchange_scanner.the_odds_api import (
    TheOddsApiClient,
    TheOddsApiError,
    bookmaker_url,
    find_value_opportunities,
    h2h_winners_from_scores,
    normalise_odds_api_events,
)


def test_bookmaker_url_handles_common_titles() -> None:
    assert bookmaker_url("Bet Victor") == "https://www.betvictor.com/"
    assert bookmaker_url("Unibet (UK)") == "https://www.unibet.co.uk/betting/sports/home"


def test_h2h_winners_from_scores_handles_wins_and_draws() -> None:
    winners = h2h_winners_from_scores(
        [
            [
                {
                    "id": "event-1",
                    "completed": True,
                    "scores": [
                        {"name": "Arsenal", "score": "2"},
                        {"name": "Chelsea", "score": "1"},
                    ],
                },
                {
                    "id": "event-2",
                    "completed": True,
                    "scores": [
                        {"name": "Liverpool", "score": "1"},
                        {"name": "Everton", "score": "1"},
                    ],
                },
                {
                    "id": "event-3",
                    "completed": False,
                    "scores": [],
                },
            ]
        ]
    )

    assert winners == {"event-1": "Arsenal", "event-2": "Draw"}


def test_filters_stale_prices() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "book_a",
                    "title": "Book A",
                    "last_update": "2026-08-12T11:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 3.0},
                                {"name": "Chelsea", "price": 3.0},
                            ],
                        }
                    ],
                }
            ],
        }
    ]

    prices = normalise_odds_api_events(events)
    signals = find_value_opportunities(
        prices,
        target_bookmakers={"book_a"},
        reference_bookmakers=None,
        min_edge=0.01,
        max_age_seconds=60,
        min_reference_books=1,
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert signals == []


def test_filters_started_events_by_default() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T11:59:00Z",
            "bookmakers": [
                {
                    "key": "book_a",
                    "title": "Book A",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 3.0},
                                {"name": "Chelsea", "price": 3.0},
                            ],
                        }
                    ],
                },
                {
                    "key": "book_b",
                    "title": "Book B",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 3.0},
                                {"name": "Chelsea", "price": 3.0},
                            ],
                        }
                    ],
                },
            ],
        }
    ]

    prices = normalise_odds_api_events(events)
    signals = find_value_opportunities(
        prices,
        target_bookmakers={"book_a"},
        reference_bookmakers=None,
        min_edge=0.01,
        max_age_seconds=300,
        min_reference_books=1,
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert signals == []


def test_finds_target_value_against_devigged_reference_market() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "basketball_wnba",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "target",
                    "title": "Target",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.3},
                                {"name": "Chelsea", "price": 1.7},
                            ],
                        }
                    ],
                },
                {
                    "key": "ref_a",
                    "title": "Reference A",
                    "last_update": "2026-08-12T12:00:00Z",
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
                    "key": "ref_b",
                    "title": "Reference B",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 1.95},
                                {"name": "Chelsea", "price": 2.05},
                            ],
                        }
                    ],
                },
            ],
        }
    ]

    prices = normalise_odds_api_events(events)
    signals = find_value_opportunities(
        prices,
        target_bookmakers={"target"},
        reference_bookmakers=None,
        min_edge=0.05,
        max_age_seconds=300,
        min_reference_books=2,
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert len(signals) == 1
    assert signals[0].sport_key == "basketball_wnba"
    assert signals[0].outcome_name == "Arsenal"
    assert signals[0].as_dict()["outcome_name"] == "Arsenal"
    assert signals[0].edge > 0.1
    assert signals[0].as_dict()["copy_search"] == "Arsenal v Chelsea Arsenal"
    assert signals[0].as_dict()["min_acceptable_odds"] == 2.3


def test_value_mode_counts_duplicate_reference_titles_once() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "basketball_wnba",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "target",
                    "title": "Target",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.3},
                                {"name": "Chelsea", "price": 1.7},
                            ],
                        }
                    ],
                },
                {
                    "key": "betfair_a",
                    "title": "Betfair",
                    "last_update": "2026-08-12T12:00:00Z",
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
                    "key": "betfair_b",
                    "title": "Betfair",
                    "last_update": "2026-08-12T12:00:00Z",
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
    ]

    prices = normalise_odds_api_events(events)
    signals = find_value_opportunities(
        prices,
        target_bookmakers={"target"},
        reference_bookmakers=None,
        min_edge=0.05,
        max_age_seconds=300,
        min_reference_books=2,
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert signals == []


def test_spreads_only_compare_the_same_point_line() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "baseball_mlb",
            "home_team": "Athletics",
            "away_team": "Rays",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "target",
                    "title": "Target",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Athletics", "price": 8.6, "point": -3.5},
                                {"name": "Rays", "price": 1.1, "point": 3.5},
                            ],
                        }
                    ],
                },
                {
                    "key": "ref_a",
                    "title": "Reference A",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Athletics", "price": 1.9, "point": 1.5},
                                {"name": "Rays", "price": 1.9, "point": -1.5},
                            ],
                        }
                    ],
                },
                {
                    "key": "ref_b",
                    "title": "Reference B",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Athletics", "price": 1.9, "point": 1.5},
                                {"name": "Rays", "price": 1.9, "point": -1.5},
                            ],
                        }
                    ],
                },
            ],
        }
    ]

    prices = normalise_odds_api_events(events)
    signals = find_value_opportunities(
        prices,
        target_bookmakers={"target"},
        reference_bookmakers=None,
        min_edge=0.05,
        max_age_seconds=300,
        min_reference_books=2,
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert signals == []


def test_value_mode_ignores_incomplete_reference_markets() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Tottenham Hotspur",
            "away_team": "Charlton Athletic",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "target",
                    "title": "Target",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Tottenham Hotspur", "price": 1.3},
                                {"name": "Draw", "price": 5.0},
                                {"name": "Charlton Athletic", "price": 10.0},
                            ],
                        }
                    ],
                },
                {
                    "key": "ref_a",
                    "title": "Reference A",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Tottenham Hotspur", "price": 1.4},
                                {"name": "Charlton Athletic", "price": 3.0},
                            ],
                        }
                    ],
                },
                {
                    "key": "ref_b",
                    "title": "Reference B",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Tottenham Hotspur", "price": 1.4},
                                {"name": "Charlton Athletic", "price": 3.0},
                            ],
                        }
                    ],
                },
            ],
        }
    ]

    prices = normalise_odds_api_events(events)
    signals = find_value_opportunities(
        prices,
        target_bookmakers={"target"},
        reference_bookmakers={"ref_a", "ref_b"},
        min_edge=0.05,
        max_age_seconds=300,
        min_reference_books=2,
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert signals == []


def test_odds_api_error_does_not_include_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            request=request,
            headers={"x-requests-used": "500", "x-requests-remaining": "0"},
        )

    client = TheOddsApiClient(api_key="secret-key")
    client.http = httpx.Client(
        base_url="https://api.the-odds-api.com/v4",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TheOddsApiError) as exc_info:
        client.fetch_odds(sport="soccer_epl", regions="uk,eu", markets="h2h")

    message = str(exc_info.value)
    assert "secret-key" not in message
    assert "HTTP 401" in message
    assert "requests_remaining=0" in message
    assert exc_info.value.__cause__ is None


def test_odds_api_client_reuses_fresh_cache(tmp_path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "id": "event-1",
                    "sport_key": "soccer_epl",
                    "home_team": "Arsenal",
                    "away_team": "Chelsea",
                    "commence_time": "2026-08-12T15:00:00Z",
                    "bookmakers": [],
                }
            ],
        )

    client = TheOddsApiClient(
        api_key="secret-key",
        cache_dir=tmp_path,
        cache_ttl_seconds=300,
    )
    client.http = httpx.Client(
        base_url="https://api.the-odds-api.com/v4",
        transport=httpx.MockTransport(handler),
    )

    first = client.fetch_odds(sport="soccer_epl", regions="uk,eu", markets="h2h")
    second = client.fetch_odds(sport="soccer_epl", regions="uk,eu", markets="h2h")

    assert first == second
    assert calls == 1
