from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from exchange_scanner.portfolio_dashboard import (
    portfolio_payload,
    render_portfolio_html,
)
from lambda_functions.portfolio_dashboard import lambda_function


class FakeTable:
    def __init__(self, items):
        self.items = list(items)

    def scan(self, **kwargs):
        return {"Items": self.items}


def live_orders():
    common = {
        "execution_mode": "live",
        "sport_key": "soccer_epl",
        "market": "h2h",
        "commence_time": "2026-09-02T22:30:00+00:00",
        "edge": Decimal("0.02"),
        "error": "",
    }
    return [
        {
            **common,
            "order_id": "live#betfair-lay",
            "logged_at": "2026-09-02T17:30:00+00:00",
            "event_name": "Manchester City v Coventry City",
            "outcome_name": "Manchester City",
            "target_bookmaker": "Betfair",
            "bet_side": "lay",
            "limit_odds": Decimal("5.0"),
            "stake": Decimal("0.25"),
            "liability": Decimal("1.0"),
            "status": "matched",
            "venue_order_id": "440809909644",
            "matched_size": Decimal("0.25"),
            "avg_matched_odds": Decimal("5.0"),
            "remaining_size": Decimal(0),
            "target_clv": Decimal("0.02"),
            "mark_to_market_clv": Decimal("0.00945945945945946"),
            "closing_target_odds": Decimal("5.2"),
            "closing_checked_at": "2026-09-02T17:59:00+00:00",
        },
        {
            **common,
            "order_id": "live#smarkets-open",
            "logged_at": "2026-09-02T17:25:00+00:00",
            "event_name": "Swindon Town v Colchester United",
            "outcome_name": "Colchester United",
            "target_bookmaker": "Smarkets",
            "bet_side": "back",
            "limit_odds": Decimal("2.64"),
            "stake": Decimal(1),
            "liability": Decimal(1),
            "status": "partially_matched",
            "venue_order_id": "203602746814873610",
            "matched_size": Decimal("0.40"),
            "avg_matched_odds": Decimal("2.64"),
            "remaining_size": Decimal("0.60"),
        },
        {
            **common,
            "order_id": "live#cancelled",
            "logged_at": "2026-09-02T17:20:00+00:00",
            "event_name": "West Bromwich Albion v Charlton Athletic",
            "outcome_name": "West Bromwich Albion",
            "target_bookmaker": "Smarkets",
            "bet_side": "lay",
            "limit_odds": Decimal("1.81"),
            "stake": Decimal("1.2345679"),
            "liability": Decimal(1),
            "status": "cancelled",
            "venue_order_id": "203602717307416586",
            "matched_size": Decimal(0),
            "avg_matched_odds": Decimal(0),
            "remaining_size": Decimal(0),
        },
        {
            **common,
            "order_id": "live#settled",
            "logged_at": "2026-09-01T15:00:00+00:00",
            "commence_time": "2026-09-01T17:00:00+00:00",
            "event_name": "Arsenal v Chelsea",
            "outcome_name": "Arsenal",
            "target_bookmaker": "Matchbook",
            "bet_side": "back",
            "limit_odds": Decimal("2.5"),
            "stake": Decimal(1),
            "liability": Decimal(1),
            "status": "settled",
            "venue_order_id": "192883471",
            "matched_size": Decimal(1),
            "avg_matched_odds": Decimal("2.5"),
            "remaining_size": Decimal(0),
            "gross_profit": Decimal("1.50"),
            "commission": Decimal("0.03"),
            "net_profit": Decimal("1.47"),
            "target_clv": Decimal("0.04"),
            "result": "Arsenal",
            "settled_at": "2026-09-01T19:00:00+00:00",
        },
        {
            **common,
            "order_id": "live#failed",
            "logged_at": "2026-09-02T17:15:00+00:00",
            "event_name": "Fleetwood Town v Crewe",
            "outcome_name": "Fleetwood Town",
            "target_bookmaker": "Matchbook",
            "bet_side": "back",
            "limit_odds": Decimal("4.2"),
            "stake": Decimal(1),
            "liability": Decimal(1),
            "status": "failed",
            "venue_order_id": "",
            "matched_size": Decimal(0),
            "avg_matched_odds": Decimal(0),
            "remaining_size": Decimal(1),
            "error": "insufficient_funds",
        },
        {
            **common,
            "order_id": "dryrun#ignored",
            "execution_mode": "dry_run",
            "logged_at": "2026-09-02T17:10:00+00:00",
            "event_name": "Dry Run v Test",
            "outcome_name": "Dry Run",
            "target_bookmaker": "Betfair",
            "bet_side": "back",
            "limit_odds": Decimal(2),
            "stake": Decimal(1),
            "liability": Decimal(1),
            "status": "dry_run",
            "matched_size": Decimal(0),
            "remaining_size": Decimal(1),
        },
    ]


def account_rows():
    return [
        {
            "venue": "Betfair",
            "currency": "GBP",
            "balance": Decimal(50),
            "available_funds": Decimal(49),
            "exposure": Decimal(-1),
            "status": "ok",
            "checked_at": "2026-09-02T17:59:30+00:00",
        },
        {
            "venue": "Matchbook",
            "currency": "GBP",
            "balance": Decimal(50),
            "available_funds": Decimal("48.53"),
            "exposure": Decimal("-1.47"),
            "status": "ok",
            "checked_at": "2026-09-02T17:59:20+00:00",
        },
        {
            "venue": "Smarkets",
            "currency": "GBP",
            "balance": Decimal(100),
            "available_balance": Decimal("99.60"),
            "exposure": Decimal("-0.40"),
            "status": "ok",
            "checked_at": "2026-09-02T17:59:10+00:00",
        },
    ]


def test_payload_separates_orders_positions_and_closed_trades() -> None:
    payload = portfolio_payload(
        FakeTable(live_orders()),
        account_table=FakeTable(account_rows()),
        now=datetime(2026, 9, 2, 18, tzinfo=timezone.utc),
    )

    assert payload["summary"]["orders"] == 5
    assert payload["summary"]["open_positions"] == 2
    assert payload["summary"]["open_position_risk"] == 1.4
    assert payload["summary"]["open_position_mtm_clv"] == pytest.approx(0.00945945945945946)
    assert payload["summary"]["open_position_mtm_clv_positions"] == 1
    assert payload["summary"]["open_orders"] == 1
    assert payload["summary"]["open_order_risk"] == 0.6
    assert payload["summary"]["closed_trades"] == 1
    assert payload["summary"]["confirmed_settlements"] == 1
    assert payload["summary"]["realized_pnl"] == 1.47
    assert payload["summary"]["failed_orders"] == 1
    assert payload["summary"]["available_funds"] == 197.13
    assert payload["summary"]["account_venues"] == 3
    assert payload["excluded_dry_run_orders"] == 1


def test_payload_normalises_lay_matched_risk_and_risk_odds() -> None:
    orders = live_orders()
    lay = next(item for item in orders if item["order_id"] == "live#betfair-lay")
    lay["closing_ev_per_risk"] = Decimal("-0.01")
    lay["beat_closing_line"] = True
    payload = portfolio_payload(
        FakeTable(orders),
        now=datetime(2026, 9, 2, 18, tzinfo=timezone.utc),
    )

    betfair = next(item for item in payload["positions"] if item["venue"] == "Betfair")
    assert betfair["matched_size"] == 0.25
    assert betfair["matched_risk"] == 1.0
    assert betfair["risk_odds"] == 1.25
    assert betfair["risk_selection"] == "Not Manchester City"
    assert betfair["clv"] == -0.01
    assert betfair["beat_close"] is False
    assert betfair["mark_to_market_clv"] == pytest.approx(0.00945945945945946)
    assert betfair["mark_to_market_odds"] == 5.2


def test_missing_accounts_are_exceptions_not_zero_balances() -> None:
    payload = portfolio_payload(
        FakeTable(live_orders()),
        now=datetime(2026, 9, 2, 18, tzinfo=timezone.utc),
    )

    assert payload["summary"]["account_venues"] == 0
    assert payload["summary"]["available_funds"] == 0
    missing = [item for item in payload["exceptions"] if item["title"] == "Account snapshot missing"]
    assert {item["venue"] for item in missing} == {"Betfair", "Matchbook", "Smarkets"}


def test_score_derived_settlement_is_not_counted_as_realized_pnl() -> None:
    orders = live_orders()
    settled = next(item for item in orders if item["order_id"] == "live#settled")
    settled["pnl_status"] = "estimated"
    settled["settlement_source"] = "score_feed"

    payload = portfolio_payload(
        FakeTable(orders),
        account_table=FakeTable(account_rows()),
        now=datetime(2026, 9, 2, 18, tzinfo=timezone.utc),
    )

    assert payload["summary"]["realized_pnl"] == 0
    assert payload["summary"]["estimated_pnl"] == 1.47
    assert payload["summary"]["estimated_settlements"] == 1
    assert payload["pnl_series"] == []
    assert any(
        item["title"] == "Settlement awaiting venue confirmation"
        for item in payload["exceptions"]
    )
    closed = render_portfolio_html(payload, view="closed")
    assert "Confirmed / estimated" in closed
    assert "Estimated P&amp;L" in closed
    assert "+£1.47" in closed
    assert "Score-settled and exchange-confirmed" in closed


def test_rendered_views_use_institutional_command_center_structure() -> None:
    payload = portfolio_payload(
        FakeTable(live_orders()),
        account_table=FakeTable(account_rows()),
        now=datetime(2026, 9, 2, 18, tzinfo=timezone.utc),
    )

    overview = render_portfolio_html(payload, token="secret")
    positions = render_portfolio_html(payload, view="positions", token="secret")
    closed = render_portfolio_html(payload, view="closed", token="secret")
    reconciliation = render_portfolio_html(payload, view="reconciliation", token="secret")

    assert "Portfolio command center" in overview
    assert "Available funds" in overview
    assert "Open positions" in overview
    assert "Venue funds" in overview
    assert "£1.00 reserved" in overview
    assert "£-1.00 exposure" not in overview
    assert "Not Manchester City" in overview
    assert "MTM CLV" in overview
    assert "Current fair edge" not in overview
    assert "Settling today" in positions
    assert "MTM measured" not in positions
    assert "0.95%" in overview
    assert "Current market odds 5.20; priced 02 Sep 17:59 UTC" in overview
    assert "Closed trades" in closed
    assert "+£1.47" in closed
    assert "Reconciliation exceptions" in reconciliation
    assert "insufficient_funds" in reconciliation
    assert "?token=secret&amp;view=positions" in overview


def test_portfolio_lambda_uses_separate_tables_and_returns_json(monkeypatch) -> None:
    monkeypatch.setenv("PORTFOLIO_DASHBOARD_TOKEN", "secret")
    monkeypatch.setenv("LIVE_ORDER_TABLE", "live-orders")
    monkeypatch.setenv("LIVE_ACCOUNT_STATE_TABLE", "account-state")
    tables = {
        "live-orders": FakeTable(live_orders()),
        "account-state": FakeTable(account_rows()),
    }
    seen = []

    def table_factory(name, region):
        seen.append((name, region))
        return tables[name]

    monkeypatch.setattr(lambda_function, "_dynamodb_table", table_factory)

    response = lambda_function.lambda_handler(
        {
            "queryStringParameters": {
                "token": "secret",
                "view": "overview",
                "format": "json",
            }
        },
        None,
    )

    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json"
    payload = json.loads(response["body"])
    assert payload["summary"]["open_positions"] == 2
    assert seen == [("live-orders", "eu-west-2"), ("account-state", "eu-west-2")]


def test_portfolio_lambda_rejects_wrong_token(monkeypatch) -> None:
    monkeypatch.setenv("PORTFOLIO_DASHBOARD_TOKEN", "secret")

    response = lambda_function.lambda_handler(
        {"queryStringParameters": {"token": "wrong"}},
        None,
    )

    assert response["statusCode"] == 401
