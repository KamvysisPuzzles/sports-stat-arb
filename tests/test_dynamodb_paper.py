from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from exchange_scanner.dynamodb_paper import log_signals_to_dynamodb, paper_item, trade_id
from exchange_scanner.the_odds_api import ValueSignal


class ConditionalCheckFailedException(Exception):
    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    def __init__(self) -> None:
        self.items = {}

    def put_item(self, *, Item, ConditionExpression):
        assert ConditionExpression == "attribute_not_exists(trade_id)"
        if Item["trade_id"] in self.items:
            raise ConditionalCheckFailedException()
        self.items[Item["trade_id"]] = Item


def signal() -> ValueSignal:
    return ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time=datetime(2026, 8, 15, 15, tzinfo=timezone.utc),
        market_key="h2h",
        outcome_name="Arsenal",
        target_bookmaker="Matchbook",
        target_odds=4.2,
        target_effective_odds=4.136,
        reference_fair_odds=4.0,
        reference_probability=0.25,
        edge=0.034,
        reference_bookmakers=("Pinnacle", "Smarkets"),
    )


def test_paper_item_uses_deterministic_trade_id_and_decimal_values() -> None:
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    item = paper_item(
        signal(),
        stake=1,
        logged_at=logged_at,
        liquidity={
            "liquidity_status": "available",
            "available_at_or_above_target": "25.50",
        },
    )

    assert item["trade_id"] == trade_id(signal())
    assert item["target_odds"] == Decimal("4.2")
    assert item["available_at_or_above_target"] == Decimal("25.5")
    assert item["status"] == "open"
    assert item["execution_mode"] == "paper"


def test_log_signals_to_dynamodb_dedupes_existing_trade_ids() -> None:
    table = FakeTable()
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)

    first = log_signals_to_dynamodb(table, [signal()], stake=1, logged_at=logged_at)
    second = log_signals_to_dynamodb(table, [signal()], stake=1, logged_at=logged_at)

    assert first.inserted == 1
    assert first.duplicates == 0
    assert second.inserted == 0
    assert second.duplicates == 1
    assert len(table.items) == 1
