from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from exchange_scanner.live_execution import LiveOrderIntent
from exchange_scanner.live_venues import (
    BetfairLiveExecutor,
    MatchbookLiveExecutor,
    SmarketsLiveExecutor,
    _normalise_pem,
    executors_from_env,
    matchbook_login,
    smarkets_login,
)
from exchange_scanner.the_odds_api import ValueSignal


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


def intent(**overrides) -> LiveOrderIntent:
    values = {
        "order_id": "live#abc",
        "paper_trade_id": "paper#abc",
        "signal": signal(),
        "limit_odds": 4.2,
        "stake": 1.0,
        "liability": 1.0,
        "sizing_method": "flat",
        "flat_order_risk": 1.0,
        "kelly_fraction": 0.1,
        "full_kelly_fraction": 0.01,
        "bankroll": 1000.0,
        "available_at_target": 10.0,
        "dry_run": False,
        "venue_metadata": {"event_id": "event-x", "market_id": "market-x", "runner_id": "123"},
    }
    values.update(overrides)
    return LiveOrderIntent(**values)


def test_matchbook_executor_fetches_account_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/account/balance"
        return httpx.Response(
            200,
            json={
                "currency": "GBP",
                "balance": 50,
                "free-funds": 46.5,
                "exposure": 3.5,
            },
        )

    executor = MatchbookLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.matchbook.test",
        transport=httpx.MockTransport(handler),
    )

    snapshot = executor.fetch_account_snapshot()

    assert snapshot == {
        "venue": "Matchbook",
        "currency": "GBP",
        "balance": 50.0,
        "available_funds": 46.5,
        "exposure": 3.5,
        "retained_commission": 0.0,
    }


def test_smarkets_executor_fetches_account_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts/"
        return httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "currency": "GBP",
                        "balance": "100.00",
                        "available_balance": "91.77",
                        "exposure": "-8.22",
                    }
                ]
            },
        )

    executor = SmarketsLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.smarkets.test",
        transport=httpx.MockTransport(handler),
    )

    snapshot = executor.fetch_account_snapshot()

    assert snapshot["venue"] == "Smarkets"
    assert snapshot["balance"] == 100.0
    assert snapshot["available_funds"] == 91.77
    assert snapshot["exposure"] == -8.22


def test_betfair_executor_fetches_account_snapshot() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {
                    "availableToBetBalance": 49,
                    "exposure": -1,
                    "retainedCommission": 0,
                },
                "id": 1,
            },
        )

    executor = BetfairLiveExecutor(app_key="app", session_token="token")
    executor.http = httpx.Client(transport=httpx.MockTransport(handler))

    snapshot = executor.fetch_account_snapshot()

    assert requests[0].url == httpx.URL(
        "https://api.betfair.com/exchange/account/json-rpc/v1"
    )
    payload = json.loads(requests[0].read().decode())
    assert payload["method"] == "AccountAPING/v1.0/getAccountFunds"
    assert snapshot["venue"] == "Betfair"
    assert snapshot["balance"] == 50.0
    assert snapshot["available_funds"] == 49.0
    assert snapshot["exposure"] == -1.0


def test_matchbook_executor_fetches_offer_level_settlement() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/reports/v2/bets/settled"
        return httpx.Response(
            200,
            json={
                "offset": 0,
                "per-page": 100,
                "total": 1,
                "markets": [
                    {
                        "commission": 0.021239788,
                        "selections": [
                            {
                                "bets": [
                                    {
                                        "offer-id": 34232966392200057,
                                        "result": "WIN",
                                        "profit-and-loss": 1.0619894,
                                        "commission": 0,
                                        "net-profit-and-loss": 1.0619894,
                                        "settled-time": "2026-09-01T20:40:00.000Z",
                                    }
                                ]
                            }
                        ]
                    }
                ],
            },
        )

    executor = MatchbookLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.matchbook.test",
        transport=httpx.MockTransport(handler),
    )

    settlement = executor.fetch_order_settlement(
        {
            "venue_order_id": "34232966392200057",
            "commence_time": "2026-09-01T18:45:00+00:00",
        }
    )

    assert settlement == {
        "settlement_source": "matchbook_settled_bets",
        "gross_profit": pytest.approx(1.0619894),
        "commission": pytest.approx(0.021239788),
        "net_profit": pytest.approx(1.040749612),
        "venue_result": "WIN",
        "venue_settled_at": "2026-09-01T20:40:00.000Z",
    }


def test_smarkets_executor_confirms_settlement_from_account_activity() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/orders/order-1/":
            return httpx.Response(
                200,
                json={
                    "id": "order-1",
                    "state": "settled",
                    "outcome": "winner",
                    "last_modified_datetime": "2026-09-02T20:00:00Z",
                },
            )
        assert request.url.path == "/accounts/activity/"
        if request.url.params.get("order_id"):
            return httpx.Response(
                200,
                json={
                    "account_activity": [
                        {
                            "order_id": "order-1",
                            "market_id": "market-1",
                            "source": "order.settle",
                            "amount": "1.62",
                            "extra": "winner",
                            "timestamp": "2026-09-02T20:00:01Z",
                        },
                        {
                            "order_id": "order-1",
                            "market_id": "market-1",
                            "source": "order.execute",
                            "amount": "-1.00",
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "account_activity": [
                    {
                        "market_id": "market-1",
                        "source": "market.settle",
                        "money_change": "1.58",
                        "commission": "-0.04",
                        "timestamp": "2026-09-02T20:00:01Z",
                    },
                    {
                        "order_id": "order-1",
                        "market_id": "market-1",
                        "source": "order.settle",
                        "amount": "1.62",
                        "extra": "winner",
                        "timestamp": "2026-09-02T20:00:01Z",
                    },
                ]
            },
        )

    executor = SmarketsLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.smarkets.test",
        transport=httpx.MockTransport(handler),
    )

    settlement = executor.fetch_order_settlement({"venue_order_id": "order-1"})

    assert settlement == {
        "settlement_source": "smarkets_market_activity",
        "gross_profit": pytest.approx(1.62),
        "commission": pytest.approx(0.04),
        "net_profit": pytest.approx(1.58),
        "venue_result": "WINNER",
        "venue_settled_at": "2026-09-02T20:00:01Z",
    }
    assert requests[1].url.params["order_id"] == "order-1"
    assert requests[1].url.params["sort"] == "-seq,-subseq"
    assert requests[2].url.params["market_id"] == "market-1"


def test_smarkets_executor_does_not_confirm_without_market_money_change() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orders/order-1/":
            return httpx.Response(200, json={"id": "order-1", "state": "settled"})
        if request.url.params.get("order_id"):
            return httpx.Response(
                200,
                json={
                    "account_activity": [
                        {
                            "order_id": "order-1",
                            "market_id": "market-1",
                            "source": "order.settle",
                            "amount": "-1.00",
                            "extra": "loser",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"account_activity": []})

    executor = SmarketsLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.smarkets.test",
        transport=httpx.MockTransport(handler),
    )

    assert executor.fetch_order_settlement({"venue_order_id": "order-1"}) is None


def test_betfair_executor_fetches_cleared_order_settlement() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.read().decode()))
        return httpx.Response(
            200,
            json={
                "result": {
                    "clearedOrders": [
                        {
                            "betId": "440833343461",
                            "marketId": "1.261706472",
                            "betOutcome": "LOST",
                            "settledDate": "2026-09-01T20:39:02.000Z",
                            "profit": -1.0,
                        }
                    ]
                }
            },
        )

    executor = BetfairLiveExecutor(app_key="app", session_token="token")
    executor.http = httpx.Client(transport=httpx.MockTransport(handler))

    settlement = executor.fetch_order_settlement({"venue_order_id": "440833343461"})

    assert requests[0]["method"] == "SportsAPING/v1.0/listClearedOrders"
    assert requests[0]["params"]["groupBy"] == "BET"
    assert settlement == {
        "settlement_source": "betfair_cleared_orders",
        "gross_profit": -1.0,
        "commission": 0.0,
        "net_profit": -1.0,
        "venue_result": "LOST",
        "venue_settled_at": "2026-09-01T20:39:02.000Z",
    }


def test_matchbook_executor_posts_limit_offer() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "cancelled"})
        return httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "id": "offer-1",
                        "status": "open",
                        "matched-amount": 0,
                        "remaining-amount": 1,
                    }
                ]
            },
        )

    executor = MatchbookLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.matchbook.test",
        transport=httpx.MockTransport(handler),
    )

    result = executor.place_limit_order(intent())

    assert result.status == "cancelled"
    assert result.venue_order_id == "offer-1"
    assert [request.method for request in requests] == ["POST", "DELETE"]
    assert requests[0].url.path == "/v2/offers"
    assert requests[1].url.path == "/v2/offers/offer-1"
    payload = json.loads(requests[0].read().decode())
    assert payload["offers"][0]["runner-id"] == 123
    assert payload["offers"][0]["stake"] == 1.0
    assert payload["offers"][0]["client-reference"] == "live#abc"


def test_matchbook_executor_does_not_mark_zero_fill_execution_complete_as_matched() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "cancelled"})
        return httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "id": "offer-1",
                        "status": "execution-complete",
                        "matched-amount": 0,
                        "remaining-amount": 1,
                    }
                ]
            },
        )

    executor = MatchbookLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.matchbook.test",
        transport=httpx.MockTransport(handler),
    )

    result = executor.place_limit_order(intent())

    assert result.status == "cancelled"
    assert result.matched_size == 0
    assert result.remaining_size == 0
    assert [request.method for request in requests] == ["POST", "DELETE"]


def test_matchbook_executor_reads_v2_matched_offer_fields() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "id": "offer-1",
                        "status": "matched",
                        "odds": 3.1,
                        "stake": 0.48,
                        "remaining": 0.00001,
                        "potential-liability": 1.008,
                        "matched-bets": [
                            {
                                "id": "bet-1",
                                "offer-id": "offer-1",
                                "odds": 3.1,
                                "stake": 0.47999,
                                "potential-liability": 1.00798,
                            }
                        ],
                    }
                ]
            },
        )

    executor = MatchbookLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.matchbook.test",
        transport=httpx.MockTransport(handler),
    )

    result = executor.place_limit_order(
        intent(
            limit_odds=3.1,
            stake=0.47619047619047616,
            signal=signal(target_odds=3.1),
        )
    )

    assert result.status == "matched"
    assert result.venue_order_id == "offer-1"
    assert result.matched_size == 0.47999
    assert result.avg_matched_odds == 3.1
    assert result.remaining_size == 0.00001
    assert [request.method for request in requests] == ["POST"]


def test_matchbook_executor_preserves_large_runner_ids() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "cancelled"})
        return httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "id": "offer-1",
                        "status": "open",
                        "matched-amount": 0,
                        "remaining-amount": 1,
                    }
                ]
            },
        )

    executor = MatchbookLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.matchbook.test",
        transport=httpx.MockTransport(handler),
    )

    executor.place_limit_order(
        intent(venue_metadata={"runner_id": "34206486631200081"})
    )

    payload = json.loads(requests[0].read().decode())
    assert payload["offers"][0]["runner-id"] == 34206486631200081


def test_matchbook_executor_cancels_partial_unmatched_remainder() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "cancelled"})
        return httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "id": "offer-1",
                        "status": "open",
                        "matched-amount": 0.4,
                        "remaining-amount": 0.6,
                        "average-odds": 4.2,
                    }
                ]
            },
        )

    executor = MatchbookLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.matchbook.test",
        transport=httpx.MockTransport(handler),
    )

    result = executor.place_limit_order(intent())

    assert result.status == "partially_matched_cancelled"
    assert result.matched_size == 0.4
    assert result.remaining_size == 0
    assert [request.method for request in requests] == ["POST", "DELETE"]


def test_smarkets_executor_posts_limit_order() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"state": "cancelled"})
        return httpx.Response(
            200,
            json={
                "order": {
                    "id": "order-1",
                    "state": "open",
                    "quantity_filled": 0,
                    "quantity_unfilled": 10000,
                }
            },
        )

    executor = SmarketsLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.smarkets.test",
        transport=httpx.MockTransport(handler),
    )

    result = executor.place_limit_order(
        intent(
            signal=signal(target_bookmaker="Smarkets"),
            venue_metadata={"event_id": "event-x", "market_id": "market-x", "runner_id": "456"},
        )
    )

    assert result.status == "cancelled"
    assert result.venue_order_id == "order-1"
    assert [request.method for request in requests] == ["POST", "DELETE"]
    assert requests[0].url.path == "/orders/"
    assert requests[1].url.path == "/orders/order-1/"
    payload = json.loads(requests[0].read().decode())
    assert payload["market_id"] == "market-x"
    assert payload["contract_id"] == "456"
    assert payload["side"] == "buy"
    assert payload["price"] == 2381
    assert payload["quantity"] == 41999


def test_smarkets_executor_parses_filled_order_response() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "order": {
                    "id": "order-1",
                    "state": "filled",
                    "price": 6061,
                    "quantity": 25387,
                    "quantity_filled": 25387,
                    "quantity_unfilled": 0,
                    "average_price_matched": 6061,
                }
            },
        )

    executor = SmarketsLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.smarkets.test",
        transport=httpx.MockTransport(handler),
    )

    result = executor.place_limit_order(
        intent(
            signal=signal(target_bookmaker="Smarkets", bet_side="lay", target_odds=1.65),
            venue_metadata={"event_id": "event-x", "market_id": "market-x", "runner_id": "456"},
            limit_odds=1.65,
            stake=1.5385,
            liability=1,
        )
    )

    assert result.status == "matched"
    assert result.venue_order_id == "order-1"
    assert result.matched_size == pytest.approx(2.5387 * 0.6061)
    assert result.remaining_size == 0
    assert result.avg_matched_odds == 10000 / 6061
    assert [request.method for request in requests] == ["POST"]
    payload = json.loads(requests[0].read().decode())
    assert payload["price"] == 6061
    assert payload["quantity"] == 25387


def test_smarkets_executor_refreshes_filled_order_after_cancel_too_late() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(
                400,
                json={"data": None, "error_type": "ORDER_CANCEL_REJECTED_TOO_LATE"},
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "order-1",
                    "state": "filled",
                    "price": 3788,
                    "quantity": 26399,
                    "quantity_filled": 26399,
                    "quantity_unfilled": 0,
                    "average_price_matched": 3788,
                },
            )
        return httpx.Response(
            200,
            json={
                "order": {
                    "id": "order-1",
                    "state": "open",
                    "price": 3788,
                    "quantity": 26399,
                    "quantity_filled": 0,
                    "quantity_unfilled": 26399,
                }
            },
        )

    executor = SmarketsLiveExecutor(session_token="token")
    executor.http = httpx.Client(
        base_url="https://api.smarkets.test",
        transport=httpx.MockTransport(handler),
    )

    result = executor.place_limit_order(
        intent(
            signal=signal(target_bookmaker="Smarkets", bet_side="back", target_odds=2.64),
            venue_metadata={"event_id": "event-x", "market_id": "market-x", "runner_id": "456"},
            limit_odds=2.64,
            stake=1,
            liability=1,
        )
    )

    assert result.status == "matched"
    assert result.error is None
    assert result.matched_size == pytest.approx(0.99999412)
    assert result.remaining_size == 0
    assert [request.method for request in requests] == ["POST", "DELETE", "GET"]
    assert requests[2].url.path == "/orders/order-1/"


def test_betfair_executor_places_limit_order_with_customer_ref() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "result": {
                    "instructionReports": [
                        {
                            "status": "SUCCESS",
                            "orderStatus": "EXECUTABLE",
                            "betId": "bet-1",
                            "sizeMatched": 0,
                        }
                    ]
                }
            },
        )

    executor = BetfairLiveExecutor(app_key="app", session_token="token")
    executor.http = httpx.Client(transport=httpx.MockTransport(handler))

    result = executor.place_limit_order(
        intent(
            signal=signal(target_bookmaker="Betfair"),
            venue_metadata={"event_id": "", "market_id": "1.234", "runner_id": "789"},
        )
    )

    assert result.status == "submitted"
    assert result.venue_order_id == "bet-1"
    payload = json.loads(requests[0].read().decode())
    assert payload["method"] == "SportsAPING/v1.0/placeOrders"
    instruction = payload["params"]["instructions"][0]
    assert payload["params"]["marketId"] == "1.234"
    assert instruction["selectionId"] == 789
    assert payload["params"]["customerRef"] == instruction["customerOrderRef"]
    assert payload["params"]["customerRef"].startswith("bf")
    assert payload["params"]["customerRef"].isalnum()
    assert len(payload["params"]["customerRef"]) <= 32
    assert instruction["limitOrder"]["timeInForce"] == "FILL_OR_KILL"


def test_betfair_executor_resolves_market_runner_before_placing_limit_order() -> None:
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        methods.append(payload["method"])
        if payload["method"] == "SportsAPING/v1.0/listMarketCatalogue":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "marketId": "1.234",
                            "event": {"name": "Arsenal v Chelsea"},
                            "runners": [
                                {"selectionId": 789, "runnerName": "Arsenal"},
                                {"selectionId": 987, "runnerName": "Chelsea"},
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "instructionReports": [
                        {
                            "status": "SUCCESS",
                            "orderStatus": "EXECUTABLE",
                            "betId": "bet-1",
                            "sizeMatched": 0,
                        }
                    ]
                }
            },
        )

    executor = BetfairLiveExecutor(app_key="app", session_token="token")
    executor.http = httpx.Client(transport=httpx.MockTransport(handler))

    result = executor.place_limit_order(
        intent(
            signal=signal(target_bookmaker="Betfair"),
            available_at_target=None,
            venue_metadata={},
        )
    )

    assert result.status == "submitted"
    assert result.venue_order_id == "bet-1"
    assert methods == [
        "SportsAPING/v1.0/listMarketCatalogue",
        "SportsAPING/v1.0/placeOrders",
    ]


def test_executors_from_env_builds_configured_venues(monkeypatch) -> None:
    monkeypatch.setattr(SmarketsLiveExecutor, "keep_alive", lambda self: {})

    executors = executors_from_env(
        {
            "MATCHBOOK_SESSION_TOKEN": "matchbook-token",
            "SMARKETS_SESSION_TOKEN": "smarkets-token",
            "BETFAIR_APP_KEY": "betfair-app",
            "BETFAIR_SESSION_TOKEN": "betfair-token",
        }
    )

    assert sorted(executors) == [
        "betfair",
        "betfair_ex_eu",
        "betfair_ex_uk",
        "matchbook",
        "smarkets",
    ]


def test_matchbook_login_returns_session_token(monkeypatch) -> None:
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return httpx.Response(
            200,
            json={"session-token": "session-1"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("exchange_scanner.live_venues.httpx.post", fake_post)

    token = matchbook_login(
        username="user@example.com",
        password="secret",
        mfa_code="123456",
        timeout=7,
    )

    assert token == "session-1"
    assert captured["url"] == "https://api.matchbook.com/bpapi/rest/security/session"
    assert captured["json"] == {
        "username": "user@example.com",
        "password": "secret",
        "mfa-code": "123456",
    }
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["timeout"] == 7


def test_executors_from_env_logs_into_matchbook_with_credentials(monkeypatch) -> None:
    calls = []

    def fake_login(**kwargs):
        calls.append(kwargs)
        return MatchbookLiveExecutor(session_token="logged-in-token")

    monkeypatch.setattr(MatchbookLiveExecutor, "login", staticmethod(fake_login))

    executors = executors_from_env(
        {
            "MATCHBOOK_USERNAME": "user@example.com",
            "MATCHBOOK_PASSWORD": "secret",
            "MATCHBOOK_MFA_CODE": "123456",
        }
    )

    assert list(executors) == ["matchbook"]
    assert calls == [
        {
            "username": "user@example.com",
            "password": "secret",
            "mfa_code": "123456",
        }
    ]


def test_executors_from_env_skips_failed_matchbook_login(monkeypatch) -> None:
    def fake_login(**kwargs):
        raise httpx.HTTPStatusError(
            "forbidden",
            request=httpx.Request("POST", "https://api.matchbook.test/session"),
            response=httpx.Response(403),
        )

    monkeypatch.setattr(MatchbookLiveExecutor, "login", staticmethod(fake_login))
    monkeypatch.setattr(SmarketsLiveExecutor, "keep_alive", lambda self: {})

    executors = executors_from_env(
        {
            "MATCHBOOK_USERNAME": "user@example.com",
            "MATCHBOOK_PASSWORD": "secret",
            "SMARKETS_SESSION_TOKEN": "smarkets-token",
            "BETFAIR_APP_KEY": "betfair-app",
            "BETFAIR_SESSION_TOKEN": "betfair-token",
        }
    )

    assert sorted(executors) == [
        "betfair",
        "betfair_ex_eu",
        "betfair_ex_uk",
        "smarkets",
    ]


def test_smarkets_login_returns_session_token(monkeypatch) -> None:
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return httpx.Response(
            200,
            json={"token": "smarkets-token"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("exchange_scanner.live_venues.httpx.post", fake_post)

    token = smarkets_login(
        username="user@example.com",
        password="secret",
        timeout=7,
    )

    assert token == "smarkets-token"
    assert captured["url"] == "https://api.smarkets.com/v3/sessions/"
    assert captured["json"] == {
        "username": "user@example.com",
        "password": "secret",
        "remember": True,
        "use_auth_v2": False,
    }
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["timeout"] == 7


def test_executors_from_env_logs_into_smarkets_with_credentials(monkeypatch) -> None:
    calls = []

    def fake_login(**kwargs):
        calls.append(kwargs)
        return "logged-in-token"

    monkeypatch.setattr("exchange_scanner.live_venues.smarkets_login", fake_login)

    executors = executors_from_env(
        {
            "SMARKETS_USERNAME": "user@example.com",
            "SMARKETS_PASSWORD": "secret",
        }
    )

    assert list(executors) == ["smarkets"]
    assert calls == [
        {
            "username": "user@example.com",
            "password": "secret",
        }
    ]


def test_executors_from_env_reuses_valid_smarkets_token(monkeypatch) -> None:
    keep_alive_calls = []

    def fake_keep_alive(self):
        keep_alive_calls.append(self)
        return {"account": {"id": "account-1"}}

    def fake_login(**kwargs):
        raise AssertionError("Smarkets login should not run with a valid token")

    monkeypatch.setattr(SmarketsLiveExecutor, "keep_alive", fake_keep_alive)
    monkeypatch.setattr("exchange_scanner.live_venues.smarkets_login", fake_login)

    executors = executors_from_env(
        {
            "SMARKETS_USERNAME": "user@example.com",
            "SMARKETS_PASSWORD": "secret",
            "SMARKETS_SESSION_TOKEN": "valid-token",
        }
    )

    assert list(executors) == ["smarkets"]
    assert len(keep_alive_calls) == 1


def test_executors_from_env_refreshes_expired_smarkets_token_in_secret(monkeypatch) -> None:
    login_calls = []
    secret_updates = []

    class FakeSecretsManager:
        def get_secret_value(self, **kwargs):
            return {
                "SecretString": json.dumps(
                    {
                        "SMARKETS_USERNAME": "user@example.com",
                        "SMARKETS_PASSWORD": "secret",
                        "SMARKETS_SESSION_TOKEN": "expired-token",
                    }
                )
            }

        def put_secret_value(self, **kwargs):
            secret_updates.append(kwargs)
            return {}

    def fake_client(service_name, **kwargs):
        assert service_name == "secretsmanager"
        assert kwargs == {"region_name": "eu-west-2"}
        return FakeSecretsManager()

    def fake_keep_alive(self):
        raise httpx.HTTPStatusError(
            "expired",
            request=httpx.Request("GET", "https://api.smarkets.test/accounts/"),
            response=httpx.Response(401),
        )

    def fake_login(**kwargs):
        login_calls.append(kwargs)
        return "fresh-token"

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=fake_client))
    monkeypatch.setattr(SmarketsLiveExecutor, "keep_alive", fake_keep_alive)
    monkeypatch.setattr("exchange_scanner.live_venues.smarkets_login", fake_login)

    executors = executors_from_env(
        {
            "EXCHANGE_CREDENTIALS_SECRET_ID": "sports-stat-arb/live-exchange-credentials",
            "EXCHANGE_CREDENTIALS_SECRET_REGION": "eu-west-2",
        }
    )

    assert list(executors) == ["smarkets"]
    assert login_calls == [
        {
            "username": "user@example.com",
            "password": "secret",
        }
    ]
    assert secret_updates[0]["SecretId"] == "sports-stat-arb/live-exchange-credentials"
    updated_secret = json.loads(secret_updates[0]["SecretString"])
    assert updated_secret["SMARKETS_SESSION_TOKEN"] == "fresh-token"


def test_executors_from_env_uses_betfair_cert_secret(monkeypatch) -> None:
    cert_pem = "-----BEGIN CERTIFICATE-----\\nabc\\n-----END CERTIFICATE-----"
    key_pem = "-----BEGIN PRIVATE KEY-----\\ndef\\n-----END PRIVATE KEY-----"
    secret_calls = []
    login_calls = []

    class FakeSecretsManager:
        def get_secret_value(self, **kwargs):
            secret_calls.append(kwargs)
            return {
                "SecretString": json.dumps(
                    {
                        "cert_pem": cert_pem,
                        "key_pem": key_pem,
                    }
                )
            }

    def fake_client(service_name, **kwargs):
        assert service_name == "secretsmanager"
        assert kwargs == {"region_name": "eu-west-2"}
        return FakeSecretsManager()

    def fake_certificate_login(**kwargs):
        login_calls.append(kwargs)
        assert kwargs["cert_file"].read_text(encoding="utf-8") == cert_pem.replace("\\n", "\n") + "\n"
        assert kwargs["key_file"].read_text(encoding="utf-8") == key_pem.replace("\\n", "\n") + "\n"
        return "fresh-betfair-token"

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=fake_client))
    monkeypatch.setattr("exchange_scanner.live_venues.certificate_login", fake_certificate_login)

    executors = executors_from_env(
        {
            "BETFAIR_APP_KEY": "app-key",
            "BETFAIR_USERNAME": "betfair-user",
            "BETFAIR_PASSWORD": "betfair-password",
            "BETFAIR_CERT_SECRET_ID": "sports-stat-arb/betfair-cert",
            "BETFAIR_CERT_SECRET_REGION": "eu-west-2",
        }
    )

    assert sorted(executors) == ["betfair", "betfair_ex_eu", "betfair_ex_uk"]
    assert secret_calls == [{"SecretId": "sports-stat-arb/betfair-cert"}]
    assert login_calls[0]["username"] == "betfair-user"
    assert login_calls[0]["password"] == "betfair-password"
    assert login_calls[0]["app_key"] == "app-key"


def test_normalise_pem_wraps_single_line_pem() -> None:
    pem = "-----BEGIN CERTIFICATE----- abcdef -----END CERTIFICATE-----"

    assert _normalise_pem(pem) == (
        "-----BEGIN CERTIFICATE-----\n"
        "abcdef\n"
        "-----END CERTIFICATE-----\n"
    )


def test_executors_from_env_uses_exchange_credentials_secret(monkeypatch) -> None:
    cert_pem = "-----BEGIN CERTIFICATE-----\\nabc\\n-----END CERTIFICATE-----"
    key_pem = "-----BEGIN PRIVATE KEY-----\\ndef\\n-----END PRIVATE KEY-----"
    secret_calls = []
    secret_updates = []
    matchbook_calls = []
    smarkets_calls = []
    betfair_calls = []

    class FakeSecretsManager:
        def get_secret_value(self, **kwargs):
            secret_calls.append(kwargs)
            return {
                "SecretString": json.dumps(
                    {
                        "matchbook_username": "matchbook-user",
                        "matchbook_password": "matchbook-password",
                        "smarkets_username": "smarkets-user",
                        "smarkets_password": "smarkets-password",
                        "betfair_app_key": "betfair-app",
                        "betfair_username": "betfair-user",
                        "betfair_password": "betfair-password",
                        "cert_pem": cert_pem,
                        "key_pem": key_pem,
                    }
                )
            }

        def put_secret_value(self, **kwargs):
            secret_updates.append(kwargs)
            return {}

    def fake_client(service_name, **kwargs):
        assert service_name == "secretsmanager"
        assert kwargs == {"region_name": "eu-west-2"}
        return FakeSecretsManager()

    def fake_matchbook_login(**kwargs):
        matchbook_calls.append(kwargs)
        return MatchbookLiveExecutor(session_token="matchbook-token")

    def fake_smarkets_login(**kwargs):
        smarkets_calls.append(kwargs)
        return "smarkets-token"

    def fake_certificate_login(**kwargs):
        betfair_calls.append(kwargs)
        assert kwargs["cert_file"].read_text(encoding="utf-8") == cert_pem.replace("\\n", "\n") + "\n"
        assert kwargs["key_file"].read_text(encoding="utf-8") == key_pem.replace("\\n", "\n") + "\n"
        return "betfair-token"

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=fake_client))
    monkeypatch.setattr(MatchbookLiveExecutor, "login", staticmethod(fake_matchbook_login))
    monkeypatch.setattr("exchange_scanner.live_venues.smarkets_login", fake_smarkets_login)
    monkeypatch.setattr("exchange_scanner.live_venues.certificate_login", fake_certificate_login)

    executors = executors_from_env(
        {
            "EXCHANGE_CREDENTIALS_SECRET_ID": "sports-stat-arb/live-exchange-credentials",
            "EXCHANGE_CREDENTIALS_SECRET_REGION": "eu-west-2",
        }
    )

    assert sorted(executors) == [
        "betfair",
        "betfair_ex_eu",
        "betfair_ex_uk",
        "matchbook",
        "smarkets",
    ]
    assert secret_calls == [{"SecretId": "sports-stat-arb/live-exchange-credentials"}]
    assert secret_updates[0]["SecretId"] == "sports-stat-arb/live-exchange-credentials"
    assert matchbook_calls == [
        {
            "username": "matchbook-user",
            "password": "matchbook-password",
            "mfa_code": "",
        }
    ]
    assert smarkets_calls == [
        {
            "username": "smarkets-user",
            "password": "smarkets-password",
        }
    ]
    assert betfair_calls[0]["username"] == "betfair-user"
    assert betfair_calls[0]["password"] == "betfair-password"
    assert betfair_calls[0]["app_key"] == "betfair-app"
