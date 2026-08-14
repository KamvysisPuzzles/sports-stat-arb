from __future__ import annotations

import csv

from exchange_scanner.betfair_liquidity import (
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
