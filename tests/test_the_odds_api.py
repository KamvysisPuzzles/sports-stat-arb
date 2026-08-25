from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from exchange_scanner.the_odds_api import (
    TheOddsApiClient,
    TheOddsApiError,
    betfair_top_of_book_fair_odds,
    bookmaker_url,
    effective_decimal_odds,
    find_value_opportunities,
    h2h_winners_from_scores,
    lay_edge_per_liability,
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


def test_normalise_odds_api_events_skips_wide_exchange_back_prices() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "betfair_ex_uk",
                    "title": "Betfair",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.02},
                                {"name": "Chelsea", "price": 2.0},
                                {"name": "Draw", "price": 1.14},
                            ],
                        },
                        {
                            "key": "h2h_lay",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.08},
                                {"name": "Chelsea", "price": 2.1},
                                {"name": "Draw", "price": 44.0},
                            ],
                        },
                    ],
                }
            ],
        }
    ]

    prices = normalise_odds_api_events(events)

    assert [price.outcome_name for price in prices] == ["Arsenal", "Chelsea"]
    assert {price.market_key for price in prices} == {"h2h"}
    arsenal = next(price for price in prices if price.outcome_name == "Arsenal")
    assert arsenal.exchange_lay_odds == 2.08
    assert arsenal.exchange_spread_pct == pytest.approx(0.02926829)


def test_betfair_top_of_book_fair_odds_uses_probability_midpoint() -> None:
    fair_odds = betfair_top_of_book_fair_odds(4.0, 4.1)

    assert fair_odds == pytest.approx(4.0493827)


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


def test_finds_matchbook_lay_value_against_reference_market() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "matchbook",
                    "title": "Matchbook",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.0},
                                {"name": "Chelsea", "price": 2.0},
                            ],
                        },
                        {
                            "key": "h2h_lay",
                            "outcomes": [
                                {"name": "Arsenal", "price": 1.8},
                                {"name": "Chelsea", "price": 2.2},
                            ],
                        },
                    ],
                },
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
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
                    "key": "smarkets",
                    "title": "Smarkets",
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
        target_bookmakers={"matchbook"},
        target_lay_bookmakers={"matchbook"},
        reference_bookmakers={"pinnacle", "smarkets"},
        min_edge=0.02,
        max_age_seconds=300,
        min_reference_books=2,
        target_commission_rates={"matchbook": 0.02},
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.bet_side == "lay"
    assert signal.outcome_name == "Arsenal"
    assert signal.target_odds == pytest.approx(1.8)
    assert signal.reference_fair_odds == pytest.approx(2.0)
    assert signal.edge == pytest.approx(
        lay_edge_per_liability(
            lay_odds=1.8,
            fair_probability=0.5,
            commission_rate=0.02,
        )
    )


def test_finds_smarkets_lay_value_against_reference_market() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "smarkets",
                    "title": "Smarkets",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.0},
                                {"name": "Chelsea", "price": 2.0},
                            ],
                        },
                        {
                            "key": "h2h_lay",
                            "outcomes": [
                                {"name": "Arsenal", "price": 1.8},
                                {"name": "Chelsea", "price": 2.2},
                            ],
                        },
                    ],
                },
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
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
                    "key": "matchbook",
                    "title": "Matchbook",
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
        target_bookmakers={"smarkets"},
        target_lay_bookmakers={"smarkets"},
        reference_bookmakers={"pinnacle", "matchbook"},
        min_edge=0.02,
        max_age_seconds=300,
        min_reference_books=2,
        target_commission_rates={"smarkets": 0.02},
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.target_bookmaker == "Smarkets"
    assert signal.bet_side == "lay"
    assert signal.outcome_name == "Arsenal"
    assert signal.target_odds == pytest.approx(1.8)
    assert signal.edge == pytest.approx(
        lay_edge_per_liability(
            lay_odds=1.8,
            fair_probability=0.5,
            commission_rate=0.02,
        )
    )


def test_value_mode_can_apply_target_book_commission_to_edge() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "matchbook",
                    "title": "Matchbook",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 4.03},
                                {"name": "Chelsea", "price": 1.2},
                            ],
                        }
                    ],
                },
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 4.0},
                                {"name": "Chelsea", "price": 1.3333333333},
                            ],
                        }
                    ],
                },
                {
                    "key": "smarkets",
                    "title": "Smarkets",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 4.0},
                                {"name": "Chelsea", "price": 1.3333333333},
                            ],
                        }
                    ],
                },
            ],
        }
    ]

    prices = normalise_odds_api_events(events)
    signals_without_commission = find_value_opportunities(
        prices,
        target_bookmakers={"matchbook"},
        reference_bookmakers={"pinnacle", "smarkets"},
        min_edge=0.0,
        max_age_seconds=300,
        min_reference_books=2,
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )
    signals_with_commission = find_value_opportunities(
        prices,
        target_bookmakers={"matchbook"},
        reference_bookmakers={"pinnacle", "smarkets"},
        min_edge=0.0,
        max_age_seconds=300,
        min_reference_books=2,
        target_commission_rates={"matchbook": 0.02},
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert effective_decimal_odds(4.0, 0.02) == pytest.approx(3.94)
    assert signals_without_commission[0].target_odds == pytest.approx(4.03)
    assert signals_without_commission[0].effective_odds == pytest.approx(4.03)
    assert signals_without_commission[0].edge == pytest.approx(0.0075)
    assert signals_with_commission == []


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


def test_spreads_devig_each_point_line_separately() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "basketball_wnba",
            "home_team": "Mystics",
            "away_team": "Dream",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "matchbook",
                    "title": "Matchbook",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Mystics", "price": 2.2, "point": -1.5},
                                {"name": "Dream", "price": 1.8, "point": 1.5},
                                {"name": "Mystics", "price": 1.4, "point": -3.5},
                                {"name": "Dream", "price": 2.6, "point": 3.5},
                            ],
                        }
                    ],
                },
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Mystics", "price": 2.0, "point": -1.5},
                                {"name": "Dream", "price": 2.0, "point": 1.5},
                                {"name": "Mystics", "price": 1.5, "point": -3.5},
                                {"name": "Dream", "price": 2.5, "point": 3.5},
                            ],
                        }
                    ],
                },
                {
                    "key": "smarkets",
                    "title": "Smarkets",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Mystics", "price": 2.0, "point": -1.5},
                                {"name": "Dream", "price": 2.0, "point": 1.5},
                                {"name": "Mystics", "price": 1.5, "point": -3.5},
                                {"name": "Dream", "price": 2.5, "point": 3.5},
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
        target_bookmakers={"matchbook"},
        reference_bookmakers={"pinnacle", "smarkets"},
        min_edge=0.05,
        max_age_seconds=300,
        min_reference_books=2,
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert [(signal.outcome_name, round(signal.edge, 2)) for signal in signals] == [
        ("Mystics -1.5", 0.1)
    ]


def test_totals_can_use_poisson_conversion_from_nearby_reference_line() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "matchbook",
                    "title": "Matchbook",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 2.4, "point": 2.5},
                                {"name": "Under", "price": 1.7, "point": 2.5},
                            ],
                        }
                    ],
                },
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 1.91, "point": 2.25},
                                {"name": "Under", "price": 1.91, "point": 2.25},
                            ],
                        }
                    ],
                },
            ],
        }
    ]

    signals = find_value_opportunities(
        normalise_odds_api_events(events),
        target_bookmakers={"matchbook"},
        reference_bookmakers={"pinnacle"},
        min_edge=0.02,
        max_age_seconds=300,
        min_reference_books=1,
        target_commission_rates={"matchbook": 0.02},
        poisson_total_conversion=True,
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert [(signal.outcome_name, round(signal.edge, 3)) for signal in signals] == [
        ("Over 2.5", 0.032)
    ]
    assert signals[0].reference_bookmakers == ("Pinnacle",)


def test_totals_poisson_conversion_counts_multiple_lines_as_one_reference_book() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "matchbook",
                    "title": "Matchbook",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 2.4, "point": 2.5},
                                {"name": "Under", "price": 1.7, "point": 2.5},
                            ],
                        }
                    ],
                },
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 1.91, "point": 2.25},
                                {"name": "Under", "price": 1.91, "point": 2.25},
                                {"name": "Over", "price": 2.05, "point": 2.75},
                                {"name": "Under", "price": 1.78, "point": 2.75},
                            ],
                        }
                    ],
                },
            ],
        }
    ]

    signals = find_value_opportunities(
        normalise_odds_api_events(events),
        target_bookmakers={"matchbook"},
        reference_bookmakers={"pinnacle"},
        min_edge=0,
        max_age_seconds=300,
        min_reference_books=2,
        poisson_total_conversion=True,
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert signals == []


def test_totals_poisson_conversion_ignores_distant_reference_lines() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "matchbook",
                    "title": "Matchbook",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 3.0, "point": 2.5},
                                {"name": "Under", "price": 1.4, "point": 2.5},
                            ],
                        }
                    ],
                },
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 1.91, "point": 3.5},
                                {"name": "Under", "price": 1.91, "point": 3.5},
                            ],
                        }
                    ],
                },
            ],
        }
    ]

    signals = find_value_opportunities(
        normalise_odds_api_events(events),
        target_bookmakers={"matchbook"},
        reference_bookmakers={"pinnacle"},
        min_edge=0,
        max_age_seconds=300,
        min_reference_books=1,
        poisson_total_conversion=True,
        poisson_total_max_line_distance=0.5,
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


def test_sharp_only_value_can_use_other_target_books_as_references() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "betfair",
                    "title": "Betfair",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.2},
                                {"name": "Chelsea", "price": 1.8},
                            ],
                        }
                    ],
                },
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
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
                    "key": "smarkets",
                    "title": "Smarkets",
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
        target_bookmakers={"betfair", "pinnacle", "smarkets"},
        reference_bookmakers={"betfair", "pinnacle", "smarkets"},
        min_edge=0.05,
        max_age_seconds=300,
        min_reference_books=2,
        allow_target_bookmakers_as_references=True,
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    betfair_signal = next(
        signal
        for signal in signals
        if signal.target_bookmaker == "Betfair" and signal.outcome_name == "Arsenal"
    )
    assert betfair_signal.reference_bookmakers == ("Pinnacle", "Smarkets")


def test_target_venue_fair_value_can_be_weighted_into_reference_consensus() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "betfair_ex_uk",
                    "title": "Betfair",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.2},
                                {"name": "Chelsea", "price": 1.8},
                            ],
                        },
                        {
                            "key": "h2h_lay",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.3},
                                {"name": "Chelsea", "price": 1.9},
                            ],
                        },
                    ],
                },
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
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
                    "key": "smarkets",
                    "title": "Smarkets",
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
        target_bookmakers={"betfair_ex_uk"},
        reference_bookmakers=None,
        min_edge=0.02,
        max_age_seconds=300,
        min_reference_books=2,
        allow_target_bookmakers_as_references=True,
        reference_weights={"pinnacle": 1.0, "smarkets": 1.0, "target venue fair value": 3.0},
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert len(signals) == 1
    assert signals[0].reference_bookmakers == (
        "Pinnacle",
        "Smarkets",
        "Target Venue Fair Value",
    )
    assert signals[0].betfair_fair_odds == pytest.approx(2.2488889)
    assert signals[0].reference_probability == pytest.approx(0.4706965)
    assert signals[0].edge == pytest.approx(0.03553236)


def test_target_venue_fair_value_falls_back_to_h2h_price_without_lay() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "betfair_ex_uk",
                    "title": "Betfair",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.2},
                                {"name": "Chelsea", "price": 1.8},
                            ],
                        }
                    ],
                },
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
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
                    "key": "smarkets",
                    "title": "Smarkets",
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
        target_bookmakers={"betfair_ex_uk"},
        reference_bookmakers=None,
        min_edge=0.01,
        max_age_seconds=300,
        min_reference_books=2,
        allow_target_bookmakers_as_references=True,
        reference_weights={"pinnacle": 1.0, "smarkets": 1.0, "target venue fair value": 3.0},
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert len(signals) == 1
    assert signals[0].betfair_fair_odds == pytest.approx(2.2)
    assert signals[0].reference_bookmakers == (
        "Pinnacle",
        "Smarkets",
        "Target Venue Fair Value",
    )


def test_value_mode_supports_sharpness_weighted_reference_consensus() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "commence_time": "2026-08-12T15:00:00Z",
            "bookmakers": [
                {
                    "key": "betfair",
                    "title": "Betfair",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.1},
                                {"name": "Chelsea", "price": 1.8},
                            ],
                        }
                    ],
                },
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
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
                    "key": "weak",
                    "title": "Weak Book",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 1.5},
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
        target_bookmakers={"betfair"},
        reference_bookmakers=None,
        min_edge=0.04,
        max_age_seconds=300,
        min_reference_books=2,
        allow_target_bookmakers_as_references=True,
        reference_weights={"pinnacle": 1.0, "weak book": 0.0, "*": 1.0},
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert len(signals) == 1
    assert signals[0].target_bookmaker == "Betfair"
    assert signals[0].reference_probability == pytest.approx(0.5)
    assert signals[0].edge == pytest.approx(0.05)
    assert signals[0].reference_bookmakers == ("Pinnacle", "Weak Book")


def test_value_mode_supports_median_devigged_reference_consensus() -> None:
    events = [
        {
            "id": "event-1",
            "sport_key": "soccer_epl",
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
                                {"name": "Arsenal", "price": 2.04},
                                {"name": "Chelsea", "price": 1.9},
                            ],
                        }
                    ],
                },
                {
                    "key": "ref_a",
                    "title": "Ref A",
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
                    "title": "Ref B",
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
                    "key": "ref_outlier",
                    "title": "Ref Outlier",
                    "last_update": "2026-08-12T12:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 1.1111111111},
                                {"name": "Chelsea", "price": 10.0},
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
        reference_bookmakers={"ref_a", "ref_b", "ref_outlier"},
        min_edge=0.01,
        max_age_seconds=300,
        min_reference_books=3,
        reference_aggregation="median",
        now=datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc),
    )

    assert len(signals) == 1
    assert signals[0].outcome_name == "Arsenal"
    assert signals[0].reference_probability == pytest.approx(0.5)
    assert signals[0].reference_fair_odds == pytest.approx(2.0)
    assert signals[0].edge == pytest.approx(0.02)
    assert signals[0].reference_bookmakers == ("Ref A", "Ref B", "Ref Outlier")


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
