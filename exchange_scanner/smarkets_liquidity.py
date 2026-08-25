from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

import httpx

SMARKETS_API_BASE = "https://api.smarkets.com/v3"
ODDS_DISPLAY_TOLERANCE = 0.001


@dataclass(frozen=True)
class SmarketsLiquidityMatch:
    smarkets_event_id: str | None
    smarkets_market_id: str | None
    smarkets_contract_id: str | None
    match_score: float
    best_back_odds: float | None
    best_back_available: float
    available_at_or_above_target: float
    best_lay_odds: float | None
    best_lay_available: float
    back_lay_spread_pct: float | None
    liquidity_status: str


class SmarketsLiquidityClient:
    def __init__(
        self,
        *,
        session_token: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.session_token = session_token or os.environ.get("SMARKETS_SESSION_TOKEN")
        headers = {"Accept": "application/json"}
        if self.session_token:
            headers["Authorization"] = f"Bearer {self.session_token}"
        self.http = httpx.Client(base_url=SMARKETS_API_BASE, headers=headers, timeout=timeout)
        self._markets_by_event: dict[str, list[dict[str, Any]]] = {}
        self._contracts_by_market: dict[str, list[dict[str, Any]]] = {}
        self._quotes_by_market: dict[str, dict[str, Any]] = {}

    def keep_alive(self) -> dict[str, Any]:
        response = self.http.get("/accounts/")
        response.raise_for_status()
        return response.json()

    def fetch_football_events(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        start = start or datetime.now(timezone.utc)
        end = end or start + timedelta(days=4)
        response = self.http.get(
            "/events/",
            params={
                "type": "football_match",
                "state": "upcoming",
                "limit": limit,
            },
        )
        response.raise_for_status()
        events = response.json().get("events", [])
        return [
            event
            for event in events
            if _event_in_window(event, start=start, end=end)
        ]

    def fetch_markets(self, event_id: str) -> list[dict[str, Any]]:
        if event_id not in self._markets_by_event:
            response = self.http.get(f"/events/{event_id}/markets/")
            response.raise_for_status()
            self._markets_by_event[event_id] = response.json().get("markets", [])
        return self._markets_by_event[event_id]

    def fetch_contracts(self, market_id: str) -> list[dict[str, Any]]:
        if market_id not in self._contracts_by_market:
            response = self.http.get(f"/markets/{market_id}/contracts/")
            response.raise_for_status()
            self._contracts_by_market[market_id] = response.json().get("contracts", [])
        return self._contracts_by_market[market_id]

    def fetch_quotes(self, market_id: str) -> dict[str, Any]:
        if market_id not in self._quotes_by_market:
            response = self.http.get(f"/markets/{market_id}/quotes/")
            response.raise_for_status()
            self._quotes_by_market[market_id] = response.json()
        return self._quotes_by_market[market_id]


def unavailable_liquidity(status: str) -> SmarketsLiquidityMatch:
    return SmarketsLiquidityMatch(
        smarkets_event_id=None,
        smarkets_market_id=None,
        smarkets_contract_id=None,
        match_score=0,
        best_back_odds=None,
        best_back_available=0,
        available_at_or_above_target=0,
        best_lay_odds=None,
        best_lay_available=0,
        back_lay_spread_pct=None,
        liquidity_status=status,
    )


def match_liquidity(
    client: SmarketsLiquidityClient,
    events: list[dict[str, Any]],
    *,
    event_name: str,
    commence_time: datetime | None,
    market_key: str = "h2h",
    outcome_name: str,
    target_odds: float,
    bet_side: str = "back",
) -> SmarketsLiquidityMatch:
    if market_key != "h2h":
        return unavailable_liquidity("unsupported_market")

    best_event = _best_event(events, event_name=event_name, commence_time=commence_time)
    if best_event is None:
        return unavailable_liquidity("not_matched")
    event_score, event = best_event
    market = _full_time_result_market(client.fetch_markets(str(event["id"])))
    if market is None:
        return unavailable_liquidity("market_not_found")

    contracts = client.fetch_contracts(str(market["id"]))
    contract = _best_contract(contracts, outcome_name=outcome_name, event_name=event_name)
    if contract is None:
        return unavailable_liquidity("runner_not_found")
    contract_score, selected_contract = contract
    score = (event_score * 0.70) + (contract_score * 0.30)
    if score < 0.70:
        return unavailable_liquidity("not_matched")

    quotes = client.fetch_quotes(str(market["id"]))
    contract_quotes = quotes.get(str(selected_contract["id"]), {})
    bids = _price_levels(contract_quotes.get("bids", []))
    offers = _price_levels(contract_quotes.get("offers", []))
    best_bid = max(bids, key=lambda quote: quote["price"], default=None)
    best_offer = min(offers, key=lambda quote: quote["price"], default=None)
    best_back_odds = _decimal_odds(best_offer["price"]) if best_offer else None
    best_lay_odds = _decimal_odds(best_bid["price"]) if best_bid else None

    if bet_side.casefold() == "lay":
        available_at_target = sum(
            _quantity_to_gbp(level["quantity"])
            for level in bids
            if _decimal_odds(level["price"]) <= target_odds + ODDS_DISPLAY_TOLERANCE
        )
    else:
        available_at_target = sum(
            _quantity_to_gbp(level["quantity"])
            for level in offers
            if _decimal_odds(level["price"]) + ODDS_DISPLAY_TOLERANCE >= target_odds
        )

    status = "available" if available_at_target > 0 else "price_not_available"
    return SmarketsLiquidityMatch(
        smarkets_event_id=str(event["id"]),
        smarkets_market_id=str(market["id"]),
        smarkets_contract_id=str(selected_contract["id"]),
        match_score=score,
        best_back_odds=best_back_odds,
        best_back_available=_quantity_to_gbp(best_offer["quantity"]) if best_offer else 0,
        available_at_or_above_target=available_at_target,
        best_lay_odds=best_lay_odds,
        best_lay_available=_quantity_to_gbp(best_bid["quantity"]) if best_bid else 0,
        back_lay_spread_pct=_spread_pct(best_back_odds, best_lay_odds),
        liquidity_status=status,
    )


def _best_event(
    events: list[dict[str, Any]],
    *,
    event_name: str,
    commence_time: datetime | None,
) -> tuple[float, dict[str, Any]] | None:
    best: tuple[float, dict[str, Any]] | None = None
    for event in events:
        name_score = _name_score(event_name, str(event.get("name", "")))
        time_score = _time_score(commence_time, _parse_time(event.get("start_datetime")))
        score = (name_score * 0.80) + (time_score * 0.20)
        if best is None or score > best[0]:
            best = (score, event)
    if best is None or best[0] < 0.65:
        return None
    return best


def _full_time_result_market(markets: list[dict[str, Any]]) -> dict[str, Any] | None:
    for market in markets:
        market_type = market.get("market_type", {})
        if (
            market.get("state") == "open"
            and market.get("name", "").casefold() == "full-time result"
            and market_type.get("name") == "WINNER_3_WAY"
        ):
            return market
    return None


def _best_contract(
    contracts: list[dict[str, Any]],
    *,
    outcome_name: str,
    event_name: str,
) -> tuple[float, dict[str, Any]] | None:
    if outcome_name.casefold() == "draw":
        for contract in contracts:
            if contract.get("contract_type", {}).get("name") == "DRAW":
                return (1.0, contract)

    home, away = _event_teams(event_name)
    desired_side = ""
    if home and _name_score(outcome_name, home) >= _name_score(outcome_name, away):
        desired_side = "HOME"
    elif away:
        desired_side = "AWAY"

    best: tuple[float, dict[str, Any]] | None = None
    for contract in contracts:
        contract_type = contract.get("contract_type", {}).get("name")
        type_bonus = 0.20 if desired_side and contract_type == desired_side else 0.0
        score = min(1.0, _name_score(outcome_name, str(contract.get("name", ""))) + type_bonus)
        if best is None or score > best[0]:
            best = (score, contract)
    if best is None or best[0] < 0.55:
        return None
    return best


def _price_levels(levels: list[dict[str, Any]]) -> list[dict[str, float]]:
    return [
        {"price": float(level["price"]), "quantity": float(level["quantity"])}
        for level in levels
    ]


def _decimal_odds(price: float) -> float:
    return 10000 / price if price > 0 else 0.0


def _quantity_to_gbp(quantity: float) -> float:
    return quantity / 10000


def _event_in_window(event: dict[str, Any], *, start: datetime, end: datetime) -> bool:
    event_time = _parse_time(event.get("start_datetime"))
    return event_time is not None and start <= event_time <= end


def _time_score(left: datetime | None, right: datetime | None) -> float:
    if left is None or right is None:
        return 0.5
    diff = abs((left - right).total_seconds())
    if diff <= 30 * 60:
        return 1.0
    if diff >= 12 * 60 * 60:
        return 0.0
    return 1.0 - (diff / (12 * 60 * 60))


def _spread_pct(back_odds: float | None, lay_odds: float | None) -> float | None:
    if not back_odds or not lay_odds:
        return None
    midpoint = (back_odds + lay_odds) / 2
    return (lay_odds - back_odds) / midpoint


def _event_teams(event_name: str) -> tuple[str, str]:
    normalised = event_name.replace(" vs ", " v ")
    if " v " not in normalised:
        return "", ""
    home, away = normalised.split(" v ", 1)
    return home.strip(), away.strip()


def _name_score(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalise_name(left), _normalise_name(right)).ratio()


def _normalise_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.casefold()
    for old, new in {
        " vs ": " v ",
        " versus ": " v ",
        ".": " ",
        "-": " ",
        "_": " ",
        " fc ": " ",
        " afc ": " ",
        " sv ": " ",
        " sc ": " ",
        " cf ": " ",
    }.items():
        value = value.replace(old, new)
    return " ".join(value.split())


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
