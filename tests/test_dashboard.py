from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from exchange_scanner.dashboard import dashboard_payload, render_dashboard_html
from lambda_functions.dashboard import lambda_function


class FakeTable:
    def __init__(self, items):
        self.items = items

    def scan(self, **kwargs):
        return {"Items": self.items}

    def put_item(self, *, Item):
        self.items = [
            item for item in self.items if item.get("trade_id") != Item.get("trade_id")
        ]
        self.items.append(Item)

    def get_item(self, *, Key):
        for item in self.items:
            if item.get("trade_id") == Key["trade_id"]:
                return {"Item": item}
        return {}


class FakeScanOnlyControlTable(FakeTable):
    def get_item(self, *, Key):
        raise PermissionError("dynamodb:GetItem denied")


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
            "closing_edge": Decimal("0.03"),
            "stake": Decimal(1),
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
            "bet_side": "lay",
            "target_odds": Decimal("5.0"),
            "available_at_or_above_target": Decimal(10),
            "edge": Decimal("0.05"),
            "betfair_fair_edge": Decimal("0.02"),
            "betfair_back_lay_spread_pct": Decimal("0.025"),
            "stake": Decimal(1),
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
            "available_at_or_above_target": Decimal(1000),
            "liquidity_status": "not_applicable",
            "edge": Decimal("0.025"),
            "target_clv": Decimal("-0.01"),
            "closing_edge": Decimal("-0.02"),
            "stake": Decimal(1),
            "status": "settled",
            "profit": Decimal(-1),
        },
    ]


def cricket_trade():
    return {
        "trade_id": "paper#4",
        "logged_at": "2026-08-18T13:00:00+00:00",
        "sport_key": "cricket_caribbean_premier_league",
        "event_name": "Saint Lucia Kings v Barbados Tridents",
        "commence_time": "2026-08-18T23:00:00+00:00",
        "outcome_name": "Saint Lucia Kings",
        "target_bookmaker": "Betfair",
        "target_odds": Decimal("2.5"),
        "available_at_or_above_target": Decimal(50),
        "edge": Decimal("0.03"),
        "target_clv": Decimal("0.01"),
        "stake": Decimal(1),
        "status": "settled",
        "profit": Decimal("1.45"),
    }


def next_day_trade():
    row = dict(cricket_trade())
    row["trade_id"] = "paper#5"
    row["logged_at"] = "2026-08-19T09:00:00+00:00"
    row["event_name"] = "Jamaica Kingsmen v Guyana Amazon Warriors"
    return row


def mark_to_market_trade():
    row = dict(trades()[1])
    row["trade_id"] = "paper#6"
    row["event_name"] = "Newcastle v Brighton"
    row["commence_time"] = "2026-08-18T23:00:00+00:00"
    row["target_clv"] = Decimal("0.03")
    return row


def test_dashboard_payload_summarises_and_filters_trades() -> None:
    payload = dashboard_payload(
        FakeTable(trades()),
        filters={"status": "settled", "bookmaker": "Matchbook"},
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )

    assert payload["summary"]["total_trades"] == 1
    assert payload["summary"]["settled_profit"] == 3.136
    assert payload["all_summary"]["total_trades"] == 3
    assert payload["all_summary"]["median_confirmed_liquidity_at_target"] == 17.75
    assert payload["trades"][0]["target_bookmaker"] == "Matchbook"


def test_dashboard_payload_ignores_not_applicable_liquidity_in_medians() -> None:
    payload = dashboard_payload(
        FakeTable(trades()),
        filters={"bookmaker": "Smarkets"},
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )

    assert payload["summary"]["total_trades"] == 1
    assert payload["summary"]["median_confirmed_liquidity_at_target"] == 0.0
    assert payload["summary"]["median_available_risk_at_target"] == 0.0


def test_dashboard_payload_splits_closed_clv_from_mark_to_market() -> None:
    payload = dashboard_payload(
        FakeTable([*trades(), mark_to_market_trade()]),
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )

    assert payload["summary"]["closed_clv_trades"] == 2
    assert payload["summary"]["average_closed_clv"] == 0.005
    assert payload["summary"]["closed_clv_beats"] == 1
    assert payload["summary"]["closed_clv_misses"] == 1
    assert payload["summary"]["mark_to_market_clv_trades"] == 1
    assert payload["summary"]["average_mark_to_market_clv"] == 0.03
    assert payload["summary"]["mark_to_market_clv_beats"] == 1
    assert payload["summary"]["average_clv"] == payload["summary"]["average_closed_clv"]
    assert payload["summary"]["closed_fair_edge_trades"] == 2
    assert payload["summary"]["average_closed_fair_edge"] == pytest.approx(0.005)
    assert payload["summary"]["positive_closed_fair_edge"] == 1


def test_dashboard_payload_filters_by_clv_scope() -> None:
    items = [*trades(), mark_to_market_trade()]
    closed_payload = dashboard_payload(
        FakeTable(items),
        filters={"clv": "closed"},
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )
    mtm_payload = dashboard_payload(
        FakeTable(items),
        filters={"clv": "mtm"},
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )
    missing_payload = dashboard_payload(
        FakeTable(items),
        filters={"clv": "missing"},
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )

    assert {row["trade_id"] for row in closed_payload["trades"]} == {"paper#1", "paper#3"}
    assert {row["trade_id"] for row in mtm_payload["trades"]} == {"paper#6"}
    assert {row["trade_id"] for row in missing_payload["trades"]} == {"paper#2"}


def test_dashboard_payload_includes_filtered_trades_last_24h() -> None:
    payload = dashboard_payload(
        FakeTable([*trades(), cricket_trade(), next_day_trade()]),
        now=datetime(2026, 8, 19, 12, 1, tzinfo=timezone.utc),
    )
    cricket_payload = dashboard_payload(
        FakeTable([*trades(), cricket_trade(), next_day_trade()]),
        filters={"sport": ["cricket"]},
        now=datetime(2026, 8, 19, 12, 1, tzinfo=timezone.utc),
    )

    assert payload["summary"]["trades_last_24h"] == 2
    assert cricket_payload["summary"]["trades_last_24h"] == 2


def test_dashboard_payload_filters_multiple_sports_and_leagues() -> None:
    payload = dashboard_payload(
        FakeTable([*trades(), cricket_trade()]),
        filters={
            "sport": ["soccer", "cricket"],
            "league": ["cricket_caribbean_premier_league", "soccer_epl"],
        },
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )

    assert payload["summary"]["total_trades"] == 4
    assert payload["filter_options"]["sports"] == [
        {"value": "cricket", "label": "Cricket"},
        {"value": "soccer", "label": "Soccer"},
    ]
    assert {"value": "soccer_epl", "label": "Soccer EPL"} in payload["filter_options"]["leagues"]
    assert {row["sport"] for row in payload["sport_results"]} == {"Soccer", "Cricket"}
    assert {row["league"] for row in payload["league_results"]} == {
        "Soccer EPL",
        "Cricket Caribbean Premier League",
    }


def test_dashboard_payload_includes_results_by_venue() -> None:
    payload = dashboard_payload(
        FakeTable(trades()),
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )

    by_venue = {row["venue"]: row for row in payload["venue_results"]}

    assert by_venue["Matchbook"]["settled_won"] == 1
    assert by_venue["Matchbook"]["settled_profit"] == 3.136
    assert by_venue["Betfair"]["open_trades"] == 1
    assert by_venue["Betfair"]["median_confirmed_liquidity_at_target"] == 10
    assert by_venue["Betfair"]["median_available_risk_at_target"] == 40
    assert by_venue["Smarkets"]["settled_lost"] == 1
    assert by_venue["Smarkets"]["settled_roi"] == -1


def test_dashboard_grouped_tables_show_median_available_risk_liquidity() -> None:
    html = render_dashboard_html(
        dashboard_payload(
            FakeTable(trades()),
            filters={"bookmaker": "Betfair"},
            now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
        )
    )

    assert "<td>40.00</td>" in html
    assert "<td>10.00</td>" not in html


def test_dashboard_payload_calculates_lay_liability_and_internal_entry_ev() -> None:
    payload = dashboard_payload(
        FakeTable(trades()),
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )

    lay = next(row for row in payload["trades"] if row["bet_side"] == "lay")

    assert lay["edge_basis"] == "liability"
    assert lay["liability"] == 4.0
    assert lay["risk_odds"] == pytest.approx(1.245)
    assert lay["risk_selection"] == "Not Everton"
    assert lay["available_risk_at_target"] == 40.0
    assert lay["entry_expected_value"] == 0.2
    assert payload["summary"]["open_liability"] == 4.0
    assert payload["summary"]["total_liability"] == 6.0
    assert payload["summary"]["median_available_risk_at_target"] == 32.75
    assert payload["summary"]["entry_expected_value"] == pytest.approx(0.259)


def test_render_dashboard_html_contains_metrics_and_trade_rows() -> None:
    payload = dashboard_payload(
        FakeTable(trades()),
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )
    payload["token"] = "secret"

    html = render_dashboard_html(payload)

    assert "Sports Stat Arb Dashboard" in html
    assert "Trading" in html
    assert "Live" in html
    assert 'name="action"' not in html
    assert "return confirm" not in html
    assert "Arsenal v Chelsea" in html
    assert "Liverpool v Everton" in html
    assert "<th>Side</th>" not in html
    assert "<th>Risk Odds</th>" in html
    assert "<th>Risk</th>" in html
    assert "<th>Liquidity</th>" in html
    assert "<th>Edge Basis</th>" not in html
    assert "<th>Entry EV</th>" not in html
    assert "Arsenal" in html
    assert "Not Everton" in html
    assert "side-badge" not in html
    assert "Risk ROI" not in html
    assert "Open Risk" in html
    assert "Entry EV" not in html
    assert "Results by Venue" in html
    assert "Trades Last 24h" in html
    assert "Results by Sport" in html
    assert "Results by League" in html
    assert "Closed CLV" in html
    assert "Closed Fair Edge" in html
    assert "MTM CLV" in html
    assert "MTM Fair Edge" in html
    assert '<details class="advanced-filters">' in html
    assert "<summary>Advanced Filters</summary>" in html
    assert '<details class="advanced-filters" open>' not in html
    assert "Median Liquidity" in html
    assert "Smarkets" in html
    assert "token=secret&amp;bookmaker=Smarkets" in html
    assert 'name="sport" value="soccer"' in html
    assert 'name="league" value="soccer_epl"' in html
    assert 'name="clv" value="closed"' in html
    assert "token=secret&amp;clv=closed" not in html
    assert "token=secret&amp;status=open" in html
    assert "3.14" in html


def test_render_dashboard_html_shows_paused_without_resume_button() -> None:
    payload = dashboard_payload(
        FakeTable(
            [
                *trades(),
                {
                    "trade_id": "control#trading",
                    "status": "control",
                    "paused": True,
                    "updated_at": "2026-08-18T11:00:00+00:00",
                    "updated_by": "test",
                },
            ]
        ),
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )
    payload["token"] = "secret"

    html = render_dashboard_html(payload)

    assert "Paused" in html
    assert 'name="action"' not in html
    assert "Resume" not in html
    assert "return confirm" not in html


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
        {"queryStringParameters": {"token": "secret", "format": "json", "clv": "missing"}},
        None,
    )

    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json"
    body = json.loads(response["body"])
    assert body["summary"]["total_trades"] == 1
    assert body["trades"][0]["trade_id"] == "paper#2"
    assert body["trading_control"]["enabled"] is True


def test_dashboard_payload_falls_back_to_scan_for_control_state() -> None:
    payload = dashboard_payload(
        FakeScanOnlyControlTable(
            [
                *trades(),
                {
                    "trade_id": "control#trading",
                    "status": "control",
                    "paused": True,
                    "updated_at": "2026-08-18T11:00:00+00:00",
                    "updated_by": "test",
                },
            ]
        ),
        now=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
    )

    assert payload["trading_control"]["paused"] is True
    assert payload["summary"]["total_trades"] == 3


def test_lambda_handler_can_pause_and_resume_trading(monkeypatch) -> None:
    table = FakeTable(trades())
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    monkeypatch.setenv("PAPER_TRADES_TABLE", "paper")
    monkeypatch.setattr(lambda_function, "_dynamodb_table", lambda name, region: table)

    pause_response = lambda_function.lambda_handler(
        {
            "requestContext": {"http": {"method": "POST"}},
            "rawQueryString": "token=secret",
            "body": "action=pause",
        },
        None,
    )

    assert pause_response["statusCode"] == 303
    assert table.get_item(Key={"trade_id": "control#trading"})["Item"]["paused"] is True

    resume_response = lambda_function.lambda_handler(
        {
            "requestContext": {"http": {"method": "POST"}},
            "rawQueryString": "token=secret",
            "body": "action=resume",
        },
        None,
    )

    assert resume_response["statusCode"] == 303
    assert table.get_item(Key={"trade_id": "control#trading"})["Item"]["paused"] is False


def test_lambda_handler_accepts_rest_api_post_shape(monkeypatch) -> None:
    table = FakeTable(trades())
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    monkeypatch.setenv("PAPER_TRADES_TABLE", "paper")
    monkeypatch.setattr(lambda_function, "_dynamodb_table", lambda name, region: table)

    response = lambda_function.lambda_handler(
        {
            "httpMethod": "POST",
            "rawQueryString": "token=secret",
            "body": "action=pause",
        },
        None,
    )

    assert response["statusCode"] == 303
    assert table.get_item(Key={"trade_id": "control#trading"})["Item"]["paused"] is True


def test_lambda_handler_accepts_repeated_sport_and_league_params(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret")
    monkeypatch.setenv("PAPER_TRADES_TABLE", "paper")
    monkeypatch.setattr(
        lambda_function,
        "_dynamodb_table",
        lambda name, region: FakeTable([*trades(), cricket_trade()]),
    )

    response = lambda_function.lambda_handler(
        {
            "rawQueryString": (
                "token=secret&format=json&sport=cricket&sport=soccer"
                "&league=cricket_caribbean_premier_league"
            )
        },
        None,
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["summary"]["total_trades"] == 1
    assert body["trades"][0]["sport_key"] == "cricket_caribbean_premier_league"
