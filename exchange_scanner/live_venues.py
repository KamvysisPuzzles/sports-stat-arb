from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import textwrap
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import httpx

from exchange_scanner.betfair_auth import certificate_login
from exchange_scanner.betfair_liquidity import (
    BETFAIR_BETTING_API_URL,
    BETFAIR_SOCCER_EVENT_TYPE_ID,
    resolve_market_runner,
)
from exchange_scanner.live_execution import LiveOrderIntent, LiveOrderResult, LiveOrderStatus
from exchange_scanner.matchbook_liquidity import MATCHBOOK_API_BASE
from exchange_scanner.smarkets_liquidity import SMARKETS_API_BASE

MATCHBOOK_LOGIN_URL = "https://api.matchbook.com/bpapi/rest/security/session"
MATCHBOOK_OFFERS_PATH = "/v2/offers"
BETFAIR_ACCOUNT_API_URL = "https://api.betfair.com/exchange/account/json-rpc/v1"
MIN_REMAINDER_TO_CANCEL = 0.01
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VenueCredentials:
    matchbook_session_token: str = ""
    matchbook_username: str = ""
    matchbook_password: str = ""
    matchbook_mfa_code: str = ""
    smarkets_session_token: str = ""
    smarkets_username: str = ""
    smarkets_password: str = ""
    betfair_app_key: str = ""
    betfair_session_token: str = ""
    betfair_username: str = ""
    betfair_password: str = ""
    betfair_cert_file: str = ""
    betfair_key_file: str = ""
    betfair_cert_secret_id: str = ""
    betfair_cert_secret_region: str = ""


class MatchbookLiveExecutor:
    def __init__(self, *, session_token: str, timeout: float = 15.0) -> None:
        self.http = httpx.Client(
            base_url=MATCHBOOK_API_BASE,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "session-token": session_token,
            },
        )

    @classmethod
    def login(
        cls,
        *,
        username: str,
        password: str,
        mfa_code: str = "",
        timeout: float = 15.0,
    ) -> MatchbookLiveExecutor:
        session_token = matchbook_login(
            username=username,
            password=password,
            mfa_code=mfa_code,
            timeout=timeout,
        )
        return cls(session_token=session_token, timeout=timeout)

    def place_limit_order(self, intent: LiveOrderIntent) -> LiveOrderResult:
        runner_id = _required(intent.venue_metadata.get("runner_id"), "matchbook_runner_id")
        payload = {
            "odds-type": "DECIMAL",
            "exchange-type": "back-lay",
            "offers": [
                {
                    "runner-id": _intish(runner_id),
                    "side": intent.signal.bet_side.casefold(),
                    "odds": _money(intent.limit_odds, places=3),
                    "stake": _money(intent.stake),
                    "keep-in-play": False,
                    "client-reference": intent.order_id,
                }
            ],
        }
        response = self.http.post(MATCHBOOK_OFFERS_PATH, json=payload)
        response.raise_for_status()
        data = response.json()
        offer = _first(data.get("offers")) or data
        venue_order_id = str(offer.get("id") or offer.get("offer-id") or "")
        matched, remaining, avg_odds = _matchbook_offer_fill(offer, fallback_stake=intent.stake)
        result = LiveOrderResult(
            order_id=intent.order_id,
            status=_status_from_sizes(
                offer.get("status") or data.get("status") or "submitted",
                matched,
                remaining,
            ),
            venue_order_id=venue_order_id or None,
            matched_size=matched,
            avg_matched_odds=avg_odds,
            remaining_size=remaining,
        )
        return self._cancel_unmatched_remainder(result)

    def fetch_order_status(self, order: dict[str, Any]) -> LiveOrderStatus:
        venue_order_id = str(order.get("venue_order_id") or "")
        if not venue_order_id:
            return LiveOrderStatus(order_id=str(order["order_id"]), status="unknown", error="missing_venue_order_id")
        response = self.http.get(f"{MATCHBOOK_OFFERS_PATH}/{venue_order_id}")
        response.raise_for_status()
        data = response.json()
        offer = _first(data.get("offers")) or data
        matched, remaining, avg_odds = _matchbook_offer_fill(offer)
        return LiveOrderStatus(
            order_id=str(order["order_id"]),
            status=_status_from_sizes(offer.get("status"), matched, remaining),
            venue_order_id=venue_order_id,
            matched_size=matched,
            avg_matched_odds=avg_odds,
            remaining_size=remaining,
        )

    def fetch_account_snapshot(self) -> dict[str, Any]:
        response = self.http.get("/account/balance")
        response.raise_for_status()
        payload = response.json()
        balance = _first_number(payload, "balance", "account-balance", "account_balance")
        available = _first_number(
            payload,
            "free-funds",
            "free_funds",
            "available-balance",
            "available_balance",
            "available",
            "balance",
        )
        exposure = _first_number(payload, "exposure", "current-exposure", "current_exposure")
        if exposure is None and balance is not None and available is not None:
            exposure = available - balance
        return _account_snapshot(
            venue="Matchbook",
            payload=payload,
            balance=balance,
            available_funds=available,
            exposure=exposure,
        )

    def fetch_order_settlement(self, order: dict[str, Any]) -> dict[str, Any] | None:
        venue_order_id = str(order.get("venue_order_id") or "")
        if not venue_order_id:
            return None
        params: dict[str, Any] = {"offset": 0, "per-page": 100}
        commence = _parse_datetime(order.get("commence_time"))
        if commence is not None:
            params["after"] = _matchbook_datetime(commence - timedelta(days=1))
            params["before"] = _matchbook_datetime(datetime.now(timezone.utc) + timedelta(days=1))
        matched_bets: list[dict[str, Any]] = []
        while params["offset"] < 1000:
            response = self.http.get("/reports/v2/bets/settled", params=params)
            response.raise_for_status()
            payload = response.json()
            matched_bets.extend(
                _matchbook_offer_settlements(payload, offer_id=venue_order_id)
            )
            total = int(_float(payload.get("total")))
            params["offset"] += int(_float(payload.get("per-page"))) or 100
            if params["offset"] >= total:
                break
        if not matched_bets:
            return None
        gross_profit = sum(_float(item.get("profit-and-loss")) for item in matched_bets)
        commission = sum(_float(item.get("commission")) for item in matched_bets)
        net_profit = sum(_float(item.get("net-profit-and-loss")) for item in matched_bets)
        settled_at = max(
            (str(item.get("settled-time") or "") for item in matched_bets),
            default="",
        )
        results = sorted({str(item.get("result") or "").upper() for item in matched_bets})
        return _settlement_payload(
            source="matchbook_settled_bets",
            gross_profit=gross_profit,
            commission=commission,
            net_profit=net_profit,
            venue_result=",".join(result for result in results if result),
            settled_at=settled_at,
        )

    def _cancel_unmatched_remainder(self, result: LiveOrderResult) -> LiveOrderResult:
        if not result.venue_order_id or not _has_unmatched_remainder(result):
            return result
        try:
            response = self.http.delete(f"{MATCHBOOK_OFFERS_PATH}/{result.venue_order_id}")
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - preserve order result with cancel failure.
            return _result_with_error(
                result,
                f"cancel_after_place_failed:{type(exc).__name__}: {exc}",
            )
        return _cancelled_remainder_result(result)


class SmarketsLiveExecutor:
    def __init__(self, *, session_token: str, timeout: float = 15.0) -> None:
        self.http = httpx.Client(
            base_url=SMARKETS_API_BASE,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Session-Token {session_token}",
            },
        )

    @classmethod
    def login(
        cls,
        *,
        username: str,
        password: str,
        timeout: float = 15.0,
    ) -> SmarketsLiveExecutor:
        session_token = smarkets_login(
            username=username,
            password=password,
            timeout=timeout,
        )
        return cls(session_token=session_token, timeout=timeout)

    def keep_alive(self) -> dict[str, Any]:
        response = self.http.get("/accounts/")
        response.raise_for_status()
        return response.json()

    def fetch_account_snapshot(self) -> dict[str, Any]:
        payload = self.keep_alive()
        account = _first(payload.get("accounts")) or payload.get("account") or payload
        return _account_snapshot(
            venue="Smarkets",
            payload=account,
            balance=_first_number(account, "balance"),
            available_funds=_first_number(
                account,
                "available_balance",
                "available-balance",
                "available",
            ),
            exposure=_first_number(account, "exposure"),
        )

    def fetch_order_settlement(self, order: dict[str, Any]) -> dict[str, Any] | None:
        venue_order_id = str(order.get("venue_order_id") or "")
        if not venue_order_id:
            return None
        response = self.http.get(f"/orders/{venue_order_id}/")
        response.raise_for_status()
        payload = response.json()
        venue_order = payload.get("order") or payload
        if str(venue_order.get("state") or "").casefold() != "settled":
            return None
        response = self.http.get(
            "/accounts/activity/",
            params={"order_id": venue_order_id, "limit": 500, "sort": "-seq,-subseq"},
        )
        response.raise_for_status()
        activity = [
            item
            for item in response.json().get("account_activity", [])
            if str(item.get("order_id") or "") == venue_order_id
        ]
        settlement_rows = [
            item
            for item in activity
            if item.get("source") == "order.settle" and item.get("amount") is not None
        ]
        if not settlement_rows:
            return None

        market_ids = {
            str(item.get("market_id"))
            for item in settlement_rows
            if item.get("market_id") not in {None, ""}
        }
        if len(market_ids) != 1:
            return None
        market_id = next(iter(market_ids))
        response = self.http.get(
            "/accounts/activity/",
            params={"market_id": market_id, "limit": 500, "sort": "-seq,-subseq"},
        )
        response.raise_for_status()
        market_activity = [
            item
            for item in response.json().get("account_activity", [])
            if str(item.get("market_id") or "") == market_id
        ]
        market_settlement = next(
            (
                item
                for item in market_activity
                if item.get("source") == "market.settle"
                and item.get("money_change") is not None
            ),
            None,
        )
        if market_settlement is None:
            return None

        gross_profit = sum(_float(item.get("amount")) for item in settlement_rows)
        market_gross_profit = sum(
            _float(item.get("amount"))
            for item in market_activity
            if item.get("source") == "order.settle"
            and item.get("amount") is not None
        )
        market_net_profit = _float(market_settlement.get("money_change"))
        market_commission = max(0.0, market_gross_profit - market_net_profit)
        positive_market_profit = sum(
            _float(item.get("amount"))
            for item in market_activity
            if item.get("source") == "order.settle"
            and item.get("amount") is not None
            and _float(item.get("amount")) > 0
        )
        commission = 0.0
        if gross_profit > 0 and positive_market_profit > 0:
            commission = market_commission * gross_profit / positive_market_profit
        gross_profit = _money(gross_profit)
        commission = _money(commission)
        net_profit = _money(gross_profit - commission)
        settled_at = max(
            (str(item.get("timestamp") or "") for item in settlement_rows),
            default=str(market_settlement.get("timestamp") or ""),
        )
        venue_results = sorted(
            {str(item.get("extra") or "").upper() for item in settlement_rows}
        )
        return _settlement_payload(
            source="smarkets_market_activity",
            gross_profit=gross_profit,
            commission=commission,
            net_profit=net_profit,
            venue_result=",".join(result for result in venue_results if result)
            or str(venue_order.get("outcome") or "").upper(),
            settled_at=settled_at,
        )

    def place_limit_order(self, intent: LiveOrderIntent) -> LiveOrderResult:
        market_id = _required(intent.venue_metadata.get("market_id"), "smarkets_market_id")
        contract_id = _required(intent.venue_metadata.get("runner_id"), "smarkets_contract_id")
        price = _smarkets_price(intent.limit_odds)
        payload = {
            "market_id": str(market_id),
            "contract_id": str(contract_id),
            "side": _smarkets_side(intent.signal.bet_side),
            "price": price,
            "quantity": _smarkets_order_quantity(intent, price=price),
            "reference": intent.order_id,
        }
        response = self.http.post("/orders/", json=payload)
        response.raise_for_status()
        data = response.json()
        order = _first(data.get("orders")) or data.get("order") or data
        venue_order_id = str(order.get("id") or order.get("order_id") or "")
        matched, remaining = _smarkets_order_fill(
            order,
            bet_side=intent.signal.bet_side,
            fallback_stake=intent.stake,
        )
        result = LiveOrderResult(
            order_id=intent.order_id,
            status=_status_from_sizes(order.get("state"), matched, remaining),
            venue_order_id=venue_order_id or None,
            matched_size=matched,
            avg_matched_odds=_smarkets_avg_odds(order),
            remaining_size=remaining,
        )
        return self._cancel_unmatched_remainder(result, bet_side=intent.signal.bet_side)

    def fetch_order_status(self, order: dict[str, Any]) -> LiveOrderStatus:
        venue_order_id = str(order.get("venue_order_id") or "")
        if not venue_order_id:
            return LiveOrderStatus(order_id=str(order["order_id"]), status="unknown", error="missing_venue_order_id")
        response = self.http.get(f"/orders/{venue_order_id}/")
        response.raise_for_status()
        data = response.json()
        payload = data.get("order") or data
        matched, remaining = _smarkets_order_fill(
            payload,
            bet_side=str(order.get("bet_side") or "back"),
        )
        return LiveOrderStatus(
            order_id=str(order["order_id"]),
            status=_status_from_sizes(payload.get("state"), matched, remaining),
            venue_order_id=venue_order_id,
            matched_size=matched,
            avg_matched_odds=_smarkets_avg_odds(payload),
            remaining_size=remaining,
        )

    def _cancel_unmatched_remainder(
        self,
        result: LiveOrderResult,
        *,
        bet_side: str,
    ) -> LiveOrderResult:
        if not result.venue_order_id or not _has_unmatched_remainder(result):
            return result
        try:
            response = self.http.delete(f"/orders/{result.venue_order_id}/")
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - preserve order result with cancel failure.
            refreshed = self._refreshed_order_after_cancel_failure(
                result,
                bet_side=bet_side,
            )
            if refreshed is not None and not _has_unmatched_remainder(refreshed):
                return refreshed
            return _result_with_error(
                result,
                f"cancel_after_place_failed:{type(exc).__name__}: {exc}",
            )
        return _cancelled_remainder_result(result)

    def _refreshed_order_after_cancel_failure(
        self,
        result: LiveOrderResult,
        *,
        bet_side: str,
    ) -> LiveOrderResult | None:
        try:
            response = self.http.get(f"/orders/{result.venue_order_id}/")
            response.raise_for_status()
        except (httpx.HTTPError, ValueError, TypeError):
            return None
        data = response.json()
        order = data.get("order") or data
        matched, remaining = _smarkets_order_fill(
            order,
            bet_side=bet_side,
            fallback_stake=result.remaining_size or 0,
        )
        return LiveOrderResult(
            order_id=result.order_id,
            status=_status_from_sizes(order.get("state"), matched, remaining),
            venue_order_id=result.venue_order_id,
            matched_size=matched,
            avg_matched_odds=_smarkets_avg_odds(order),
            remaining_size=remaining,
        )


class BetfairLiveExecutor:
    def __init__(self, *, app_key: str, session_token: str, timeout: float = 15.0) -> None:
        self.http = httpx.Client(
            timeout=timeout,
            headers={
                "X-Application": app_key,
                "X-Authentication": session_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def place_limit_order(self, intent: LiveOrderIntent) -> LiveOrderResult:
        venue_metadata = self._betfair_venue_metadata(intent)
        market_id = _required(venue_metadata.get("market_id"), "betfair_market_id")
        selection_id = _required(venue_metadata.get("runner_id"), "betfair_selection_id")
        customer_ref = _betfair_customer_ref(intent.order_id)
        payload = self._rpc(
            "SportsAPING/v1.0/placeOrders",
            {
                "marketId": str(market_id),
                "instructions": [
                    {
                        "selectionId": int(float(selection_id)),
                        "side": "BACK" if intent.signal.bet_side.casefold() == "back" else "LAY",
                        "orderType": "LIMIT",
                        "limitOrder": {
                            "size": _money(intent.stake),
                            "price": _money(intent.limit_odds, places=2),
                            "persistenceType": "LAPSE",
                            "timeInForce": "FILL_OR_KILL",
                        },
                        "customerOrderRef": customer_ref,
                    }
                ],
                "customerRef": customer_ref,
            },
        )
        report = _first(payload.get("instructionReports")) or {}
        venue_order_id = str(report.get("betId") or "")
        size_matched = _float(report.get("sizeMatched"))
        return LiveOrderResult(
            order_id=intent.order_id,
            status=_betfair_status(report),
            venue_order_id=venue_order_id or None,
            matched_size=size_matched,
            avg_matched_odds=_float(report.get("averagePriceMatched")),
            remaining_size=max(0.0, intent.stake - size_matched),
            error=_betfair_error(report),
        )

    def fetch_order_status(self, order: dict[str, Any]) -> LiveOrderStatus:
        venue_order_id = str(order.get("venue_order_id") or "")
        if not venue_order_id:
            return LiveOrderStatus(order_id=str(order["order_id"]), status="unknown", error="missing_venue_order_id")
        payload = self._rpc(
            "SportsAPING/v1.0/listCurrentOrders",
            {
                "betIds": [venue_order_id],
                "orderProjection": "ALL",
                "includeItemDescription": True,
            },
        )
        current = _first(payload.get("currentOrders")) or {}
        size_matched = _float(current.get("sizeMatched"))
        size_remaining = _float(current.get("sizeRemaining"))
        return LiveOrderStatus(
            order_id=str(order["order_id"]),
            status=_status_from_sizes(current.get("status"), size_matched, size_remaining),
            venue_order_id=venue_order_id,
            matched_size=size_matched,
            avg_matched_odds=_float(current.get("averagePriceMatched")),
            remaining_size=size_remaining,
        )

    def fetch_account_snapshot(self) -> dict[str, Any]:
        payload = self._rpc_at(
            BETFAIR_ACCOUNT_API_URL,
            "AccountAPING/v1.0/getAccountFunds",
            {},
        )
        available = _first_number(payload, "availableToBetBalance")
        exposure = _first_number(payload, "exposure")
        retained_commission = _first_number(payload, "retainedCommission") or 0.0
        balance = None
        if available is not None:
            balance = available - (exposure or 0.0) + retained_commission
        return _account_snapshot(
            venue="Betfair",
            payload=payload,
            balance=balance,
            available_funds=available,
            exposure=exposure,
            retained_commission=retained_commission,
        )

    def fetch_order_settlement(self, order: dict[str, Any]) -> dict[str, Any] | None:
        venue_order_id = str(order.get("venue_order_id") or "")
        if not venue_order_id:
            return None
        payload = self._rpc(
            "SportsAPING/v1.0/listClearedOrders",
            {
                "betStatus": "SETTLED",
                "betIds": [venue_order_id],
                "groupBy": "BET",
                "includeItemDescription": True,
            },
        )
        cleared = next(
            (
                item
                for item in payload.get("clearedOrders", [])
                if str(item.get("betId") or "") == venue_order_id
            ),
            None,
        )
        if cleared is None:
            return None
        gross_profit = _float(cleared.get("profit"))
        commission = _float(cleared.get("commission"))
        market_id = str(cleared.get("marketId") or "")
        if gross_profit > 0 and commission <= 0 and market_id:
            market_payload = self._rpc(
                "SportsAPING/v1.0/listClearedOrders",
                {
                    "betStatus": "SETTLED",
                    "marketIds": [market_id],
                    "groupBy": "MARKET",
                },
            )
            market = _first(market_payload.get("clearedOrders")) or {}
            commission = _float(market.get("commission"))
        return _settlement_payload(
            source="betfair_cleared_orders",
            gross_profit=gross_profit,
            commission=commission,
            net_profit=gross_profit - commission,
            venue_result=str(cleared.get("betOutcome") or "").upper(),
            settled_at=str(cleared.get("settledDate") or ""),
        )

    def fetch_market_catalogue(
        self,
        *,
        event_name: str,
        commence_time,
        market_key: str = "h2h",
        max_results: int = 10,
        use_text_query: bool = True,
    ) -> list[dict[str, Any]]:
        market_filter: dict[str, Any] = {
            "eventTypeIds": [BETFAIR_SOCCER_EVENT_TYPE_ID],
            "marketTypeCodes": [market_key],
            "marketStartTime": {
                "from": (commence_time - timedelta(hours=12)).isoformat(),
                "to": (commence_time + timedelta(hours=12)).isoformat(),
            },
        }
        if use_text_query:
            market_filter["textQuery"] = event_name
        payload = self._rpc(
            "SportsAPING/v1.0/listMarketCatalogue",
            {
                "filter": market_filter,
                "marketProjection": [
                    "EVENT",
                    "RUNNER_DESCRIPTION",
                    "MARKET_START_TIME",
                ],
                "sort": "FIRST_TO_START",
                "maxResults": str(max_results),
            },
        )
        return payload if isinstance(payload, list) else []

    def _betfair_venue_metadata(self, intent: LiveOrderIntent) -> dict[str, Any]:
        if intent.venue_metadata.get("market_id") and intent.venue_metadata.get("runner_id"):
            return intent.venue_metadata
        match = resolve_market_runner(
            self,
            event_name=intent.signal.event_name,
            commence_time=intent.signal.commence_time,
            market_key=intent.signal.market_key,
            outcome_name=intent.signal.outcome_name,
        )
        if match is None:
            return intent.venue_metadata
        return {
            **intent.venue_metadata,
            "market_id": match.betfair_market_id,
            "runner_id": match.betfair_selection_id,
            "match_score": match.match_score,
        }

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        return self._rpc_at(BETFAIR_BETTING_API_URL, method, params)

    def _rpc_at(self, url: str, method: str, params: dict[str, Any]) -> Any:
        response = self.http.post(
            url,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"Betfair API error for {method}: {payload['error']}")
        return payload.get("result") or {}


def executors_from_env(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    exchange_secret_id = env.get("EXCHANGE_CREDENTIALS_SECRET_ID", "")
    exchange_secret_region = (
        env.get("EXCHANGE_CREDENTIALS_SECRET_REGION")
        or env.get("AWS_REGION")
        or None
    )
    secret_payload = _exchange_credentials_secret_from_env(env)
    credentials = VenueCredentials(
        matchbook_session_token=_credential(
            env, secret_payload, "MATCHBOOK_SESSION_TOKEN", "matchbook_session_token"
        ),
        matchbook_username=_credential(
            env, secret_payload, "MATCHBOOK_USERNAME", "matchbook_username"
        ),
        matchbook_password=_credential(
            env, secret_payload, "MATCHBOOK_PASSWORD", "matchbook_password"
        ),
        matchbook_mfa_code=_credential(
            env, secret_payload, "MATCHBOOK_MFA_CODE", "matchbook_mfa_code"
        ),
        smarkets_session_token=_credential(
            env, secret_payload, "SMARKETS_SESSION_TOKEN", "smarkets_session_token"
        ),
        smarkets_username=_credential(env, secret_payload, "SMARKETS_USERNAME", "smarkets_username"),
        smarkets_password=_credential(env, secret_payload, "SMARKETS_PASSWORD", "smarkets_password"),
        betfair_app_key=_credential(env, secret_payload, "BETFAIR_APP_KEY", "betfair_app_key")
        or _credential(env, secret_payload, "BETFAIR_APP_KEY_DELAYED", "betfair_app_key_delayed"),
        betfair_session_token=_credential(
            env, secret_payload, "BETFAIR_SESSION_TOKEN", "betfair_session_token"
        ),
        betfair_username=_credential(env, secret_payload, "BETFAIR_USERNAME", "betfair_username"),
        betfair_password=_credential(env, secret_payload, "BETFAIR_PASSWORD", "betfair_password"),
        betfair_cert_file=_credential(env, secret_payload, "BETFAIR_CERT_FILE", "betfair_cert_file"),
        betfair_key_file=_credential(env, secret_payload, "BETFAIR_KEY_FILE", "betfair_key_file"),
        betfair_cert_secret_id=_credential(
            env, secret_payload, "BETFAIR_CERT_SECRET_ID", "betfair_cert_secret_id"
        ),
        betfair_cert_secret_region=_credential(
            env, secret_payload, "BETFAIR_CERT_SECRET_REGION", "betfair_cert_secret_region"
        ),
    )
    executors: dict[str, Any] = {}
    if credentials.matchbook_session_token:
        executors["matchbook"] = MatchbookLiveExecutor(
            session_token=credentials.matchbook_session_token
        )
    elif credentials.matchbook_username and credentials.matchbook_password:
        try:
            executors["matchbook"] = MatchbookLiveExecutor.login(
                username=credentials.matchbook_username,
                password=credentials.matchbook_password,
                mfa_code=credentials.matchbook_mfa_code,
            )
        except Exception as exc:  # noqa: BLE001 - one venue auth failure should not abort all live execution.
            logger.warning("Skipping Matchbook live executor: %s", exc)
    if credentials.smarkets_session_token:
        smarkets = SmarketsLiveExecutor(
            session_token=credentials.smarkets_session_token
        )
        try:
            smarkets.keep_alive()
            executors["smarkets"] = smarkets
        except Exception as exc:  # noqa: BLE001 - expired tokens are refreshed below when credentials exist.
            logger.warning("Smarkets session token is not reusable: %s", exc)
    if "smarkets" not in executors and credentials.smarkets_username and credentials.smarkets_password:
        try:
            smarkets_token = smarkets_login(
                username=credentials.smarkets_username,
                password=credentials.smarkets_password,
            )
            executors["smarkets"] = SmarketsLiveExecutor(session_token=smarkets_token)
            if exchange_secret_id and secret_payload:
                _update_smarkets_session_token_secret(
                    secret_id=exchange_secret_id,
                    region_name=exchange_secret_region,
                    payload=secret_payload,
                    session_token=smarkets_token,
                )
        except Exception as exc:  # noqa: BLE001 - one venue auth failure should not abort all live execution.
            logger.warning("Skipping Smarkets live executor: %s", exc)
    betfair_session_token = credentials.betfair_session_token
    if (
        not betfair_session_token
        and not credentials.betfair_cert_file
        and not credentials.betfair_key_file
        and credentials.betfair_app_key
        and credentials.betfair_username
        and credentials.betfair_password
        and (secret_payload or credentials.betfair_cert_secret_id)
    ):
        cert_file, key_file = _betfair_cert_files(
            payload=secret_payload,
            secret_id=credentials.betfair_cert_secret_id,
            region_name=credentials.betfair_cert_secret_region or None,
        )
        credentials = replace(
            credentials,
            betfair_cert_file=str(cert_file),
            betfair_key_file=str(key_file),
        )
    if not betfair_session_token and _can_cert_login(credentials):
        try:
            betfair_session_token = certificate_login(
                username=credentials.betfair_username,
                password=credentials.betfair_password,
                app_key=credentials.betfair_app_key,
                cert_file=Path(credentials.betfair_cert_file),
                key_file=Path(credentials.betfair_key_file),
            )
        except Exception as exc:  # noqa: BLE001 - one venue auth failure should not abort all live execution.
            logger.warning("Skipping Betfair live executor: %s", exc)
            betfair_session_token = ""
    if credentials.betfair_app_key and betfair_session_token:
        betfair = BetfairLiveExecutor(
            app_key=credentials.betfair_app_key,
            session_token=betfair_session_token,
        )
        executors["betfair"] = betfair
        executors["betfair_ex_uk"] = betfair
        executors["betfair_ex_eu"] = betfair
    return executors


def _exchange_credentials_secret_from_env(env: dict[str, str]) -> dict[str, Any]:
    secret_id = env.get("EXCHANGE_CREDENTIALS_SECRET_ID", "")
    if not secret_id:
        return {}
    return _load_json_secret(
        secret_id=secret_id,
        region_name=env.get("EXCHANGE_CREDENTIALS_SECRET_REGION")
        or env.get("AWS_REGION")
        or None,
    )


def _credential(
    env: dict[str, str],
    secret_payload: dict[str, Any],
    env_key: str,
    secret_key: str,
) -> str:
    value = env.get(env_key)
    if value:
        return str(value)
    value = secret_payload.get(secret_key)
    if value:
        return str(value)
    value = secret_payload.get(env_key)
    return str(value) if value else ""


def _can_cert_login(credentials: VenueCredentials) -> bool:
    return all(
        (
            credentials.betfair_app_key,
            credentials.betfair_username,
            credentials.betfair_password,
            credentials.betfair_cert_file,
            credentials.betfair_key_file,
        )
    )


def _load_json_secret(*, secret_id: str, region_name: str | None = None) -> dict[str, Any]:
    import boto3

    client_kwargs = {"region_name": region_name} if region_name else {}
    response = boto3.client("secretsmanager", **client_kwargs).get_secret_value(
        SecretId=secret_id
    )
    secret_string = response.get("SecretString")
    if not secret_string:
        raise RuntimeError(f"Secret {secret_id} has no SecretString")
    payload = json.loads(secret_string)
    if not isinstance(payload, dict):
        raise TypeError(f"Secret {secret_id} must contain a JSON object")
    return payload


def _update_smarkets_session_token_secret(
    *,
    secret_id: str,
    region_name: str | None,
    payload: dict[str, Any],
    session_token: str,
) -> None:
    import boto3

    updated = dict(payload)
    if "SMARKETS_SESSION_TOKEN" in updated:
        updated["SMARKETS_SESSION_TOKEN"] = session_token
    if "smarkets_session_token" in updated or "SMARKETS_SESSION_TOKEN" not in updated:
        updated["smarkets_session_token"] = session_token
    client_kwargs = {"region_name": region_name} if region_name else {}
    boto3.client("secretsmanager", **client_kwargs).put_secret_value(
        SecretId=secret_id,
        SecretString=json.dumps(updated),
    )


def _betfair_cert_files(
    *,
    payload: dict[str, Any],
    secret_id: str,
    region_name: str | None = None,
) -> tuple[Path, Path]:
    cert_pem = payload.get("cert_pem") or payload.get("BETFAIR_CERT_PEM")
    key_pem = payload.get("key_pem") or payload.get("BETFAIR_KEY_PEM")
    if cert_pem and key_pem:
        return _write_betfair_cert_files(cert_pem=cert_pem, key_pem=key_pem)
    if not secret_id:
        raise RuntimeError(
            "Betfair certificate PEMs missing from exchange credentials secret"
        )
    return _betfair_cert_files_from_secret(secret_id=secret_id, region_name=region_name)


def _betfair_cert_files_from_secret(
    *,
    secret_id: str,
    region_name: str | None = None,
) -> tuple[Path, Path]:
    payload = _load_json_secret(secret_id=secret_id, region_name=region_name)
    cert_pem = payload.get("cert_pem") or payload.get("BETFAIR_CERT_PEM")
    key_pem = payload.get("key_pem") or payload.get("BETFAIR_KEY_PEM")
    if not cert_pem or not key_pem:
        raise RuntimeError(
            "Betfair certificate secret must contain cert_pem/key_pem "
            "or BETFAIR_CERT_PEM/BETFAIR_KEY_PEM"
        )
    return _write_betfair_cert_files(cert_pem=cert_pem, key_pem=key_pem)


def _write_betfair_cert_files(*, cert_pem: Any, key_pem: Any) -> tuple[Path, Path]:
    cert_file = Path("/tmp/betfair-client.crt")
    key_file = Path("/tmp/betfair-client.key")
    cert_file.write_text(_normalise_pem(str(cert_pem)), encoding="utf-8")
    key_file.write_text(_normalise_pem(str(key_pem)), encoding="utf-8")
    key_file.chmod(0o600)
    return cert_file, key_file


def _normalise_pem(value: str) -> str:
    value = value.strip().replace("\\n", "\n")
    if "\n" in value:
        return value + "\n"

    match = re.fullmatch(
        r"-----BEGIN ([^-]+)-----\s+(.+?)\s+-----END \1-----",
        value,
    )
    if not match:
        return value + "\n"

    label = match.group(1)
    body = re.sub(r"\s+", "", match.group(2))
    wrapped_body = "\n".join(textwrap.wrap(body, width=64))
    return f"-----BEGIN {label}-----\n{wrapped_body}\n-----END {label}-----\n"


def matchbook_login(
    *,
    username: str,
    password: str,
    mfa_code: str = "",
    timeout: float = 15.0,
) -> str:
    payload = {
        "username": username,
        "password": password,
    }
    if mfa_code:
        payload["mfa-code"] = mfa_code
    response = httpx.post(
        MATCHBOOK_LOGIN_URL,
        json=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    session_token = data.get("session-token")
    if not session_token:
        raise RuntimeError("Matchbook login failed: missing session-token")
    return str(session_token)


def smarkets_login(
    *,
    username: str,
    password: str,
    timeout: float = 15.0,
) -> str:
    response = httpx.post(
        f"{SMARKETS_API_BASE}/sessions/",
        json={
            "username": username,
            "password": password,
            "remember": True,
            "use_auth_v2": False,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    token = data.get("token")
    if not token:
        raise RuntimeError("Smarkets login failed: missing token")
    return str(token)


def _required(value: Any, field: str) -> Any:
    if value in {None, ""}:
        raise ValueError(f"Missing {field}")
    return value


def _money(value: float, *, places: int = 2) -> float:
    quant = "0." + ("0" * (places - 1)) + "1"
    return float(Decimal(str(value)).quantize(Decimal(quant), rounding=ROUND_HALF_UP))


def _intish(value: Any) -> int | str:
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    text = str(value)
    try:
        return int(text)
    except (TypeError, ValueError):
        return text


def _first(values: Any) -> dict[str, Any] | None:
    if isinstance(values, list) and values:
        first = values[0]
        return first if isinstance(first, dict) else None
    return None


def _float(value: Any) -> float:
    try:
        if value in {None, ""}:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first_number(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value in {None, ""}:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _account_snapshot(
    *,
    venue: str,
    payload: dict[str, Any],
    balance: float | None,
    available_funds: float | None,
    exposure: float | None,
    retained_commission: float = 0.0,
) -> dict[str, Any]:
    if available_funds is None:
        raise RuntimeError(f"{venue} account response is missing available funds")
    return {
        "venue": venue,
        "currency": str(
            payload.get("currency")
            or payload.get("currency_code")
            or payload.get("currency-code")
            or "GBP"
        ).upper(),
        "balance": balance if balance is not None else available_funds,
        "available_funds": available_funds,
        "exposure": exposure or 0.0,
        "retained_commission": retained_commission,
    }


def _normal_status(value: Any) -> str:
    raw = str(value or "").casefold().replace("-", "_")
    if raw in {"executable", "unmatched", "open"}:
        return "submitted"
    if raw in {"execution_complete", "matched", "filled"}:
        return "matched"
    if raw in {"cancelled", "canceled"}:
        return "cancelled"
    if raw in {"failed", "rejected"}:
        return raw
    return raw or "submitted"


def _matchbook_offer_fill(offer: dict[str, Any], *, fallback_stake: float = 0.0) -> tuple[float, float, float]:
    remaining_value = _first_present(
        offer,
        ("remaining", "remaining-amount", "remaining_amount", "open-amount", "open_amount"),
    )
    remaining = _float(remaining_value) if remaining_value is not None else fallback_stake
    matched = _float(offer.get("matched-amount") or offer.get("matched_amount"))
    matched_bets = offer.get("matched-bets") or offer.get("matched_bets") or []
    matched_bet_stake = sum(_float(bet.get("stake")) for bet in matched_bets if isinstance(bet, dict))
    if matched <= 0 and matched_bet_stake > 0:
        matched = matched_bet_stake
    stake = _float(offer.get("stake")) or fallback_stake
    if matched <= 0 and stake > 0 and remaining_value is not None:
        matched = max(0.0, stake - remaining)
    avg_odds = _float(offer.get("average-odds") or offer.get("average_odds"))
    if avg_odds <= 0 and matched_bet_stake > 0:
        weighted_odds = sum(
            _float(bet.get("stake")) * _float(bet.get("odds") or bet.get("decimal-odds"))
            for bet in matched_bets
            if isinstance(bet, dict)
        )
        avg_odds = weighted_odds / matched_bet_stake
    if avg_odds <= 0:
        avg_odds = _float(offer.get("decimal-odds") or offer.get("odds"))
    return matched, remaining, avg_odds


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _matchbook_offer_settlements(
    payload: dict[str, Any], *, offer_id: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for market in payload.get("markets", []):
        market_bets = [
            bet
            for selection in market.get("selections", [])
            for bet in selection.get("bets", [])
            if isinstance(bet, dict)
        ]
        matched_bets = [
            bet for bet in market_bets if str(bet.get("offer-id") or "") == offer_id
        ]
        if not matched_bets:
            continue
        gross_profit = sum(_float(bet.get("profit-and-loss")) for bet in matched_bets)
        commission = sum(_float(bet.get("commission")) for bet in matched_bets)
        market_commission = _float(market.get("commission"))
        if market_commission > 0:
            profit_by_offer: dict[str, float] = {}
            for bet in market_bets:
                bet_offer_id = str(bet.get("offer-id") or "")
                profit_by_offer[bet_offer_id] = profit_by_offer.get(
                    bet_offer_id, 0.0
                ) + _float(bet.get("profit-and-loss"))
            positive_profit = sum(max(0.0, value) for value in profit_by_offer.values())
            if positive_profit > 0:
                commission = market_commission * max(0.0, gross_profit) / positive_profit
        results = sorted(
            {str(bet.get("result") or "").upper() for bet in matched_bets}
        )
        settled_at = max(
            (str(bet.get("settled-time") or "") for bet in matched_bets),
            default=str(market.get("settled-time") or ""),
        )
        result.append(
            {
                "profit-and-loss": gross_profit,
                "commission": commission,
                "net-profit-and-loss": gross_profit - commission,
                "result": ",".join(value for value in results if value),
                "settled-time": settled_at,
            }
        )
    return result


def _settlement_payload(
    *,
    source: str,
    gross_profit: float,
    commission: float,
    net_profit: float,
    venue_result: str,
    settled_at: str,
) -> dict[str, Any]:
    return {
        "settlement_source": source,
        "gross_profit": gross_profit,
        "commission": commission,
        "net_profit": net_profit,
        "venue_result": venue_result,
        "venue_settled_at": settled_at,
    }


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _matchbook_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _status_from_sizes(value: Any, matched: float, remaining: float) -> str:
    status = _normal_status(value)
    remaining = 0.0 if remaining <= MIN_REMAINDER_TO_CANCEL else remaining
    if status in {"cancelled", "canceled", "failed", "rejected", "unknown", "status_check_failed"}:
        return status
    if matched > 0 and remaining > 0:
        return "partially_matched"
    if matched > 0 and remaining <= 0:
        return "matched"
    if status == "matched":
        return "submitted" if remaining > 0 else "cancelled"
    return "submitted"


def _has_unmatched_remainder(result: LiveOrderResult) -> bool:
    return (result.remaining_size or 0) > MIN_REMAINDER_TO_CANCEL and result.status not in {
        "cancelled",
        "failed",
        "rejected",
    }


def _cancelled_remainder_result(result: LiveOrderResult) -> LiveOrderResult:
    matched_size = result.matched_size or 0
    return LiveOrderResult(
        order_id=result.order_id,
        status="partially_matched_cancelled" if matched_size > 0 else "cancelled",
        venue_order_id=result.venue_order_id,
        matched_size=result.matched_size,
        avg_matched_odds=result.avg_matched_odds,
        remaining_size=0,
        error=result.error,
    )


def _result_with_error(result: LiveOrderResult, error: str) -> LiveOrderResult:
    return LiveOrderResult(
        order_id=result.order_id,
        status=result.status,
        venue_order_id=result.venue_order_id,
        matched_size=result.matched_size,
        avg_matched_odds=result.avg_matched_odds,
        remaining_size=result.remaining_size,
        error=error,
    )


def _smarkets_side(bet_side: str) -> str:
    return "buy" if bet_side.casefold() == "back" else "sell"


def _smarkets_price(decimal_odds: float) -> int:
    return round(10000 / decimal_odds)


def _smarkets_quantity(stake: float) -> int:
    return round(stake * 10000)


def _smarkets_quantity_to_gbp(value: Any) -> float:
    return _float(value) / 10000


def _smarkets_order_quantity(intent: LiveOrderIntent, *, price: int) -> int:
    if price <= 0 or price >= 10000:
        return _smarkets_quantity(intent.stake)
    if intent.signal.bet_side.casefold() == "lay":
        payout_quantity = intent.liability / (1 - (price / 10000))
    else:
        payout_quantity = intent.stake / (price / 10000)
    return _smarkets_quantity(payout_quantity)


def _smarkets_order_fill(
    order: dict[str, Any],
    *,
    bet_side: str,
    fallback_stake: float = 0.0,
) -> tuple[float, float]:
    matched_value = _first_present(
        order,
        (
            "quantity_filled_user_currency",
            "quantity_filled",
            "matched_quantity",
        ),
    )
    remaining_value = _first_present(
        order,
        (
            "quantity_unfilled_user_currency",
            "quantity_unfilled",
            "remaining_quantity",
        ),
    )
    price = _float(
        _first_present(
            order,
            (
                "average_price_matched",
                "average_price_matched_precise",
                "price",
            ),
        )
    )
    matched_payout_quantity = _smarkets_quantity_to_gbp(matched_value)
    matched = _smarkets_stake_from_payout_quantity(
        matched_payout_quantity,
        price=price,
        bet_side=bet_side,
        fallback_stake=fallback_stake,
    )
    if remaining_value is not None:
        remaining_payout_quantity = _smarkets_quantity_to_gbp(remaining_value)
        remaining = _smarkets_stake_from_payout_quantity(
            remaining_payout_quantity,
            price=_float(order.get("price")) or price,
            bet_side=bet_side,
            fallback_stake=0,
        )
    elif matched > 0:
        remaining = max(0.0, fallback_stake - matched)
    else:
        remaining = fallback_stake
    return matched, remaining


def _smarkets_stake_from_payout_quantity(
    payout_quantity: float,
    *,
    price: float,
    bet_side: str,
    fallback_stake: float,
) -> float:
    if payout_quantity <= 0:
        return 0
    if price <= 0 or price >= 10000:
        return fallback_stake if fallback_stake > 0 else payout_quantity
    if bet_side.casefold() == "lay":
        liability = payout_quantity * (1 - (price / 10000))
        odds = 10000 / price
        return liability / max(odds - 1, 1e-9)
    return payout_quantity * (price / 10000)


def _smarkets_avg_odds(order: dict[str, Any]) -> float | None:
    for key in (
        "average_price_matched",
        "average_price_matched_precise",
        "average_price",
        "avg_price",
        "matched_price",
    ):
        value = _float(order.get(key))
        if value > 0:
            return 10000 / value
    return None


def _betfair_status(report: dict[str, Any]) -> str:
    if str(report.get("status") or "").casefold() == "failure":
        return "rejected"
    order_status = str(report.get("orderStatus") or "").casefold()
    if order_status == "execution_complete":
        return "matched"
    if order_status == "executable":
        return "submitted"
    return _normal_status(order_status or report.get("status") or "submitted")


def _betfair_error(report: dict[str, Any]) -> str | None:
    if str(report.get("status") or "").casefold() != "failure":
        return None
    return str(report.get("errorCode") or "betfair_order_failed")


def _betfair_customer_ref(order_id: str) -> str:
    return f"bf{hashlib.sha1(order_id.encode('utf-8')).hexdigest()[:24]}"
