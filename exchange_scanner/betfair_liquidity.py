from __future__ import annotations

import csv
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx

BETFAIR_BETTING_API_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"
LIQUIDITY_FIELDS = [
    "matchbook_market_id",
    "matchbook_runner_id",
    "liquidity_status",
    "available_at_or_above_target",
    "best_back_odds",
    "best_back_available",
    "best_lay_odds",
    "best_lay_available",
    "back_lay_spread_pct",
]


@dataclass(frozen=True)
class BetfairLiquidityMatch:
    betfair_market_id: str | None
    betfair_selection_id: int | None
    match_score: float
    best_back_odds: float | None
    best_back_available: float
    available_at_or_above_target: float
    best_lay_odds: float | None
    best_lay_available: float
    back_lay_spread_pct: float | None
    liquidity_status: str


class BetfairLiquidityClient:
    def __init__(
        self,
        *,
        app_key: str,
        session_token: str,
        timeout: float = 15.0,
    ) -> None:
        self.http = httpx.Client(
            timeout=timeout,
            headers={
                "X-Application": app_key,
                "X-Authentication": session_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def fetch_market_catalogue(
        self,
        *,
        event_name: str,
        commence_time: datetime,
        market_key: str = "h2h",
        max_results: int = 10,
        use_text_query: bool = True,
    ) -> list[dict[str, Any]]:
        market_type = _betfair_market_type(market_key)
        if market_type is None:
            return []
        market_filter: dict[str, Any] = {
            "marketTypeCodes": [market_type],
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

    def fetch_market_books(self, market_ids: list[str]) -> list[dict[str, Any]]:
        if not market_ids:
            return []
        payload = self._rpc(
            "SportsAPING/v1.0/listMarketBook",
            {
                "marketIds": market_ids,
                "priceProjection": {
                    "priceData": ["EX_BEST_OFFERS"],
                    "exBestOffersOverrides": {
                        "bestPricesDepth": 10,
                    },
                },
            },
        )
        return payload if isinstance(payload, list) else []

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        response = self.http.post(
            BETFAIR_BETTING_API_URL,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"Betfair API error for {method}: {payload['error']}")
        return payload.get("result")


def enrich_opportunities_csv(
    *,
    opportunities_csv: Path,
    output_csv: Path,
    client: BetfairLiquidityClient | None,
) -> None:
    with opportunities_csv.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0].keys()) if rows else []
    for field in LIQUIDITY_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            output_row = dict(row)
            if row.get("target_bookmaker", "").casefold() != "betfair":
                writer.writerow(output_row)
                continue
            if client is None:
                output_row.update(_liquidity_row(unavailable_liquidity("betfair_not_configured")))
                writer.writerow(output_row)
                continue
            try:
                match = match_liquidity(
                    client,
                    event_name=row["event_name"],
                    commence_time=datetime.fromisoformat(row["commence_time"]),
                    market_key=row.get("market", row.get("market_key", "h2h")),
                    outcome_name=row["outcome_name"],
                    target_odds=float(row["target_odds"]),
                )
            except Exception as exc:
                match = unavailable_liquidity(f"betfair_error:{type(exc).__name__}")
            output_row.update(_liquidity_row(match))
            writer.writerow(output_row)


def match_liquidity(
    client: BetfairLiquidityClient,
    *,
    event_name: str,
    commence_time: datetime,
    market_key: str,
    outcome_name: str,
    target_odds: float,
) -> BetfairLiquidityMatch:
    catalogues = client.fetch_market_catalogue(
        event_name=event_name,
        commence_time=commence_time,
        market_key=market_key,
    )
    best_catalogue = _best_catalogue_match(
        catalogues,
        event_name=event_name,
        outcome_name=outcome_name,
    )
    if best_catalogue is None:
        catalogues = client.fetch_market_catalogue(
            event_name=event_name,
            commence_time=commence_time,
            market_key=market_key,
            max_results=200,
            use_text_query=False,
        )
        best_catalogue = _best_catalogue_match(
            catalogues,
            event_name=event_name,
            outcome_name=outcome_name,
        )
    if best_catalogue is None:
        return unavailable_liquidity("betfair_not_matched")

    match_score, catalogue, runner = best_catalogue
    books = client.fetch_market_books([catalogue["marketId"]])
    runner_book = _runner_book(books, selection_id=int(runner["selectionId"]))
    if runner_book is None:
        return unavailable_liquidity("betfair_runner_not_found")

    exchange = runner_book.get("ex", {})
    back_prices = _prices(exchange.get("availableToBack", []))
    lay_prices = _prices(exchange.get("availableToLay", []))
    best_back = max(back_prices, key=lambda price: price["price"], default=None)
    best_lay = min(lay_prices, key=lambda price: price["price"], default=None)
    available_at_target = sum(
        price["size"] for price in back_prices if price["price"] >= target_odds
    )
    spread = _spread_pct(
        best_back["price"] if best_back else None,
        best_lay["price"] if best_lay else None,
    )
    status = "available" if available_at_target > 0 else "price_not_available"
    return BetfairLiquidityMatch(
        betfair_market_id=catalogue["marketId"],
        betfair_selection_id=int(runner["selectionId"]),
        match_score=match_score,
        best_back_odds=best_back["price"] if best_back else None,
        best_back_available=best_back["size"] if best_back else 0,
        available_at_or_above_target=available_at_target,
        best_lay_odds=best_lay["price"] if best_lay else None,
        best_lay_available=best_lay["size"] if best_lay else 0,
        back_lay_spread_pct=spread,
        liquidity_status=status,
    )


def unavailable_liquidity(status: str) -> BetfairLiquidityMatch:
    return BetfairLiquidityMatch(
        betfair_market_id=None,
        betfair_selection_id=None,
        match_score=0,
        best_back_odds=None,
        best_back_available=0,
        available_at_or_above_target=0,
        best_lay_odds=None,
        best_lay_available=0,
        back_lay_spread_pct=None,
        liquidity_status=status,
    )


def _liquidity_row(match: BetfairLiquidityMatch) -> dict[str, str]:
    return {
        "matchbook_market_id": match.betfair_market_id or "",
        "matchbook_runner_id": str(match.betfair_selection_id or ""),
        "liquidity_status": match.liquidity_status,
        "available_at_or_above_target": _format_number(match.available_at_or_above_target),
        "best_back_odds": _format_optional(match.best_back_odds, decimals=4),
        "best_back_available": _format_number(match.best_back_available),
        "best_lay_odds": _format_optional(match.best_lay_odds, decimals=4),
        "best_lay_available": _format_number(match.best_lay_available),
        "back_lay_spread_pct": _format_optional(match.back_lay_spread_pct, decimals=4),
    }


def _best_catalogue_match(
    catalogues: list[dict[str, Any]],
    *,
    event_name: str,
    outcome_name: str,
) -> tuple[float, dict[str, Any], dict[str, Any]] | None:
    best: tuple[float, dict[str, Any], dict[str, Any]] | None = None
    for catalogue in catalogues:
        event_score = _name_score(event_name, catalogue.get("event", {}).get("name", ""))
        for runner in catalogue.get("runners", []):
            runner_score = _runner_score(outcome_name, runner.get("runnerName", ""))
            if runner_score < 0.70:
                continue
            score = (event_score * 0.70) + (runner_score * 0.30)
            if best is None or score > best[0]:
                best = (score, catalogue, runner)
    if best is None or best[0] < 0.70:
        return None
    return best


def _runner_book(
    books: list[dict[str, Any]],
    *,
    selection_id: int,
) -> dict[str, Any] | None:
    for book in books:
        for runner in book.get("runners", []):
            if int(runner.get("selectionId", 0)) == selection_id:
                return runner
    return None


def _prices(values: list[dict[str, Any]]) -> list[dict[str, float]]:
    prices = []
    for value in values:
        prices.append(
            {
                "price": float(value["price"]),
                "size": float(value["size"]),
            }
        )
    return prices


def _betfair_market_type(market_key: str) -> str | None:
    if market_key == "h2h":
        return "MATCH_ODDS"
    return None


def _runner_score(outcome_name: str, runner_name: str) -> float:
    if outcome_name.casefold() == "draw" and runner_name.casefold() in {"draw", "the draw"}:
        return 1.0
    return _name_score(outcome_name, runner_name)


def _name_score(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalise_name(left), _normalise_name(right)).ratio()


def _normalise_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return (
        value.casefold()
        .replace("&", "and")
        .replace(" fc", "")
        .replace(" cf", "")
        .replace(" bk", "")
        .replace(".", "")
        .replace("-", " ")
        .strip()
    )


def _spread_pct(back_odds: float | None, lay_odds: float | None) -> float | None:
    if not back_odds or not lay_odds or back_odds <= 1 or lay_odds <= 1:
        return None
    midpoint = (back_odds + lay_odds) / 2
    return (lay_odds - back_odds) / midpoint


def _format_number(value: float) -> str:
    return f"{value:.2f}"


def _format_optional(value: float | None, *, decimals: int) -> str:
    return f"{value:.{decimals}f}" if value is not None else ""
