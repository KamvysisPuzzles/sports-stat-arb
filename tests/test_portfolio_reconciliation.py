from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from exchange_scanner.portfolio_reconciliation import refresh_account_state
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
    monkeypatch.setattr(lambda_function, "_dynamodb_table", lambda name, region: table)
    monkeypatch.setattr(lambda_function, "executors_from_env", lambda: executors)

    response = lambda_function.lambda_handler({}, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["updated"] == 3
    assert set(table.items) == {"Betfair", "Matchbook", "Smarkets"}
