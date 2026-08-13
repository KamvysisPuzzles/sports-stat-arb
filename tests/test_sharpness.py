from __future__ import annotations

import csv
from datetime import datetime, timezone

import pytest

from exchange_scanner.sharpness import (
    list_sharpness_weights,
    recompute_sharpness_weights,
    sharpness_weight_mapping,
    store_odds_snapshot,
    write_sharpness_weights_csv,
)
from exchange_scanner.the_odds_api import OutcomePrice


def price(
    *,
    bookmaker_key: str,
    bookmaker_title: str,
    outcome_name: str,
    odds: float,
    snapshot_day: int = 12,
) -> OutcomePrice:
    return OutcomePrice(
        bookmaker_key=bookmaker_key,
        bookmaker_title=bookmaker_title,
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time=datetime(2026, 8, 14, 15, tzinfo=timezone.utc),
        market_key="h2h",
        market_name="h2h",
        outcome_name=outcome_name,
        point=None,
        odds=odds,
        last_update=datetime(2026, 8, snapshot_day, 12, tzinfo=timezone.utc),
    )


def test_store_odds_snapshot_dedupes_rows(tmp_path) -> None:
    db_path = tmp_path / "markets.sqlite3"
    snapshot_time = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    prices = [
        price(bookmaker_key="pinnacle", bookmaker_title="Pinnacle", outcome_name="Arsenal", odds=2),
        price(bookmaker_key="pinnacle", bookmaker_title="Pinnacle", outcome_name="Chelsea", odds=2),
    ]

    assert store_odds_snapshot(db_path, prices, snapshot_time=snapshot_time) == 2
    assert store_odds_snapshot(db_path, prices, snapshot_time=snapshot_time) == 0


def test_recompute_sharpness_weights_scores_books_against_closing_benchmark(tmp_path) -> None:
    db_path = tmp_path / "markets.sqlite3"
    early_time = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    closing_time = datetime(2026, 8, 14, 14, 55, tzinfo=timezone.utc)
    early_prices = [
        price(bookmaker_key="pinnacle", bookmaker_title="Pinnacle", outcome_name="Arsenal", odds=2),
        price(bookmaker_key="pinnacle", bookmaker_title="Pinnacle", outcome_name="Chelsea", odds=2),
        price(bookmaker_key="weak", bookmaker_title="Weak Book", outcome_name="Arsenal", odds=1.5),
        price(bookmaker_key="weak", bookmaker_title="Weak Book", outcome_name="Chelsea", odds=3),
    ]
    closing_prices = [
        price(bookmaker_key="pinnacle", bookmaker_title="Pinnacle", outcome_name="Arsenal", odds=2),
        price(bookmaker_key="pinnacle", bookmaker_title="Pinnacle", outcome_name="Chelsea", odds=2),
        price(bookmaker_key="weak", bookmaker_title="Weak Book", outcome_name="Arsenal", odds=1.8),
        price(bookmaker_key="weak", bookmaker_title="Weak Book", outcome_name="Chelsea", odds=2.2),
    ]
    store_odds_snapshot(db_path, early_prices, snapshot_time=early_time)
    store_odds_snapshot(db_path, closing_prices, snapshot_time=closing_time)

    weights = recompute_sharpness_weights(
        db_path,
        benchmark_bookmakers={"pinnacle"},
        min_samples=1,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )

    by_book = {weight.bookmaker_identity: weight for weight in weights}
    assert by_book["pinnacle"].weight == pytest.approx(1)
    assert by_book["weak book"].weight < by_book["pinnacle"].weight
    assert list_sharpness_weights(db_path) == weights
    assert sharpness_weight_mapping(db_path)["pinnacle"] == pytest.approx(1)


def test_write_sharpness_weights_csv(tmp_path) -> None:
    db_path = tmp_path / "markets.sqlite3"
    csv_path = tmp_path / "weights.csv"
    snapshot_time = datetime(2026, 8, 14, 14, 55, tzinfo=timezone.utc)
    prices = [
        price(bookmaker_key="pinnacle", bookmaker_title="Pinnacle", outcome_name="Arsenal", odds=2),
        price(bookmaker_key="pinnacle", bookmaker_title="Pinnacle", outcome_name="Chelsea", odds=2),
    ]
    store_odds_snapshot(db_path, prices, snapshot_time=snapshot_time)
    weights = recompute_sharpness_weights(
        db_path,
        benchmark_bookmakers={"pinnacle"},
        min_samples=1,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )

    write_sharpness_weights_csv(weights, csv_path)

    rows = list(csv.DictReader(csv_path.open()))
    assert rows[0]["bookmaker_identity"] == "pinnacle"
    assert rows[0]["weight"] == "1.000000"
