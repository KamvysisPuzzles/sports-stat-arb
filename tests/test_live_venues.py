from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

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
                    "matched_quantity": 0,
                    "remaining_quantity": 10000,
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
    assert payload["contract_id"] == "456"
    assert payload["side"] == "buy"
    assert payload["quantity"] == 10000


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
    assert instruction["customerOrderRef"] == "live#abc"
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


def test_executors_from_env_builds_configured_venues() -> None:
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
        return SmarketsLiveExecutor(session_token="logged-in-token")

    monkeypatch.setattr(SmarketsLiveExecutor, "login", staticmethod(fake_login))

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


def test_executors_from_env_prefers_smarkets_credentials_over_token(monkeypatch) -> None:
    calls = []

    def fake_login(**kwargs):
        calls.append(kwargs)
        return SmarketsLiveExecutor(session_token="fresh-token")

    monkeypatch.setattr(SmarketsLiveExecutor, "login", staticmethod(fake_login))

    executors = executors_from_env(
        {
            "SMARKETS_USERNAME": "user@example.com",
            "SMARKETS_PASSWORD": "secret",
            "SMARKETS_SESSION_TOKEN": "expired-token",
        }
    )

    assert list(executors) == ["smarkets"]
    assert calls == [
        {
            "username": "user@example.com",
            "password": "secret",
        }
    ]


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

    def fake_client(service_name, **kwargs):
        assert service_name == "secretsmanager"
        assert kwargs == {"region_name": "eu-west-2"}
        return FakeSecretsManager()

    def fake_matchbook_login(**kwargs):
        matchbook_calls.append(kwargs)
        return MatchbookLiveExecutor(session_token="matchbook-token")

    def fake_smarkets_login(**kwargs):
        smarkets_calls.append(kwargs)
        return SmarketsLiveExecutor(session_token="smarkets-token")

    def fake_certificate_login(**kwargs):
        betfair_calls.append(kwargs)
        assert kwargs["cert_file"].read_text(encoding="utf-8") == cert_pem.replace("\\n", "\n") + "\n"
        assert kwargs["key_file"].read_text(encoding="utf-8") == key_pem.replace("\\n", "\n") + "\n"
        return "betfair-token"

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=fake_client))
    monkeypatch.setattr(MatchbookLiveExecutor, "login", staticmethod(fake_matchbook_login))
    monkeypatch.setattr(SmarketsLiveExecutor, "login", staticmethod(fake_smarkets_login))
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
