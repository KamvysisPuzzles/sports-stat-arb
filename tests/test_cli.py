from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone

import pytest

from exchange_scanner.bookmaker_links import EventPageResolution
from exchange_scanner.cli import (
    ACTIVE_H2H_PROFILE,
    EXCHANGE_CLV_TARGET_BOOKMAKERS,
    MATCHBOOK_TARGET_BOOKMAKERS,
    SHARP_REFERENCE_BOOKMAKERS,
    SHARPNESS_WEIGHTS,
    SPORT_PROFILES,
    STRATEGIES,
    _american_odds,
    _filter_prices_by_event_horizon,
    _filter_signals_by_max_edge,
    _filter_signals_by_strategy_rules,
    _fractional_odds,
    _recommended_value_stake,
    _sport_keys,
    _unique_bet_signals,
    _unique_event_signals,
    active_h2h_sports,
    scan_the_odds_api,
    write_value_csv,
)
from exchange_scanner.the_odds_api import ValueSignal


def test_sharp_profile_adds_reference_books() -> None:
    assert SHARP_REFERENCE_BOOKMAKERS == {
        "pinnacle",
        "betfair",
        "smarkets",
        "matchbook",
    }


def test_sharp_weighted_clv_targets_sharp_books_against_weighted_all_book_consensus() -> None:
    strategy = STRATEGIES["sharp-weighted-clv"]

    assert strategy["target_bookmakers"] == MATCHBOOK_TARGET_BOOKMAKERS
    assert strategy["reference_bookmakers"] is None
    assert strategy["allow_target_bookmakers_as_references"] is True
    assert strategy["reference_weights"] == SHARPNESS_WEIGHTS
    assert set(STRATEGIES) == {"exchange-clv", "sharp-weighted-clv"}


def test_exchange_clv_targets_available_exchange_books() -> None:
    strategy = STRATEGIES["exchange-clv"]

    assert strategy["target_bookmakers"] == EXCHANGE_CLV_TARGET_BOOKMAKERS
    assert strategy["target_bookmakers"] == {
        "betfair",
        "matchbook",
        "smarkets",
        "betfair_ex_uk",
        "betfair_ex_eu",
    }
    assert strategy["reference_bookmakers"] is None
    assert strategy["allow_target_bookmakers_as_references"] is True
    assert strategy["target_commission_rates"]["betfair"] == pytest.approx(0.02)
    assert strategy["reference_weights"] == SHARPNESS_WEIGHTS
    assert strategy["min_sharp_reference_books"] == 1


def test_matchbook_h2h_expanded_profile_excludes_futures_and_outrights() -> None:
    sports = SPORT_PROFILES["matchbook-h2h-expanded"]

    assert "americanfootball_nfl" in sports
    assert "baseball_mlb" in sports
    assert "basketball_nba" in sports
    assert "icehockey_nhl" in sports
    assert "soccer_league_of_ireland" in sports
    assert "soccer_switzerland_superleague" in sports
    assert "tennis_atp_cincinnati_open" in sports
    assert all("winner" not in sport for sport in sports)
    assert len(sports) <= 80

def test_sports_profile_combines_with_explicit_sports_without_duplicates() -> None:
    args = Namespace(
        sports_profile="matchbook-h2h-expanded",
        sports="soccer_efl_champ,soccer_epl",
        sport="football",
    )

    sports = _sport_keys(args)

    assert sports.count("soccer_efl_champ") == 1
    assert "soccer_epl" in sports


def test_active_h2h_profile_keeps_active_non_outright_sports() -> None:
    sports = active_h2h_sports(
        [
            {"key": "basketball_nba", "active": True},
            {"key": "basketball_nba_championship_winner", "active": True},
            {"key": "politics_us_presidential_election_winner", "active": True},
            {"key": "soccer_uefa_champs_league", "active": False},
        ]
    )

    assert sports == ["basketball_nba"]


def test_active_h2h_profile_uses_live_sports_client() -> None:
    class FakeOddsClient:
        def fetch_sports(self):
            return [
                {"key": "basketball_nba", "active": True},
                {"key": "baseball_mlb_world_series_winner", "active": True},
            ]

    args = Namespace(
        sports_profile=ACTIVE_H2H_PROFILE,
        sports="soccer_epl,basketball_nba",
        sport="football",
    )

    sports = _sport_keys(args, odds_client=FakeOddsClient())

    assert sports == ["basketball_nba", "soccer_epl"]


def test_unique_event_signals_keeps_highest_edge_per_event() -> None:
    first = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Arsenal",
        target_bookmaker="Book A",
        target_odds=2.2,
        reference_fair_odds=2.0,
        reference_probability=0.5,
        edge=0.1,
        reference_bookmakers=("Pinnacle", "Betfair"),
    )
    second = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Draw",
        target_bookmaker="Book B",
        target_odds=4.0,
        reference_fair_odds=3.8,
        reference_probability=0.2632,
        edge=0.0528,
        reference_bookmakers=("Pinnacle", "Betfair"),
    )
    third = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-2",
        event_name="Liverpool v Everton",
        commence_time="2026-08-12T17:00:00Z",
        market_key="h2h",
        outcome_name="Everton",
        target_bookmaker="Book A",
        target_odds=5.0,
        reference_fair_odds=4.7,
        reference_probability=0.2128,
        edge=0.064,
        reference_bookmakers=("Pinnacle", "Betfair"),
    )

    unique = _unique_event_signals([first, second, third])

    assert unique == [first, third]


def test_unique_bet_signals_keeps_best_price_for_same_bet() -> None:
    lower_price = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Arsenal",
        target_bookmaker="Smarkets",
        target_odds=2.1,
        target_effective_odds=2.078,
        reference_fair_odds=2.0,
        reference_probability=0.5,
        edge=0.039,
        reference_bookmakers=("Pinnacle", "Betfair"),
    )
    best_price = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Arsenal",
        target_bookmaker="Betfair",
        target_odds=2.2,
        target_effective_odds=2.176,
        reference_fair_odds=2.0,
        reference_probability=0.5,
        edge=0.088,
        reference_bookmakers=("Pinnacle", "Smarkets"),
    )
    different_outcome = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Draw",
        target_bookmaker="Matchbook",
        target_odds=4.0,
        reference_fair_odds=3.7,
        reference_probability=0.2703,
        edge=0.081,
        reference_bookmakers=("Pinnacle", "Betfair"),
    )

    unique = _unique_bet_signals([lower_price, best_price, different_outcome])

    assert unique == [best_price, different_outcome]


def test_filter_signals_by_max_edge_excludes_suspicious_high_edges() -> None:
    clean = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Arsenal",
        target_bookmaker="Book A",
        target_odds=2.1,
        reference_fair_odds=2.0,
        reference_probability=0.5,
        edge=0.05,
        reference_bookmakers=("Pinnacle", "Betfair"),
    )
    suspicious = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-2",
        event_name="Liverpool v Everton",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Everton",
        target_bookmaker="Book A",
        target_odds=3.0,
        reference_fair_odds=2.0,
        reference_probability=0.5,
        edge=0.5,
        reference_bookmakers=("Pinnacle", "Betfair"),
    )

    assert _filter_signals_by_max_edge([suspicious, clean], max_edge=0.10) == [clean]


def test_filter_strategy_rules_can_reject_wide_betfair_spreads() -> None:
    accepted = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Arsenal",
        target_bookmaker="Betfair",
        target_odds=4.2,
        reference_fair_odds=4.0,
        reference_probability=0.25,
        edge=0.05,
        reference_bookmakers=("Pinnacle", "Smarkets"),
        betfair_fair_odds=4.1,
        betfair_fair_edge=0.02,
        betfair_back_lay_spread_pct=0.025,
    )
    wide = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-2",
        event_name="Liverpool v Everton",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Everton",
        target_bookmaker="Betfair",
        target_odds=5.0,
        reference_fair_odds=4.7,
        reference_probability=0.2128,
        edge=0.064,
        reference_bookmakers=("Pinnacle", "Smarkets"),
        betfair_fair_odds=4.9,
        betfair_fair_edge=0.03,
        betfair_back_lay_spread_pct=0.08,
    )

    assert _filter_signals_by_strategy_rules(
        [wide, accepted],
        {
            "max_betfair_spread_pct": 0.03,
        },
    ) == [accepted]


def test_filter_strategy_rules_do_not_apply_betfair_spread_to_matchbook() -> None:
    matchbook = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Arsenal",
        target_bookmaker="Matchbook",
        target_odds=4.2,
        reference_fair_odds=4.0,
        reference_probability=0.25,
        edge=0.05,
        reference_bookmakers=("Pinnacle", "Smarkets"),
    )
    betfair_wide = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-2",
        event_name="Liverpool v Everton",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Everton",
        target_bookmaker="Betfair",
        target_odds=5.0,
        reference_fair_odds=4.7,
        reference_probability=0.2128,
        edge=0.064,
        reference_bookmakers=("Pinnacle", "Smarkets"),
        betfair_back_lay_spread_pct=0.08,
    )

    assert _filter_signals_by_strategy_rules(
        [matchbook, betfair_wide],
        {"max_betfair_spread_pct": 0.03},
    ) == [matchbook]


def test_filter_strategy_rules_requires_sharp_reference_when_configured() -> None:
    sharp = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Arsenal",
        target_bookmaker="Betfair",
        target_odds=4.2,
        reference_fair_odds=4.0,
        reference_probability=0.25,
        edge=0.05,
        reference_bookmakers=("Pinnacle", "Soft Book"),
    )
    soft_only = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-2",
        event_name="Liverpool v Everton",
        commence_time="2026-08-12T15:00:00Z",
        market_key="h2h",
        outcome_name="Everton",
        target_bookmaker="Betfair",
        target_odds=5.0,
        reference_fair_odds=4.7,
        reference_probability=0.2128,
        edge=0.064,
        reference_bookmakers=("Soft Book",),
    )

    assert _filter_signals_by_strategy_rules(
        [soft_only, sharp],
        {"min_sharp_reference_books": 1},
    ) == [sharp]


def test_filter_prices_by_event_horizon_excludes_events_more_than_two_days_out() -> None:
    now = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    inside = Namespace(commence_time=datetime(2026, 8, 14, 12, tzinfo=timezone.utc))
    outside = Namespace(commence_time=datetime(2026, 8, 14, 12, 1, tzinfo=timezone.utc))

    prices = _filter_prices_by_event_horizon(
        [inside, outside],
        max_event_days=2,
        now=now,
    )

    assert prices == [inside]


def test_odds_format_helpers_show_fractional_and_american_prices() -> None:
    assert _fractional_odds(4.5) == "7/2"
    assert _fractional_odds(2.2) == "6/5"
    assert _american_odds(4.5) == "+350"
    assert _american_odds(1.5) == "-200"


def test_write_value_csv_falls_back_to_bookmaker_url_for_unresolved_pages(
    monkeypatch, capsys
) -> None:
    resolved = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time=datetime(2026, 8, 14, 15, tzinfo=timezone.utc),
        market_key="h2h",
        outcome_name="Arsenal",
        target_bookmaker="William Hill",
        target_odds=2.2,
        reference_fair_odds=2.0,
        reference_probability=0.5,
        edge=0.1,
        reference_bookmakers=("Pinnacle", "Betfair"),
    )
    unresolved = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-2",
        event_name="Liverpool v Everton",
        commence_time=datetime(2026, 8, 14, 17, tzinfo=timezone.utc),
        market_key="h2h",
        outcome_name="Liverpool",
        target_bookmaker="William Hill",
        target_odds=2.1,
        reference_fair_odds=2.0,
        reference_probability=0.5,
        edge=0.05,
        reference_bookmakers=("Pinnacle", "Betfair"),
    )

    def fake_resolve_event_page(*, bookmaker: str, event_name: str, selection: str):
        if event_name == resolved.event_name:
            return EventPageResolution(
                "resolved",
                url="https://sports.williamhill.com/betting/en-gb/football/arsenal-v-chelsea",
            )
        return EventPageResolution("not_found", reason="No bookmaker-owned event result found.")

    monkeypatch.setattr("exchange_scanner.cli.resolve_event_page", fake_resolve_event_page)
    args = Namespace(
        resolve_event_pages=True,
        bankroll=0,
        kelly_fraction=0.25,
        stake_cap_pct=0.005,
        max_stake=50,
    )

    write_value_csv([resolved, unresolved], args)

    output = capsys.readouterr().out
    assert "bet_to_place" in output
    assert "target_odds_fractional" in output
    assert "target_odds_american" in output
    assert "target_implied_probability" in output
    assert "reference_fair_odds_fractional" in output
    assert "Back Arsenal with William Hill at 2.2 (6/5)+" in output
    assert "Back Liverpool with William Hill at 2.1 (11/10)+" in output
    assert ",6/5,+120,45.45%," in output
    assert "Arsenal v Chelsea" in output
    assert "Liverpool v Everton" in output
    assert ",resolved," in output
    assert (
        "https://sports.williamhill.com/betting/en-gb,not_found,"
        "No bookmaker-owned event result found."
    ) in output


def test_recommended_value_stake_uses_capped_fractional_kelly() -> None:
    signal = ValueSignal(
        sport_key="cricket_the_hundred",
        event_id="event-1",
        event_name="Manchester Super Giants v Sunrisers Leeds",
        commence_time="2026-08-14T17:00:00Z",
        market_key="h2h",
        outcome_name="Sunrisers Leeds",
        target_bookmaker="Unibet (UK)",
        target_odds=2.15,
        reference_fair_odds=2.032,
        reference_probability=0.4921,
        edge=0.0581,
        reference_bookmakers=("Betfair", "Pinnacle", "Smarkets"),
    )
    args = Namespace(bankroll=1000, kelly_fraction=0.25, stake_cap_pct=0.005, max_stake=50)

    assert _recommended_value_stake(signal, args) == pytest.approx(5.0)


def test_scan_uses_matchbook_target_against_weighted_reference_consensus(tmp_path) -> None:
    fixture = tmp_path / "odds.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "id": "event-1",
                    "sport_key": "soccer_epl",
                    "home_team": "Arsenal",
                    "away_team": "Chelsea",
                    "commence_time": "2027-08-12T15:00:00Z",
                    "bookmakers": [
                        {
                            "key": "matchbook",
                            "title": "Matchbook",
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
                            "key": "betfair",
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
                            "key": "random_book",
                            "title": "Random Book",
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
        )
    )
    args = Namespace(
        fixtures=fixture,
        markets="h2h",
        min_edge=0.02,
        max_age_seconds=999999999,
        min_reference_books=2,
        include_started=False,
        unique_events=False,
        max_event_days=-1,
    )

    signals = scan_the_odds_api(args)

    assert len(signals) == 1
    assert signals[0].target_bookmaker == "Matchbook"
    assert signals[0].outcome_name == "Arsenal"
    assert signals[0].reference_bookmakers == ("Betfair", "Pinnacle", "Random Book")


def test_scan_aborts_before_fetching_when_request_cap_is_exceeded(monkeypatch) -> None:
    monkeypatch.setenv("THE_ODDS_API_KEY", "secret-key")
    args = Namespace(
        fixtures=None,
        sports="soccer_epl,soccer_spain_la_liga",
        sports_profile="",
        sport="soccer_epl",
        markets="h2h",
        regions="uk,eu",
        dry_run_estimate=False,
        max_api_requests=1,
    )

    with pytest.raises(SystemExit) as exc_info:
        scan_the_odds_api(args)

    assert "Refusing to make 2 The Odds API requests" in str(exc_info.value)


def test_dry_run_estimate_does_not_require_api_key(monkeypatch, capsys) -> None:
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    args = Namespace(
        fixtures=None,
        sports="soccer_epl,soccer_spain_la_liga",
        sports_profile="",
        sport="soccer_epl",
        markets="h2h",
        regions="uk,eu",
        dry_run_estimate=True,
    )

    with pytest.raises(SystemExit) as exc_info:
        scan_the_odds_api(args)

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["planned_odds_requests"] == 2
