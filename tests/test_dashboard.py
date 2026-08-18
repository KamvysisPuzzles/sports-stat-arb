from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from exchange_scanner.dashboard import dashboard_payload, render_dashboard_html
from lambda_functions.dashboard import lambda_function


class FakeTable:
    def __init__(self, items):
        self.items = items

    def scan(self, **kwargs):
        return {"Items": self.items}


def trades():
    return [
        {
            "trade_id": "paper#1",
            "logged_at": "2026-08-18T10:00:00+00:00",
            "sport_key": "soccer_epl",
            "event_name": "Arsenal v Chelsea",
            "commence_time": "2026-08-18T20:00:00+00:00",
            "outcome_name": "Arsenal",
            "target_bookmaker": "Matchbook",
            "target_odds": Decimal("4.2"),
            "available_at_or_above_target": Decimal("25.5"),
            "edge": Decimal("0.034"),
            "target_clv": Decimal("0.02"),
            "stake": Decimal("1"),
            "status": "settled",
            "profit": Decimal("3.136"),
        },
        {
            "trade_id": "paper#2",
            "logged_at": "2026-08-18T11:00:00+00:00",
            "sport_key": "soccer_epl",
            "event_name": "Liverpool v Everton",
            "commence_time": "2026-08-18T21:00:00+00:00",
            "outcome_name": "Everton",
            "target_bookmaker": "Betfair",
            "target_odds": Decimal("5.0"),
            "available_at_or_above_target": Decimal("10"),
            "edge": Decimal("0.05"),
            "stake": Decimal("1"),
            "status": "open",
        },
        {
            "trade_id": "paper#3",
            "logged_at": "2026-08-18T12:00:00+00:00",
            "sport_key": "soccer_epl",
            "event_name": "Spurs v West Ham",
            "commence_time": "2026-08-18T22:00:00+00:00",
            "outcome_name": "Draw",
            "target_bookmaker": "Smarkets",
            "target_odds": Decimal("3.5"),
            "available_at_or_above_target": Decimal("1000"),
            "edge": Decimal("0.025"),
            "target_clv": Decimal("-0.01"),
            "stake": Decimal("1"),
            "status": "settled",
            "profit": Decimal("-1"),
        },
    ]


def test_dashboard_payload_summarises_and_filters_trades() -> None:
    payload = dashboard_payload(
        FakeTable(trades()),
        filters={"status": "settled", "bookmaker": "Matchbook"},
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )

    assert payload["summary"]["total_trades"] == 1
    assert payload["summary"]["settled_profit"] == 3.136
    assert payload["all_summary"]["total_trades"] == 3
    assert payload["all_summary"]["median_confirmed_liquidity_at_target"] == 25.5
    assert payload["trades"][0]["target_bookmaker"] == "Matchbook"


def test_dashboard_payload_includes_results_by_venue() -> None:
    payload = dashboard_payload(
        FakeTable(trades()),
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )

    by_venue = {row["venue"]: row for row in payload["venue_results"]}

    assert by_venue["Matchbook"]["settled_won"] == 1
    assert by_venue["Matchbook"]["settled_profit"] == 3.136
    assert by_venue["Betfair"]["open_trades"] == 1
    assert by_venue["Smarkets"]["settled_lost"] == 1
    assert by_venue["Smarkets"]["settled_roi"] == -1


def test_render_dashboard_html_contains_metrics_and_trade_rows() -> None:
    payload = dashboard_payload(
        FakeTable(trades()),
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )
    payload["token"] = "secret"

    html = render_dashboard_html(payload)

    assert "Sports Stat Arb Dashboard" in html
    assert "Arsenal v Chelsea" in html
    assert "Liverpool v Everton" in html
    assert "Results by Venue" in html
    assert "Median Liquidity" in html
    assert "Smarkets" in html
    assert "token=secret&amp;status=open" in html
    assert "3.14" in html


def test_lambda_handler_requires_token(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")

    response = lambda_function.lambda_handler({"queryStringParameters": {"token": "wrong"}}, None)

    assert response["statusCode"] == 401


def test_lambda_handler_returns_json(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    monkeypatch.setenv("PAPER_TRADES_TABLE", "paper")
    monkeypatch.setattr(
        lambda_function,
        "_dynamodb_table",
        lambda name, region: FakeTable(trades()),
    )

    response = lambda_function.lambda_handler(
        {"queryStringParameters": {"token": "secret", "format": "json"}},
        None,
    )

    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json"
    body = json.loads(response["body"])
    assert body["summary"]["total_trades"] == 3
    assert body["venue_results"][0]["venue"] == "Matchbook"
