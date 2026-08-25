from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from exchange_scanner.smarkets_liquidity import SmarketsLiquidityClient, match_liquidity


class FakeSmarketsClient:
    def fetch_markets(self, event_id):
        assert event_id == "event-1"
        return [
            {
                "id": "market-1",
                "name": "Full-time result",
                "state": "open",
                "market_type": {"name": "WINNER_3_WAY"},
            }
        ]

    def fetch_contracts(self, market_id):
        assert market_id == "market-1"
        return [
            {"id": "home", "name": "Grimsby Town", "contract_type": {"name": "HOME"}},
            {"id": "draw", "name": "Draw", "contract_type": {"name": "DRAW"}},
            {"id": "away", "name": "Exeter City", "contract_type": {"name": "AWAY"}},
        ]

    def fetch_quotes(self, market_id):
        assert market_id == "market-1"
        return {
            "away": {
                "bids": [{"price": 1961, "quantity": 550000}],
                "offers": [
                    {"price": 2041, "quantity": 425000},
                    {"price": 2083, "quantity": 200000},
                ],
            }
        }


def test_match_liquidity_finds_smarkets_back_depth() -> None:
    match = match_liquidity(
        FakeSmarketsClient(),
        [
            {
                "id": "event-1",
                "name": "Grimsby Town vs Exeter City",
                "start_datetime": "2026-08-14T15:00:00Z",
            }
        ],
        event_name="Grimsby Town v Exeter City",
        commence_time=datetime(2026, 8, 14, 15, tzinfo=timezone.utc),
        outcome_name="Exeter City",
        target_odds=4.9,
    )

    assert match.liquidity_status == "available"
    assert match.smarkets_event_id == "event-1"
    assert match.smarkets_market_id == "market-1"
    assert match.smarkets_contract_id == "away"
    assert match.best_back_odds == pytest.approx(4.89956, rel=1e-4)
    assert match.best_back_available == pytest.approx(42.5)
    assert match.available_at_or_above_target == pytest.approx(42.5)
    assert match.best_lay_odds == pytest.approx(5.09944, rel=1e-4)


def test_match_liquidity_uses_bids_for_smarkets_lay_depth() -> None:
    match = match_liquidity(
        FakeSmarketsClient(),
        [
            {
                "id": "event-1",
                "name": "Grimsby Town vs Exeter City",
                "start_datetime": "2026-08-14T15:00:00Z",
            }
        ],
        event_name="Grimsby Town v Exeter City",
        commence_time=datetime(2026, 8, 14, 15, tzinfo=timezone.utc),
        outcome_name="Exeter City",
        target_odds=5.1,
        bet_side="lay",
    )

    assert match.liquidity_status == "available"
    assert match.available_at_or_above_target == pytest.approx(55)


def test_match_liquidity_marks_missing_smarkets_event() -> None:
    match = match_liquidity(
        FakeSmarketsClient(),
        [],
        event_name="Grimsby Town v Exeter City",
        commence_time=datetime(2026, 8, 14, 15, tzinfo=timezone.utc),
        outcome_name="Exeter City",
        target_odds=4.9,
    )

    assert match.liquidity_status == "not_matched"


def test_smarkets_login_uses_classic_session_token_flow_by_default() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={"token": "fresh-token", "stop": "2026-08-14T12:30:00Z"},
        )

    client = SmarketsLiquidityClient()
    client.http = httpx.Client(
        base_url="https://api.smarkets.com/v3",
        transport=httpx.MockTransport(handler),
    )

    response = client.login(username="user@example.com", password="secret")

    assert response["token"] == "fresh-token"
    assert client.http.headers["Authorization"] == "Bearer fresh-token"
    assert requests[0].url.path == "/v3/sessions/"
    assert requests[0].read().decode("utf-8") == (
        '{"username":"user@example.com","password":"secret",'
        '"remember":true,"use_auth_v2":false}'
    )
