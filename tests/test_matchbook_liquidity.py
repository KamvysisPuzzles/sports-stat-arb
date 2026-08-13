from __future__ import annotations

import csv

import pytest

from exchange_scanner.matchbook_liquidity import enrich_opportunities_csv, match_liquidity


def matchbook_events():
    return [
        {
            "id": 1,
            "name": "Grimsby Town vs Exeter City",
            "markets": [
                {
                    "id": 10,
                    "name": "Match Odds",
                    "market-type": "one_x_two",
                    "product": "EXCHANGE",
                    "status": "open",
                    "runners": [
                        {
                            "id": 100,
                            "name": "Exeter City",
                            "prices": [
                                {
                                    "side": "back",
                                    "decimal-odds": 4.9,
                                    "available-amount": 42.5,
                                },
                                {
                                    "side": "back",
                                    "decimal-odds": 4.8,
                                    "available-amount": 100,
                                },
                                {
                                    "side": "lay",
                                    "decimal-odds": 5.1,
                                    "available-amount": 55,
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    ]


def test_match_liquidity_finds_runner_and_available_amount() -> None:
    match = match_liquidity(
        matchbook_events(),
        event_name="Grimsby Town v Exeter City",
        outcome_name="Exeter City",
        target_odds=4.9,
    )

    assert match.liquidity_status == "available"
    assert match.matchbook_event_id == 1
    assert match.matchbook_market_id == 10
    assert match.matchbook_runner_id == 100
    assert match.best_back_odds == 4.9
    assert match.best_back_available == pytest.approx(42.5)
    assert match.available_at_or_above_target == pytest.approx(42.5)
    assert match.best_lay_odds == 5.1
    assert match.back_lay_spread_pct == pytest.approx(0.04)


def test_match_liquidity_marks_missing_target_price() -> None:
    match = match_liquidity(
        matchbook_events(),
        event_name="Grimsby Town v Exeter City",
        outcome_name="Exeter City",
        target_odds=5.0,
    )

    assert match.liquidity_status == "price_not_available"
    assert match.available_at_or_above_target == 0


def test_enrich_opportunities_csv_appends_liquidity_columns(tmp_path) -> None:
    input_csv = tmp_path / "opportunities.csv"
    output_csv = tmp_path / "enriched.csv"
    input_csv.write_text(
        "event_name,outcome_name,target_odds\n"
        "Grimsby Town v Exeter City,Exeter City,4.9\n"
    )

    enrich_opportunities_csv(
        opportunities_csv=input_csv,
        output_csv=output_csv,
        events=matchbook_events(),
    )

    rows = list(csv.DictReader(output_csv.open()))
    assert rows[0]["liquidity_status"] == "available"
    assert rows[0]["available_at_or_above_target"] == "42.50"
