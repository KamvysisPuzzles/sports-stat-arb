from __future__ import annotations

import csv
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx

MATCHBOOK_API_BASE = "https://api.matchbook.com/edge/rest"


@dataclass(frozen=True)
class LiquidityMatch:
    matchbook_event_id: int | None
    matchbook_market_id: int | None
    matchbook_runner_id: int | None
    match_score: float
    best_back_odds: float | None
    best_back_available: float
    available_at_or_above_target: float
    best_lay_odds: float | None
    best_lay_available: float
    back_lay_spread_pct: float | None
    liquidity_status: str


class MatchbookLiquidityClient:
    def __init__(self, *, timeout: float = 15.0) -> None:
        self.http = httpx.Client(base_url=MATCHBOOK_API_BASE, timeout=timeout)

    def fetch_events(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        currency: str = "GBP",
        minimum_liquidity: float = 2,
        per_page: int = 500,
    ) -> list[dict[str, Any]]:
        start = start or datetime.now(timezone.utc)
        end = end or start + timedelta(days=2)
        events: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = self.http.get(
                "/events",
                params={
                    "offset": offset,
                    "per-page": per_page,
                    "after": int(start.timestamp()),
                    "before": int(end.timestamp()),
                    "states": "open",
                    "include-prices": "true",
                    "price-depth": 10,
                    "price-mode": "expanded",
                    "currency": currency,
                    "minimum-liquidity": minimum_liquidity,
                    "exchange-type": "back-lay",
                },
            )
            response.raise_for_status()
            payload = response.json()
            page_events = payload.get("events", [])
            events.extend(page_events)
            offset += len(page_events)
            total = int(payload.get("total", len(events)))
            if not page_events or offset >= total:
                break
        return events


def enrich_opportunities_csv(
    *,
    opportunities_csv: Path,
    output_csv: Path,
    events: list[dict[str, Any]],
) -> None:
    with opportunities_csv.open() as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0].keys()) if rows else []

    extra_fields = [
        "matchbook_event_id",
        "matchbook_market_id",
        "matchbook_runner_id",
        "match_score",
        "best_back_odds",
        "best_back_available",
        "available_at_or_above_target",
        "best_lay_odds",
        "best_lay_available",
        "back_lay_spread_pct",
        "liquidity_status",
    ]
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames + extra_fields)
        writer.writeheader()
        for row in rows:
            match = match_liquidity(
                events,
                event_name=row["event_name"],
                outcome_name=row["outcome_name"],
                target_odds=float(row["target_odds"]),
            )
            output_row = dict(row)
            output_row.update(_liquidity_row(match))
            writer.writerow(output_row)


def match_liquidity(
    events: list[dict[str, Any]],
    *,
    event_name: str,
    outcome_name: str,
    target_odds: float,
) -> LiquidityMatch:
    best: tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
    for event in events:
        for market in event.get("markets", []):
            if not _is_match_odds_market(market):
                continue
            for runner in market.get("runners", []):
                event_score = _name_score(event_name, event.get("name", ""))
                runner_score = _name_score(outcome_name, runner.get("name", ""))
                score = (event_score * 0.75) + (runner_score * 0.25)
                if runner_score < 0.72:
                    continue
                if best is None or score > best[0]:
                    best = (score, event, market, runner)

    if best is None or best[0] < 0.72:
        return LiquidityMatch(
            matchbook_event_id=None,
            matchbook_market_id=None,
            matchbook_runner_id=None,
            match_score=0,
            best_back_odds=None,
            best_back_available=0,
            available_at_or_above_target=0,
            best_lay_odds=None,
            best_lay_available=0,
            back_lay_spread_pct=None,
            liquidity_status="not_matched",
        )

    score, event, market, runner = best
    back_prices = _prices(runner, side="back")
    lay_prices = _prices(runner, side="lay")
    best_back = max(back_prices, key=lambda price: price["decimal-odds"], default=None)
    best_lay = min(lay_prices, key=lambda price: price["decimal-odds"], default=None)
    available_at_target = sum(
        price["available-amount"]
        for price in back_prices
        if price["decimal-odds"] >= target_odds
    )
    spread = _spread_pct(
        best_back["decimal-odds"] if best_back else None,
        best_lay["decimal-odds"] if best_lay else None,
    )
    status = "available" if available_at_target > 0 else "price_not_available"
    return LiquidityMatch(
        matchbook_event_id=event.get("id"),
        matchbook_market_id=market.get("id"),
        matchbook_runner_id=runner.get("id"),
        match_score=score,
        best_back_odds=best_back["decimal-odds"] if best_back else None,
        best_back_available=best_back["available-amount"] if best_back else 0,
        available_at_or_above_target=available_at_target,
        best_lay_odds=best_lay["decimal-odds"] if best_lay else None,
        best_lay_available=best_lay["available-amount"] if best_lay else 0,
        back_lay_spread_pct=spread,
        liquidity_status=status,
    )


def _is_match_odds_market(market: dict[str, Any]) -> bool:
    return (
        market.get("product") == "EXCHANGE"
        and market.get("status") == "open"
        and (
            market.get("name", "").casefold() == "match odds"
            or market.get("market-type") in {"one_x_two", "money_line"}
        )
    )


def _prices(runner: dict[str, Any], *, side: str) -> list[dict[str, float]]:
    prices = []
    for price in runner.get("prices", []):
        if price.get("side") != side:
            continue
        prices.append(
            {
                "decimal-odds": float(price["decimal-odds"]),
                "available-amount": float(price["available-amount"]),
            }
        )
    return prices


def _spread_pct(back_odds: float | None, lay_odds: float | None) -> float | None:
    if not back_odds or not lay_odds:
        return None
    midpoint = (back_odds + lay_odds) / 2
    return (lay_odds - back_odds) / midpoint


def _liquidity_row(match: LiquidityMatch) -> dict[str, str]:
    return {
        "matchbook_event_id": str(match.matchbook_event_id or ""),
        "matchbook_market_id": str(match.matchbook_market_id or ""),
        "matchbook_runner_id": str(match.matchbook_runner_id or ""),
        "match_score": f"{match.match_score:.4f}",
        "best_back_odds": _fmt_optional(match.best_back_odds),
        "best_back_available": f"{match.best_back_available:.2f}",
        "available_at_or_above_target": f"{match.available_at_or_above_target:.2f}",
        "best_lay_odds": _fmt_optional(match.best_lay_odds),
        "best_lay_available": f"{match.best_lay_available:.2f}",
        "back_lay_spread_pct": (
            f"{match.back_lay_spread_pct:.4f}" if match.back_lay_spread_pct is not None else ""
        ),
        "liquidity_status": match.liquidity_status,
    }


def _fmt_optional(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


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
        " sk ": " ",
    }.items():
        value = value.replace(old, new)
    return " ".join(value.split())
