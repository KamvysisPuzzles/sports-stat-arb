from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from exchange_scanner.live_execution import (
    LiveExecutionConfig,
    LiveOrderResult,
    LiveOrderStatus,
    execute_live_signals,
    live_filter_reject_reason,
    reconcile_live_orders,
    size_live_order,
)
from exchange_scanner.the_odds_api import ValueSignal


class ConditionalCheckFailedException(Exception):
    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeLiveOrderTable:
    def __init__(self) -> None:
        self.items = {}

    def put_item(self, *, Item, ConditionExpression):
        assert ConditionExpression == "attribute_not_exists(order_id)"
        if Item["order_id"] in self.items:
            raise ConditionalCheckFailedException()
        self.items[Item["order_id"]] = Item

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}

    def update_item(
        self,
        *,
        Key,
        UpdateExpression,
        ExpressionAttributeValues,
        ExpressionAttributeNames=None,
    ):
        item = self.items[Key["order_id"]]
        names = ExpressionAttributeNames or {}
        for assignment in UpdateExpression.replace("SET ", "").split(", "):
            field, value_key = [part.strip() for part in assignment.split(" = ")]
            field = names.get(field, field)
            item[field] = ExpressionAttributeValues[value_key]
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class FakeExecutor:
    def __init__(self) -> None:
        self.intents = []

    def place_limit_order(self, intent):
        self.intents.append(intent)
        return LiveOrderResult(
            order_id=intent.order_id,
            status="submitted",
            venue_order_id="venue-order-1",
        )

    def fetch_order_status(self, order):
        return LiveOrderStatus(
            order_id=order["order_id"],
            status="partially_matched",
            venue_order_id=order["venue_order_id"],
            matched_size=0.4,
            avg_matched_odds=4.2,
            remaining_size=0.6,
        )


def signal(**overrides) -> ValueSignal:
    values = {
        "sport_key": "soccer_epl",
        "event_id": "event-1",
        "event_name": "Arsenal v Chelsea",
        "commence_time": datetime(2026, 8, 15, 15, tzinfo=timezone.utc),
        "market_key": "h2h",
        "outcome_name": "Arsenal",
        "target_bookmaker": "Matchbook",
        "bet_side": "back",
        "target_odds": 4.2,
        "target_effective_odds": 4.136,
        "reference_fair_odds": 4.0,
        "reference_probability": 0.25,
        "edge": 0.034,
        "reference_bookmakers": ("Pinnacle", "Smarkets"),
        "reference_disagreement_pct": 0.02,
    }
    values.update(overrides)
    return ValueSignal(**values)


def test_live_filter_accepts_soccer_under_three_percent_disagreement() -> None:
    config = LiveExecutionConfig(enabled=True)

    assert live_filter_reject_reason(signal(), config=config) is None
    assert (
        live_filter_reject_reason(
            signal(reference_disagreement_pct=0.031),
            config=config,
        )
        == "reference_disagreement_too_high"
    )
    assert (
        live_filter_reject_reason(signal(sport_key="basketball_nba"), config=config)
        == "sport_not_allowed"
    )


def test_size_live_order_uses_fractional_kelly_and_risk_caps() -> None:
    intent = size_live_order(
        signal(),
        config=LiveExecutionConfig(
            enabled=True,
            bankroll=1000,
            kelly_fraction=0.25,
            max_order_risk_pct=0.01,
            max_order_risk=20,
            min_order_risk=1,
        ),
        liquidity={"available_at_or_above_target": 100},
        dry_run=True,
    )

    assert intent is not None
    assert intent.liability == intent.stake
    assert intent.liability == min(1000 * (0.034 / 3.2) * 0.25, 10)


def test_lay_size_is_capped_by_liability_not_stake() -> None:
    intent = size_live_order(
        signal(bet_side="lay", target_odds=5.0, target_effective_odds=5.0),
        config=LiveExecutionConfig(
            enabled=True,
            bankroll=1000,
            kelly_fraction=0.25,
            max_order_risk_pct=0.01,
            max_order_risk=20,
            min_order_risk=1,
        ),
        liquidity={"available_at_or_above_target": 100},
        dry_run=True,
    )

    assert intent is not None
    assert intent.liability == 8.5
    assert intent.stake == 8.5 / 4.0


def test_size_live_order_supports_flat_back_risk() -> None:
    intent = size_live_order(
        signal(),
        config=LiveExecutionConfig(
            enabled=True,
            sizing_method="flat",
            flat_order_risk=1,
            bankroll=1000,
            max_order_risk_pct=0.01,
            max_order_risk=20,
            min_order_risk=1,
        ),
        liquidity={"available_at_or_above_target": 100},
        dry_run=True,
    )

    assert intent is not None
    assert intent.sizing_method == "flat"
    assert intent.flat_order_risk == 1
    assert intent.stake == 1
    assert intent.liability == 1
    assert intent.full_kelly_fraction == 0.034 / 3.2


def test_size_live_order_supports_flat_lay_liability() -> None:
    intent = size_live_order(
        signal(bet_side="lay", target_odds=5.0, target_effective_odds=5.0),
        config=LiveExecutionConfig(
            enabled=True,
            sizing_method="flat",
            flat_order_risk=1,
            bankroll=1000,
            max_order_risk_pct=0.01,
            max_order_risk=20,
            min_order_risk=1,
        ),
        liquidity={"available_at_or_above_target": 100},
        dry_run=True,
    )

    assert intent is not None
    assert intent.sizing_method == "flat"
    assert intent.liability == 1
    assert intent.stake == 0.25


def test_flat_size_is_still_capped_by_available_liquidity() -> None:
    intent = size_live_order(
        signal(),
        config=LiveExecutionConfig(
            enabled=True,
            sizing_method="flat",
            flat_order_risk=2,
            bankroll=1000,
            max_order_risk_pct=0.01,
            max_order_risk=20,
            min_order_risk=0.5,
        ),
        liquidity={"available_at_or_above_target": 0.75},
        dry_run=True,
    )

    assert intent is not None
    assert intent.stake == 0.75
    assert intent.liability == 0.75


def test_execute_live_signals_records_dry_run_orders_without_executor() -> None:
    table = FakeLiveOrderTable()
    result = execute_live_signals(
        table,
        [signal()],
        config=LiveExecutionConfig(enabled=True, dry_run=True),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={
            ("event-1", "h2h", "arsenal", "matchbook", "back"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            }
        },
    )

    assert result.recorded == 1
    item = next(iter(table.items.values()))
    assert item["execution_mode"] == "dry_run"
    assert item["status"] == "dry_run"
    assert item["sizing_method"] == "kelly"
    assert item["reference_disagreement_pct"] == Decimal("0.02")


def test_execute_live_signals_requires_minimum_confirmed_liquidity() -> None:
    table = FakeLiveOrderTable()
    result = execute_live_signals(
        table,
        [signal()],
        config=LiveExecutionConfig(
            enabled=True,
            dry_run=True,
            min_confirmed_liquidity=5,
        ),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={
            ("event-1", "h2h", "arsenal", "matchbook", "back"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 4.99,
            }
        },
    )

    assert result.recorded == 0
    assert result.skipped == {"confirmed_liquidity_below_minimum": 1}


def test_execute_live_signals_allows_configured_venue_without_confirmed_liquidity() -> None:
    table = FakeLiveOrderTable()
    result = execute_live_signals(
        table,
        [signal(target_bookmaker="Betfair")],
        config=LiveExecutionConfig(
            enabled=True,
            dry_run=True,
            allow_unconfirmed_liquidity_bookmakers=("betfair",),
        ),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={},
    )

    assert result.recorded == 1
    item = next(iter(table.items.values()))
    assert item["target_bookmaker"] == "Betfair"
    assert item["available_at_target"] == Decimal(0)


def test_execute_live_signals_allows_live_betfair_without_confirmed_liquidity() -> None:
    table = FakeLiveOrderTable()

    result = execute_live_signals(
        table,
        [signal(target_bookmaker="Betfair")],
        config=LiveExecutionConfig(
            enabled=True,
            dry_run=False,
            allow_unconfirmed_liquidity_bookmakers=("betfair",),
        ),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={
            ("event-1", "h2h", "arsenal", "betfair", "back"): {
                "liquidity_status": "unavailable",
                "available_at_or_above_target": 0,
                "matchbook_market_id": "1.234",
                "matchbook_runner_id": "789",
            }
        },
        executors={"betfair": FakeExecutor()},
    )

    assert result.recorded == 1
    item = next(iter(table.items.values()))
    assert item["execution_mode"] == "live"
    assert item["target_bookmaker"] == "Betfair"


def test_execute_live_signals_allows_live_betfair_without_pre_enriched_execution_ids() -> None:
    table = FakeLiveOrderTable()

    result = execute_live_signals(
        table,
        [signal(target_bookmaker="Betfair")],
        config=LiveExecutionConfig(
            enabled=True,
            dry_run=False,
            allow_unconfirmed_liquidity_bookmakers=("betfair",),
        ),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={},
        executors={"betfair": FakeExecutor()},
    )

    assert result.recorded == 1
    assert result.submitted == 1
    assert result.skipped == {}
    item = next(iter(table.items.values()))
    assert item["target_bookmaker"] == "Betfair"
    assert item["exchange_market_id"] == ""


def test_execute_live_signals_has_no_daily_cap_by_default() -> None:
    table = FakeLiveOrderTable()
    first = signal(event_id="event-1", outcome_name="Arsenal")
    second = signal(
        event_id="event-2",
        event_name="Liverpool v Everton",
        outcome_name="Liverpool",
    )

    result = execute_live_signals(
        table,
        [first, second],
        config=LiveExecutionConfig(
            enabled=True,
            dry_run=True,
            sizing_method="flat",
            flat_order_risk=1,
            bankroll=1,
            max_order_risk_pct=1,
            max_order_risk=1,
        ),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={
            ("event-1", "h2h", "arsenal", "matchbook", "back"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            },
            ("event-2", "h2h", "liverpool", "matchbook", "back"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            },
        },
    )

    assert result.recorded == 2
    assert "daily_risk_cap" not in result.skipped


def test_execute_live_signals_blocks_stacked_positive_exposure_within_batch() -> None:
    table = FakeLiveOrderTable()
    result = execute_live_signals(
        table,
        [
            signal(outcome_name="Chelsea", target_odds=2.2, reference_fair_odds=2.1),
            signal(
                outcome_name="Arsenal",
                bet_side="lay",
                target_odds=2.1,
                target_effective_odds=2.1,
                reference_fair_odds=2.2,
                reference_probability=1 / 2.2,
            ),
        ],
        config=LiveExecutionConfig(enabled=True, dry_run=True),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={
            ("event-1", "h2h", "chelsea", "matchbook", "back"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            },
            ("event-1", "h2h", "arsenal", "matchbook", "lay"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            },
        },
    )

    assert result.recorded == 1
    assert result.skipped == {"stacked_event_exposure": 1}
    assert len(table.items) == 1


def test_execute_live_signals_allows_same_selection_back_lay_hedge() -> None:
    table = FakeLiveOrderTable()
    result = execute_live_signals(
        table,
        [
            signal(),
            signal(
                bet_side="lay",
                target_odds=3.8,
                target_effective_odds=3.8,
                reference_fair_odds=4.0,
                reference_probability=0.25,
            ),
        ],
        config=LiveExecutionConfig(enabled=True, dry_run=True),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={
            ("event-1", "h2h", "arsenal", "matchbook", "back"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            },
            ("event-1", "h2h", "arsenal", "matchbook", "lay"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            },
        },
    )

    assert result.recorded == 2
    assert result.skipped == {}
    assert len(table.items) == 2


def test_execute_live_signals_blocks_cross_venue_positive_exposure_by_default() -> None:
    table = FakeLiveOrderTable()
    result = execute_live_signals(
        table,
        [
            signal(outcome_name="Chelsea", target_bookmaker="Matchbook"),
            signal(
                outcome_name="Arsenal",
                bet_side="lay",
                target_bookmaker="Smarkets",
                target_odds=2.1,
                target_effective_odds=2.1,
                reference_fair_odds=2.2,
                reference_probability=1 / 2.2,
            ),
        ],
        config=LiveExecutionConfig(enabled=True, dry_run=True),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={
            ("event-1", "h2h", "chelsea", "matchbook", "back"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            },
            ("event-1", "h2h", "arsenal", "smarkets", "lay"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            },
        },
    )

    assert result.recorded == 1
    assert result.skipped == {"stacked_event_exposure": 1}
    assert len(table.items) == 1


def test_execute_live_signals_can_scope_exposure_guardrail_to_same_venue() -> None:
    table = FakeLiveOrderTable()
    result = execute_live_signals(
        table,
        [
            signal(outcome_name="Chelsea", target_bookmaker="Matchbook"),
            signal(
                outcome_name="Arsenal",
                bet_side="lay",
                target_bookmaker="Smarkets",
                target_odds=2.1,
                target_effective_odds=2.1,
                reference_fair_odds=2.2,
                reference_probability=1 / 2.2,
            ),
        ],
        config=LiveExecutionConfig(
            enabled=True,
            dry_run=True,
            prevent_cross_venue_event_exposure=False,
        ),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={
            ("event-1", "h2h", "chelsea", "matchbook", "back"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            },
            ("event-1", "h2h", "arsenal", "smarkets", "lay"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            },
        },
    )

    assert result.recorded == 2
    assert result.skipped == {}
    assert len(table.items) == 2


def test_execute_live_signals_submits_to_configured_executor_when_not_dry_run() -> None:
    table = FakeLiveOrderTable()
    executor = FakeExecutor()

    result = execute_live_signals(
        table,
        [signal()],
        config=LiveExecutionConfig(enabled=True, dry_run=False),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={
            ("event-1", "h2h", "arsenal", "matchbook", "back"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
                "matchbook_market_id": "market-1",
                "matchbook_runner_id": "runner-1",
            }
        },
        executors={"matchbook": executor},
    )

    assert result.submitted == 1
    assert result.recorded == 1
    assert len(executor.intents) == 1
    item = next(iter(table.items.values()))
    assert item["execution_mode"] == "live"
    assert item["status"] == "submitted"
    assert item["venue_order_id"] == "venue-order-1"


def test_execute_live_signals_retries_failed_zero_match_attempts() -> None:
    class FailedThenMatchedExecutor(FakeExecutor):
        def place_limit_order(self, intent):
            self.intents.append(intent)
            if len(self.intents) == 1:
                return LiveOrderResult(
                    order_id=intent.order_id,
                    status="failed",
                    matched_size=0,
                    remaining_size=intent.stake,
                    error="temporary_mapping_failure",
                )
            return LiveOrderResult(
                order_id=intent.order_id,
                status="matched",
                venue_order_id="venue-order-2",
                matched_size=intent.stake,
                avg_matched_odds=intent.limit_odds,
                remaining_size=0,
            )

    table = FakeLiveOrderTable()
    executor = FailedThenMatchedExecutor()
    liquidity_by_key = {
        ("event-1", "h2h", "arsenal", "matchbook", "back"): {
            "liquidity_status": "available",
            "available_at_or_above_target": 25,
            "matchbook_market_id": "market-1",
            "matchbook_runner_id": "runner-1",
        }
    }

    first = execute_live_signals(
        table,
        [signal()],
        config=LiveExecutionConfig(enabled=True, dry_run=False),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key=liquidity_by_key,
        executors={"matchbook": executor},
    )
    second = execute_live_signals(
        table,
        [signal()],
        config=LiveExecutionConfig(enabled=True, dry_run=False),
        logged_at=datetime(2026, 8, 14, 12, 2, tzinfo=timezone.utc),
        liquidity_by_key=liquidity_by_key,
        executors={"matchbook": executor},
    )

    assert first.recorded == 1
    assert second.recorded == 1
    assert second.skipped == {}
    assert len(executor.intents) == 2
    assert len(table.items) == 2
    assert len({item["order_id"] for item in table.items.values()}) == 2


def test_execute_live_signals_dedupes_after_matched_attempt() -> None:
    class MatchedExecutor(FakeExecutor):
        def place_limit_order(self, intent):
            self.intents.append(intent)
            return LiveOrderResult(
                order_id=intent.order_id,
                status="matched",
                venue_order_id="venue-order-1",
                matched_size=intent.stake,
                avg_matched_odds=intent.limit_odds,
                remaining_size=0,
            )

    table = FakeLiveOrderTable()
    executor = MatchedExecutor()
    liquidity_by_key = {
        ("event-1", "h2h", "arsenal", "matchbook", "back"): {
            "liquidity_status": "available",
            "available_at_or_above_target": 25,
            "matchbook_market_id": "market-1",
            "matchbook_runner_id": "runner-1",
        }
    }

    first = execute_live_signals(
        table,
        [signal()],
        config=LiveExecutionConfig(enabled=True, dry_run=False),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key=liquidity_by_key,
        executors={"matchbook": executor},
    )
    second = execute_live_signals(
        table,
        [signal()],
        config=LiveExecutionConfig(enabled=True, dry_run=False),
        logged_at=datetime(2026, 8, 14, 12, 2, tzinfo=timezone.utc),
        liquidity_by_key=liquidity_by_key,
        executors={"matchbook": executor},
    )

    assert first.recorded == 1
    assert second.recorded == 0
    assert second.skipped == {"duplicate_live_signal": 1}
    assert len(executor.intents) == 1
    assert len(table.items) == 1


def test_execute_live_signals_skips_real_order_without_execution_ids() -> None:
    table = FakeLiveOrderTable()

    result = execute_live_signals(
        table,
        [signal()],
        config=LiveExecutionConfig(enabled=True, dry_run=False),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={
            ("event-1", "h2h", "arsenal", "matchbook", "back"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
            }
        },
        executors={"matchbook": FakeExecutor()},
    )

    assert result.recorded == 0
    assert result.skipped == {"missing_execution_market_id": 1}


def test_reconcile_live_orders_updates_partial_fill_status() -> None:
    table = FakeLiveOrderTable()
    executor = FakeExecutor()
    result = execute_live_signals(
        table,
        [signal()],
        config=LiveExecutionConfig(enabled=True, dry_run=False, sizing_method="flat"),
        logged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        liquidity_by_key={
            ("event-1", "h2h", "arsenal", "matchbook", "back"): {
                "liquidity_status": "available",
                "available_at_or_above_target": 25,
                "matchbook_market_id": "market-1",
                "matchbook_runner_id": "runner-1",
            }
        },
        executors={"matchbook": executor},
    )

    monitor = reconcile_live_orders(
        table,
        executors={"matchbook": executor},
        checked_at=datetime(2026, 8, 14, 12, 1, tzinfo=timezone.utc),
    )

    assert result.recorded == 1
    assert monitor.monitored == 1
    assert monitor.updated == 1
    item = next(iter(table.items.values()))
    assert item["status"] == "partially_matched"
    assert item["matched_size"] == Decimal("0.4")
    assert item["remaining_size"] == Decimal("0.6")
