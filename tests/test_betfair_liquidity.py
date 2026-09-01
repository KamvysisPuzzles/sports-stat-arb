from __future__ import annotations

import csv
from datetime import datetime, timezone

from exchange_scanner.betfair_liquidity import (
    BETFAIR_SOCCER_EVENT_TYPE_ID,
    BetfairLiquidityClient,
    _name_score,
    enrich_opportunities_csv,
    match_liquidity,
)


class FakeBetfairClient:
    def fetch_market_catalogue(self, **kwargs):
        return [
            {
                "marketId": "1.234",
                "event": {"name": "Arsenal v Chelsea"},
                "runners": [
                    {"selectionId": 101, "runnerName": "Arsenal"},
                    {"selectionId": 102, "runnerName": "Chelsea"},
                    {"selectionId": 103, "runnerName": "The Draw"},
                ],
            }
        ]

    def fetch_market_books(self, market_ids):
        return [
            {
                "marketId": market_ids[0],
                "runners": [
                    {
                        "selectionId": 102,
                        "ex": {
                            "availableToBack": [
                                {"price": 5.2, "size": 12.5},
                                {"price": 5.1, "size": 20.0},
                                {"price": 5.0, "size": 30.0},
                            ],
                            "availableToLay": [
                                {"price": 5.4, "size": 10.0},
                                {"price": 5.5, "size": 40.0},
                            ],
                        },
                    }
                ],
            }
        ]


def test_match_liquidity_finds_betfair_runner_and_sums_available_at_target() -> None:
    match = match_liquidity(
        FakeBetfairClient(),
        event_name="Arsenal v Chelsea",
        commence_time="unused",
        market_key="h2h",
        outcome_name="Chelsea",
        target_odds=5.1,
    )

    assert match.betfair_market_id == "1.234"
    assert match.betfair_selection_id == 102
    assert match.best_back_odds == 5.2
    assert match.best_back_available == 12.5
    assert match.available_at_or_above_target == 32.5
    assert match.best_lay_odds == 5.4
    assert match.best_lay_available == 10
    assert match.liquidity_status == "available"


def test_match_liquidity_handles_common_betfair_team_abbreviations() -> None:
    class AbbreviatedTeamClient(FakeBetfairClient):
        def fetch_market_catalogue(self, **kwargs):
            return [
                {
                    "marketId": "1.261450247",
                    "event": {"name": "Man City v Coventry"},
                    "runners": [
                        {"selectionId": 301, "runnerName": "Man City"},
                        {"selectionId": 302, "runnerName": "Coventry"},
                        {"selectionId": 303, "runnerName": "The Draw"},
                    ],
                }
            ]

        def fetch_market_books(self, market_ids):
            return [
                {
                    "marketId": market_ids[0],
                    "runners": [
                        {
                            "selectionId": 301,
                            "ex": {
                                "availableToBack": [{"price": 1.2, "size": 50.0}],
                                "availableToLay": [{"price": 1.21, "size": 40.0}],
                            },
                        }
                    ],
                }
            ]

    match = match_liquidity(
        AbbreviatedTeamClient(),
        event_name="Manchester City v Coventry City",
        commence_time="unused",
        market_key="h2h",
        outcome_name="Manchester City",
        target_odds=1.2,
    )

    assert match.betfair_market_id == "1.261450247"
    assert match.betfair_selection_id == 301
    assert match.liquidity_status == "available"


def test_match_liquidity_handles_west_ham_and_psg_aliases() -> None:
    class AliasClient(FakeBetfairClient):
        def __init__(self, event_name: str, runner_name: str) -> None:
            self.event_name = event_name
            self.runner_name = runner_name

        def fetch_market_catalogue(self, **kwargs):
            return [
                {
                    "marketId": "1.261",
                    "event": {"name": self.event_name},
                    "runners": [
                        {"selectionId": 401, "runnerName": self.runner_name},
                        {"selectionId": 402, "runnerName": "The Draw"},
                    ],
                }
            ]

        def fetch_market_books(self, market_ids):
            return [
                {
                    "marketId": market_ids[0],
                    "runners": [
                        {
                            "selectionId": 401,
                            "ex": {
                                "availableToBack": [{"price": 1.4, "size": 50.0}],
                                "availableToLay": [{"price": 1.42, "size": 40.0}],
                            },
                        }
                    ],
                }
            ]

    west_ham = match_liquidity(
        AliasClient("West Ham v Derby", "West Ham"),
        event_name="West Ham United v Derby County",
        commence_time="unused",
        market_key="h2h",
        outcome_name="West Ham United",
        target_odds=1.4,
    )
    psg = match_liquidity(
        AliasClient("Paris St-G v Monaco", "Paris St-G"),
        event_name="Paris Saint Germain v AS Monaco",
        commence_time="unused",
        market_key="h2h",
        outcome_name="Paris Saint Germain",
        target_odds=1.4,
    )

    assert west_ham.betfair_market_id == "1.261"
    assert west_ham.betfair_selection_id == 401
    assert psg.betfair_market_id == "1.261"
    assert psg.betfair_selection_id == 401


def test_betfair_catalogue_lookup_is_restricted_to_soccer() -> None:
    class RecordingClient(BetfairLiquidityClient):
        def __init__(self) -> None:
            self.params = None

        def _rpc(self, method, params):
            self.params = params
            return []

    client = RecordingClient()

    client.fetch_market_catalogue(
        event_name="West Ham United v Derby County",
        commence_time=datetime(2026, 9, 5, 14, tzinfo=timezone.utc),
        market_key="h2h",
    )

    assert client.params["filter"]["eventTypeIds"] == [BETFAIR_SOCCER_EVENT_TYPE_ID]


def test_name_score_handles_token_subset_variants_without_overmatching() -> None:
    assert _name_score("FC Schalke 04", "Schalke") >= 0.95
    assert _name_score("Real Madrid CF", "Real Madrid") >= 0.95
    assert _name_score("Manchester United", "Manchester City") < 0.70


def test_match_liquidity_maps_half_goal_total_to_betfair_market_type() -> None:
    class RecordingTotalsClient(FakeBetfairClient):
        def __init__(self) -> None:
            self.market_keys = []

        def fetch_market_catalogue(self, **kwargs):
            self.market_keys.append(kwargs["market_key"])
            return [
                {
                    "marketId": "1.999",
                    "event": {"name": "Arsenal v Chelsea"},
                    "runners": [
                        {"selectionId": 201, "runnerName": "Over 2.5 Goals"},
                        {"selectionId": 202, "runnerName": "Under 2.5 Goals"},
                    ],
                }
            ]

        def fetch_market_books(self, market_ids):
            return [
                {
                    "marketId": market_ids[0],
                    "runners": [
                        {
                            "selectionId": 201,
                            "ex": {
                                "availableToBack": [{"price": 2.1, "size": 50.0}],
                                "availableToLay": [{"price": 2.14, "size": 20.0}],
                            },
                        }
                    ],
                }
            ]

    client = RecordingTotalsClient()
    match = match_liquidity(
        client,
        event_name="Arsenal v Chelsea",
        commence_time="unused",
        market_key="totals",
        outcome_name="Over 2.5",
        target_odds=2.0,
    )

    assert client.market_keys == ["OVER_UNDER_25"]
    assert match.betfair_market_id == "1.999"
    assert match.betfair_selection_id == 201
    assert match.available_at_or_above_target == 50.0


def test_match_liquidity_rejects_total_lines_without_direct_betfair_market_type() -> None:
    class BlockedClient(FakeBetfairClient):
        def fetch_market_catalogue(self, **kwargs):
            raise AssertionError("unsupported markets should not call Betfair")

    match = match_liquidity(
        BlockedClient(),
        event_name="Arsenal v Chelsea",
        commence_time="unused",
        market_key="totals",
        outcome_name="Over 2.25",
        target_odds=2.0,
    )

    assert match.liquidity_status == "betfair_unsupported_market"


def test_enrich_opportunities_csv_marks_betfair_not_configured(tmp_path) -> None:
    input_csv = tmp_path / "opportunities.csv"
    output_csv = tmp_path / "with_liquidity.csv"
    input_csv.write_text(
        "event_name,commence_time,market,outcome_name,target_bookmaker,target_odds\n"
        "Arsenal v Chelsea,2026-08-14T12:00:00+00:00,h2h,Chelsea,Betfair,5.1\n",
        encoding="utf-8",
    )

    enrich_opportunities_csv(
        opportunities_csv=input_csv,
        output_csv=output_csv,
        client=None,
    )

    rows = list(csv.DictReader(output_csv.open(encoding="utf-8")))
    assert rows[0]["liquidity_status"] == "betfair_not_configured"
    assert rows[0]["available_at_or_above_target"] == "0.00"
