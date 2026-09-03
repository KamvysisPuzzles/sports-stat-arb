from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from exchange_scanner.portfolio_reconciliation import (
    refresh_account_state,
    refresh_order_settlements,
)
from lambda_functions.portfolio_reconciler import lambda_function


class FakeTable:
    def __init__(self, items=None):
        self.items = {item["venue"]: dict(item) for item in (items or [])}

    def get_item(self, *, Key):
        item = self.items.get(Key["venue"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, *, Item):
        self.items[Item["venue"]] = dict(Item)


class FakeExecutor:
    def __init__(self, venue, *, available=10, error=None):
        self.venue = venue
        self.available = available
        self.error = error

    def fetch_account_snapshot(self):
        if self.error:
            raise RuntimeError(self.error)
        return {
            "venue": self.venue,
            "currency": "GBP",
            "balance": self.available + 1,
            "available_funds": self.available,
            "exposure": -1,
            "retained_commission": 0,
        }


class FakeOrderTable:
    def __init__(self, items=None):
        self.items = {item["order_id"]: dict(item) for item in (items or [])}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}

    def update_item(self, *, Key, ExpressionAttributeValues, **kwargs):
        item = self.items[Key["order_id"]]
        item.update(
            {
                "pnl_status": ExpressionAttributeValues[":confirmed"],
                "settlement_source": ExpressionAttributeValues[":source"],
                "gross_profit": ExpressionAttributeValues[":gross_profit"],
                "commission": ExpressionAttributeValues[":commission"],
                "net_profit": ExpressionAttributeValues[":net_profit"],
                "profit": ExpressionAttributeValues[":net_profit"],
                "venue_result": ExpressionAttributeValues[":venue_result"],
                "venue_settled_at": ExpressionAttributeValues[":venue_settled_at"],
                "settlement_confirmed_at": ExpressionAttributeValues[":confirmed_at"],
            }
        )
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class FakeSettlementExecutor:
    def __init__(self, settlement=None, *, error=None):
        self.settlement = settlement
        self.error = error

    def fetch_order_settlement(self, order):
        if self.error:
            raise RuntimeError(self.error)
        return self.settlement


def test_refresh_account_state_records_each_venue_independently() -> None:
    table = FakeTable()
    result = refresh_account_state(
        table,
        {
            "betfair": FakeExecutor("Betfair", available=49),
            "matchbook": FakeExecutor("Matchbook", error="session expired"),
            "smarkets": FakeExecutor("Smarkets", available=91.77),
        },
        checked_at=datetime(2026, 9, 2, 18, tzinfo=timezone.utc),
    )

    assert result.checked == 3
    assert result.updated == 2
    assert set(result.failed) == {"Matchbook"}
    assert table.items["Betfair"]["available_funds"] == Decimal(49)
    assert table.items["Smarkets"]["available_funds"] == Decimal("91.77")
    assert table.items["Matchbook"]["status"] == "error"
    assert "session expired" in table.items["Matchbook"]["error"]


def test_refresh_error_preserves_last_successful_balance() -> None:
    table = FakeTable(
        [
            {
                "venue": "Matchbook",
                "balance": Decimal(50),
                "available_funds": Decimal(48),
                "status": "ok",
                "last_success_at": "2026-09-02T17:58:00+00:00",
            }
        ]
    )

    refresh_account_state(
        table,
        {
            "betfair": FakeExecutor("Betfair"),
            "matchbook": FakeExecutor("Matchbook", error="unauthorized"),
            "smarkets": FakeExecutor("Smarkets"),
        },
        checked_at=datetime(2026, 9, 2, 18, tzinfo=timezone.utc),
    )

    assert table.items["Matchbook"]["available_funds"] == Decimal(48)
    assert table.items["Matchbook"]["last_success_at"] == "2026-09-02T17:58:00+00:00"
    assert table.items["Matchbook"]["status"] == "error"


def test_reconciler_lambda_refreshes_configured_table(monkeypatch) -> None:
    table = FakeTable()
    executors = {
        "betfair": FakeExecutor("Betfair"),
        "matchbook": FakeExecutor("Matchbook"),
        "smarkets": FakeExecutor("Smarkets"),
    }
    monkeypatch.setenv("LIVE_ACCOUNT_STATE_TABLE", "account-state")
    monkeypatch.delenv("LIVE_ORDER_TABLE", raising=False)
    monkeypatch.setattr(lambda_function, "_dynamodb_table", lambda name, region: table)
    monkeypatch.setattr(lambda_function, "executors_from_env", lambda: executors)

    response = lambda_function.lambda_handler({}, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["updated"] == 3
    assert set(table.items) == {"Betfair", "Matchbook", "Smarkets"}


def test_refresh_order_settlements_promotes_only_venue_confirmed_rows() -> None:
    table = FakeOrderTable(
        [
            {
                "order_id": "live#confirmed",
                "execution_mode": "live",
                "target_bookmaker": "Matchbook",
                "venue_order_id": "offer-1",
                "status": "settled",
                "pnl_status": "estimated",
                "matched_size": Decimal(1),
            },
            {
                "order_id": "live#pending",
                "execution_mode": "live",
                "target_bookmaker": "Betfair",
                "venue_order_id": "bet-1",
                "status": "settled",
                "pnl_status": "estimated",
                "matched_size": Decimal(1),
            },
        ]
    )
    result = refresh_order_settlements(
        table,
        {
            "matchbook": FakeSettlementExecutor(
                {
                    "settlement_source": "matchbook_settled_bets",
                    "gross_profit": 1.5,
                    "commission": 0.03,
                    "net_profit": 1.47,
                    "venue_result": "WIN",
                    "venue_settled_at": "2026-09-02T19:00:00Z",
                }
            ),
            "betfair": FakeSettlementExecutor(None),
        },
        checked_at=datetime(2026, 9, 2, 20, tzinfo=timezone.utc),
    )

    assert result.checked == 2
    assert result.confirmed == 1
    assert result.pending == 1
    assert result.failed == {}
    confirmed = table.items["live#confirmed"]
    assert confirmed["pnl_status"] == "confirmed"
    assert confirmed["net_profit"] == Decimal("1.47")
    assert confirmed["settlement_source"] == "matchbook_settled_bets"
    assert table.items["live#pending"]["pnl_status"] == "estimated"


def test_refresh_order_settlements_repairs_legacy_zero_smarkets_pnl() -> None:
    table = FakeOrderTable(
        [
            {
                "order_id": "live#smarkets-zero",
                "execution_mode": "live",
                "target_bookmaker": "Smarkets",
                "venue_order_id": "order-1",
                "status": "settled",
                "pnl_status": "confirmed",
                "settlement_source": "smarkets_account_activity",
                "matched_size": Decimal(1),
                "gross_profit": Decimal(0),
                "commission": Decimal(0),
                "net_profit": Decimal(0),
            }
        ]
    )

    result = refresh_order_settlements(
        table,
        {
            "smarkets": FakeSettlementExecutor(
                {
                    "settlement_source": "smarkets_market_activity",
                    "gross_profit": -1,
                    "commission": 0,
                    "net_profit": -1,
                    "venue_result": "LOSER",
                    "venue_settled_at": "2026-09-02T19:00:00Z",
                }
            )
        },
        checked_at=datetime(2026, 9, 3, 8, tzinfo=timezone.utc),
    )

    assert result.checked == 1
    assert result.confirmed == 1
    repaired = table.items["live#smarkets-zero"]
    assert repaired["settlement_source"] == "smarkets_market_activity"
    assert repaired["net_profit"] == Decimal("-1")


def test_reconciler_lambda_refreshes_accounts_and_settlements(monkeypatch) -> None:
    account_table = FakeTable()
    order_table = FakeOrderTable(
        [
            {
                "order_id": "live#settled",
                "target_bookmaker": "Smarkets",
                "venue_order_id": "order-1",
                "status": "settled",
                "pnl_status": "estimated",
                "matched_size": Decimal(1),
            }
        ]
    )
    smarkets = FakeExecutor("Smarkets")
    smarkets.fetch_order_settlement = lambda order: {
        "settlement_source": "smarkets_account_activity",
        "gross_profit": -1,
        "commission": 0,
        "net_profit": -1,
        "venue_result": "LOSER",
        "venue_settled_at": "2026-09-02T19:00:00Z",
    }
    executors = {
        "betfair": FakeExecutor("Betfair"),
        "matchbook": FakeExecutor("Matchbook"),
        "smarkets": smarkets,
    }
    tables = {"account-state": account_table, "live-orders": order_table}
    monkeypatch.setenv("LIVE_ACCOUNT_STATE_TABLE", "account-state")
    monkeypatch.setenv("LIVE_ORDER_TABLE", "live-orders")
    monkeypatch.setattr(lambda_function, "_dynamodb_table", lambda name, region: tables[name])
    monkeypatch.setattr(lambda_function, "executors_from_env", lambda: executors)

    response = lambda_function.lambda_handler({}, None)
    payload = json.loads(response["body"])

    assert payload["updated"] == 3
    assert payload["settlements"]["confirmed"] == 1
    assert order_table.items["live#settled"]["pnl_status"] == "confirmed"
