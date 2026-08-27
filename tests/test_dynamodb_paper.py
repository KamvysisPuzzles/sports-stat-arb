from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from exchange_scanner.dynamodb_paper import (
    STRATEGY_REFERENCE_VERSION,
    log_signals_to_dynamodb,
    paper_item,
    settle_results_in_dynamodb,
    trade_id,
    update_closing_values_in_dynamodb,
)
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

    def scan(self, **kwargs):
        status = kwargs["ExpressionAttributeValues"][":open_status"]
        return {
            "Items": [
                item
                for item in self.items.values()
                if item.get("status") == status
            ]
        }

    def update_item(self, *, Key, UpdateExpression, ExpressionAttributeValues, **kwargs):
        item = self.items[Key["trade_id"]]
        if "closing_checked_at" in UpdateExpression:
            item["closing_checked_at"] = ExpressionAttributeValues[":checked_at"]
            item["closing_target_odds"] = ExpressionAttributeValues[":closing_target_odds"]
            item["target_clv"] = ExpressionAttributeValues[":target_clv"]
            item["beat_closing_line"] = ExpressionAttributeValues[":beat_closing_line"]
            item["closing_reference_fair_odds"] = ExpressionAttributeValues[
                ":closing_reference_fair_odds"
            ]
            item["closing_edge"] = ExpressionAttributeValues[":closing_edge"]
            item["positive_closing_edge"] = ExpressionAttributeValues[":positive_closing_edge"]
        else:
            item["status"] = ExpressionAttributeValues[":settled"]
            item["result"] = ExpressionAttributeValues[":result"]
            item["profit"] = ExpressionAttributeValues[":profit"]
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def signal(**overrides) -> ValueSignal:
    values = {
        "sport_key": "soccer_epl",
        "event_id": "event-1",
        "event_name": "Arsenal v Chelsea",
        "commence_time": datetime(2026, 8, 15, 15, tzinfo=timezone.utc),
        "market_key": "h2h",
        "outcome_name": "Arsenal",
        "target_bookmaker": "Matchbook",
        "target_odds": 4.2,
        "target_effective_odds": 4.136,
        "reference_fair_odds": 4.0,
        "reference_probability": 0.25,
        "edge": 0.034,
        "reference_bookmakers": ("Pinnacle", "Smarkets"),
    }
    values.update(overrides)
    return ValueSignal(**values)


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
    assert item["strategy_reference_version"] == STRATEGY_REFERENCE_VERSION
    assert item["status"] == "open"
    assert item["execution_mode"] == "paper"


def test_paper_item_logs_reference_diagnostics() -> None:
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    item = paper_item(
        signal(
            reference_fair_odds_by_bookmaker=(
                ("Betfair", 4.1),
                ("Pinnacle", 4.0),
            ),
            reference_spread_pct_by_bookmaker=(("Betfair", 0.02),),
            reference_last_update_by_bookmaker=(
                ("Betfair", "2026-08-14T11:59:00+00:00"),
                ("Pinnacle", "2026-08-14T12:00:00+00:00"),
            ),
            reference_disagreement_pct=0.0247,
            reference_max_spread_pct=0.02,
            reference_avg_spread_pct=0.02,
        ),
        stake=1,
        logged_at=logged_at,
    )

    assert item["reference_fair_odds_by_bookmaker"] == '{"Betfair":4.1,"Pinnacle":4.0}'
    assert item["reference_spread_pct_by_bookmaker"] == '{"Betfair":0.02}'
    assert item["reference_last_update_by_bookmaker"] == (
        '{"Betfair":"2026-08-14T11:59:00+00:00",'
        '"Pinnacle":"2026-08-14T12:00:00+00:00"}'
    )
    assert item["reference_disagreement_pct"] == Decimal("0.0247")
    assert item["reference_max_spread_pct"] == Decimal("0.02")
    assert item["reference_avg_spread_pct"] == Decimal("0.02")


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


def test_log_signals_to_dynamodb_blocks_indirect_same_outcome_exposure() -> None:
    table = FakeTable()
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    log_signals_to_dynamodb(
        table,
        [
            signal(
                outcome_name="Chelsea",
                target_odds=2.2,
                target_effective_odds=2.176,
                reference_fair_odds=2.1,
                reference_probability=1 / 2.1,
            )
        ],
        stake=1,
        logged_at=logged_at,
    )

    result = log_signals_to_dynamodb(
        table,
        [
            signal(
                outcome_name="Arsenal",
                bet_side="lay",
                target_odds=2.1,
                target_effective_odds=2.1,
                reference_fair_odds=2.2,
                reference_probability=1 / 2.2,
            )
        ],
        stake=1,
        logged_at=logged_at,
    )

    assert result.attempted == 1
    assert result.inserted == 0
    assert result.duplicates == 0
    assert len(table.items) == 1


def test_log_signals_to_dynamodb_allows_same_selection_back_lay_hedge() -> None:
    table = FakeTable()
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    log_signals_to_dynamodb(table, [signal()], stake=1, logged_at=logged_at)

    result = log_signals_to_dynamodb(
        table,
        [
            signal(
                bet_side="lay",
                target_odds=3.8,
                target_effective_odds=3.8,
                reference_fair_odds=4.0,
                reference_probability=0.25,
            )
        ],
        stake=1,
        logged_at=logged_at,
    )

    assert result.inserted == 1
    assert len(table.items) == 2


def test_log_signals_to_dynamodb_scopes_exposure_to_same_venue() -> None:
    table = FakeTable()
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    log_signals_to_dynamodb(
        table,
        [
            signal(
                outcome_name="Chelsea",
                target_bookmaker="Matchbook",
                target_odds=2.2,
                target_effective_odds=2.176,
                reference_fair_odds=2.1,
                reference_probability=1 / 2.1,
            )
        ],
        stake=1,
        logged_at=logged_at,
    )

    result = log_signals_to_dynamodb(
        table,
        [
            signal(
                outcome_name="Arsenal",
                bet_side="lay",
                target_bookmaker="Smarkets",
                target_odds=2.1,
                target_effective_odds=2.1,
                reference_fair_odds=2.2,
                reference_probability=1 / 2.2,
            )
        ],
        stake=1,
        logged_at=logged_at,
    )

    assert result.inserted == 1
    assert len(table.items) == 2


def test_log_signals_to_dynamodb_blocks_indirect_exposure_within_batch() -> None:
    table = FakeTable()
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)

    result = log_signals_to_dynamodb(
        table,
        [
            signal(
                outcome_name="Chelsea",
                target_odds=2.2,
                target_effective_odds=2.176,
                reference_fair_odds=2.1,
                reference_probability=1 / 2.1,
            ),
            signal(
                outcome_name="Arsenal",
                bet_side="lay",
                target_odds=2.1,
                target_effective_odds=2.1,
                reference_fair_odds=2.2,
                reference_probability=1 / 2.2,
            ),
        ],
        stake=1,
        logged_at=logged_at,
    )

    assert result.attempted == 2
    assert result.inserted == 1
    assert len(table.items) == 1


def test_update_closing_values_in_dynamodb_updates_matching_open_trade() -> None:
    table = FakeTable()
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    log_signals_to_dynamodb(table, [signal()], stake=1, logged_at=logged_at)
    closing = ValueSignal(
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time=datetime(2026, 8, 15, 15, tzinfo=timezone.utc),
        market_key="h2h",
        outcome_name="Arsenal",
        target_bookmaker="Matchbook",
        target_odds=4.0,
        target_effective_odds=3.94,
        reference_fair_odds=3.8,
        reference_probability=1 / 3.8,
        edge=0.0368,
        reference_bookmakers=("Pinnacle", "Smarkets"),
    )

    result = update_closing_values_in_dynamodb(table, [closing], checked_at=logged_at)
    item = next(iter(table.items.values()))

    assert result.open_trades == 1
    assert result.updated == 1
    assert item["closing_target_odds"] == Decimal("4.0")
    assert item["target_clv"] == Decimal("0.050000000000000044")
    assert item["beat_closing_line"] is True


def test_update_closing_values_in_dynamodb_prices_lay_edge_per_liability() -> None:
    table = FakeTable()
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    lay_signal = signal(
        target_odds=1.8,
        target_effective_odds=1.8,
        reference_fair_odds=2.0,
        reference_probability=0.5,
        edge=0.1125,
        bet_side="lay",
    )
    log_signals_to_dynamodb(table, [lay_signal], stake=10, logged_at=logged_at)
    closing = signal(
        target_odds=2.0,
        target_effective_odds=2.0,
        reference_fair_odds=1 / 0.56,
        reference_probability=0.56,
        edge=-0.01,
        bet_side="lay",
    )

    result = update_closing_values_in_dynamodb(table, [closing], checked_at=logged_at)
    item = next(iter(table.items.values()))

    assert result.open_trades == 1
    assert result.updated == 1
    assert item["bet_side"] == "lay"
    assert item["stake"] == Decimal("12.5")
    assert item["target_clv"] == Decimal("0.11111111111111116")
    assert item["closing_edge"] == Decimal("-0.021000000000000185")
    assert item["positive_closing_edge"] is False


def test_settle_results_in_dynamodb_sets_profit_for_winner() -> None:
    table = FakeTable()
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    log_signals_to_dynamodb(table, [signal()], stake=1, logged_at=logged_at)

    result = settle_results_in_dynamodb(table, {"event-1": "Arsenal"})
    item = next(iter(table.items.values()))

    assert result.open_trades == 1
    assert result.settled == 1
    assert item["status"] == "settled"
    assert item["result"] == "Arsenal"
    assert item["profit"] == Decimal("3.136")


def test_settle_results_in_dynamodb_sets_lay_profit_from_liability() -> None:
    table = FakeTable()
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    lay_signal = signal(
        target_odds=1.8,
        target_effective_odds=1.8,
        reference_fair_odds=2.0,
        reference_probability=0.5,
        edge=0.1125,
        bet_side="lay",
    )
    log_signals_to_dynamodb(table, [lay_signal], stake=10, logged_at=logged_at)

    result = settle_results_in_dynamodb(table, {"event-1": "Chelsea"})
    item = next(iter(table.items.values()))

    assert result.open_trades == 1
    assert result.settled == 1
    assert item["status"] == "settled"
    assert item["result"] == "Chelsea"
    assert item["stake"] == Decimal("12.5")
    assert item["profit"] == Decimal("12.25")


def test_settle_results_in_dynamodb_sets_lay_loss_to_configured_risk() -> None:
    table = FakeTable()
    logged_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    lay_signal = signal(
        target_odds=1.8,
        target_effective_odds=1.8,
        reference_fair_odds=2.0,
        reference_probability=0.5,
        edge=0.1125,
        bet_side="lay",
    )
    log_signals_to_dynamodb(table, [lay_signal], stake=10, logged_at=logged_at)

    settle_results_in_dynamodb(table, {"event-1": "Arsenal"})
    item = next(iter(table.items.values()))

    assert item["result"] == "Arsenal"
    assert item["profit"] == Decimal("-10.0")
