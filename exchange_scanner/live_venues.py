from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import textwrap
from dataclasses import dataclass, replace
from datetime import timedelta
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

    def place_limit_order(self, intent: LiveOrderIntent) -> LiveOrderResult:
        market_id = _required(intent.venue_metadata.get("market_id"), "smarkets_market_id")
        contract_id = _required(intent.venue_metadata.get("runner_id"), "smarkets_contract_id")
        payload = {
            "market_id": str(market_id),
            "contract_id": str(contract_id),
            "side": _smarkets_side(intent.signal.bet_side),
            "price": _smarkets_price(intent.limit_odds),
            "quantity": _smarkets_quantity(intent.stake),
            "reference": intent.order_id,
        }
        response = self.http.post("/orders/", json=payload)
        response.raise_for_status()
        data = response.json()
        order = _first(data.get("orders")) or data.get("order") or data
        venue_order_id = str(order.get("id") or order.get("order_id") or "")
        matched = _smarkets_quantity_to_gbp(order.get("matched_quantity"))
        remaining = (
            _smarkets_quantity_to_gbp(order["remaining_quantity"])
            if "remaining_quantity" in order
            else intent.stake
        )
        result = LiveOrderResult(
            order_id=intent.order_id,
            status=_status_from_sizes(order.get("state"), matched, remaining),
            venue_order_id=venue_order_id or None,
            matched_size=matched,
            avg_matched_odds=_smarkets_avg_odds(order),
            remaining_size=remaining,
        )
        return self._cancel_unmatched_remainder(result)

    def fetch_order_status(self, order: dict[str, Any]) -> LiveOrderStatus:
        venue_order_id = str(order.get("venue_order_id") or "")
        if not venue_order_id:
            return LiveOrderStatus(order_id=str(order["order_id"]), status="unknown", error="missing_venue_order_id")
        response = self.http.get(f"/orders/{venue_order_id}/")
        response.raise_for_status()
        data = response.json()
        payload = data.get("order") or data
        matched = _smarkets_quantity_to_gbp(payload.get("matched_quantity"))
        remaining = _smarkets_quantity_to_gbp(payload.get("remaining_quantity"))
        return LiveOrderStatus(
            order_id=str(order["order_id"]),
            status=_status_from_sizes(payload.get("state"), matched, remaining),
            venue_order_id=venue_order_id,
            matched_size=matched,
            avg_matched_odds=_smarkets_avg_odds(payload),
            remaining_size=remaining,
        )

    def _cancel_unmatched_remainder(self, result: LiveOrderResult) -> LiveOrderResult:
        if not result.venue_order_id or not _has_unmatched_remainder(result):
            return result
        try:
            response = self.http.delete(f"/orders/{result.venue_order_id}/")
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - preserve order result with cancel failure.
            return _result_with_error(
                result,
                f"cancel_after_place_failed:{type(exc).__name__}: {exc}",
            )
        return _cancelled_remainder_result(result)


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
        response = self.http.post(
            BETFAIR_BETTING_API_URL,
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


def _smarkets_avg_odds(order: dict[str, Any]) -> float | None:
    for key in ("average_price", "avg_price", "matched_price"):
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
