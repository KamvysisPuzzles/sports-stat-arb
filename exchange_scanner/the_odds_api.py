from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import quote_plus

import httpx

THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4"
MAX_EXCHANGE_BACK_LAY_SPREAD = 0.25
MATCHBOOK_COMMISSION_RATE = 0.02

BOOKMAKER_URLS = {
    "bet365": "https://www.bet365.com/",
    "betfred": "https://www.betfred.com/sports",
    "betvictor": "https://www.betvictor.com/",
    "betway": "https://betway.com/sports",
    "boylesports": "https://www.boylesports.com/",
    "coral": "https://sports.coral.co.uk/",
    "grosvenor": "https://www.grosvenorcasinos.com/sport",
    "ladbrokes": "https://www.ladbrokes.com/en/sports",
    "livescorebet": "https://www.livescorebet.com/uk/sports",
    "paddypower": "https://www.paddypower.com/bet",
    "skybet": "https://m.skybet.com/",
    "sport888": "https://www.888sport.com/",
    "unibet_uk": "https://www.unibet.co.uk/betting/sports/home",
    "virginbet": "https://www.virginbet.com/sports",
    "williamhill": "https://sports.williamhill.com/betting/en-gb",
}


class TheOddsApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutcomePrice:
    bookmaker_key: str
    bookmaker_title: str
    sport_key: str
    event_id: str
    event_name: str
    commence_time: datetime
    market_key: str
    market_name: str
    outcome_name: str
    point: float | None
    odds: float
    last_update: datetime
    exchange_lay_odds: float | None = None
    exchange_spread_pct: float | None = None

    @property
    def comparable_outcome_name(self) -> str:
        if self.point is None:
            return self.outcome_name
        return f"{self.outcome_name} {self.point:g}"


@dataclass(frozen=True)
class ValueSignal:
    sport_key: str
    event_id: str
    event_name: str
    commence_time: datetime
    market_key: str
    outcome_name: str
    target_bookmaker: str
    target_odds: float
    reference_fair_odds: float
    reference_probability: float
    edge: float
    reference_bookmakers: tuple[str, ...]
    bet_side: str = "back"
    target_effective_odds: float | None = None
    betfair_fair_odds: float | None = None
    betfair_fair_edge: float | None = None
    betfair_back_lay_spread_pct: float | None = None
    reference_fair_odds_by_bookmaker: tuple[tuple[str, float], ...] = ()
    reference_spread_pct_by_bookmaker: tuple[tuple[str, float], ...] = ()
    reference_last_update_by_bookmaker: tuple[tuple[str, str], ...] = ()
    reference_disagreement_pct: float | None = None
    reference_max_spread_pct: float | None = None
    reference_avg_spread_pct: float | None = None

    @property
    def effective_odds(self) -> float:
        return self.target_effective_odds or self.target_odds

    def as_dict(self) -> dict[str, str | float]:
        return {
            "sport_key": self.sport_key,
            "event_id": self.event_id,
            "event_name": self.event_name,
            "commence_time": self.commence_time.isoformat(),
            "market": self.market_key,
            "outcome_name": self.outcome_name,
            "target_bookmaker": self.target_bookmaker,
            "target_odds": self.target_odds,
            "target_effective_odds": self.effective_odds,
            "reference_fair_odds": self.reference_fair_odds,
            "reference_probability": self.reference_probability,
            "edge": self.edge,
            "reference_bookmakers": ", ".join(self.reference_bookmakers),
            "bet_side": self.bet_side,
            "betfair_fair_odds": self.betfair_fair_odds or "",
            "betfair_fair_edge": self.betfair_fair_edge or "",
            "betfair_back_lay_spread_pct": self.betfair_back_lay_spread_pct or "",
            "reference_fair_odds_by_bookmaker": _json_diagnostic(
                self.reference_fair_odds_by_bookmaker
            ),
            "reference_spread_pct_by_bookmaker": _json_diagnostic(
                self.reference_spread_pct_by_bookmaker
            ),
            "reference_last_update_by_bookmaker": _json_diagnostic(
                self.reference_last_update_by_bookmaker
            ),
            "reference_disagreement_pct": (
                self.reference_disagreement_pct
                if self.reference_disagreement_pct is not None
                else ""
            ),
            "reference_max_spread_pct": (
                self.reference_max_spread_pct
                if self.reference_max_spread_pct is not None
                else ""
            ),
            "reference_avg_spread_pct": (
                self.reference_avg_spread_pct
                if self.reference_avg_spread_pct is not None
                else ""
            ),
            "target_bookmaker_url": bookmaker_url(self.target_bookmaker),
            "event_search_url": bookmaker_event_search_url(self.target_bookmaker, self.event_name),
            "copy_search": f"{self.event_name} {self.outcome_name}",
            "copy_bet_instruction": (
                f"{self.target_bookmaker}: {self.bet_side.title()} {self.event_name} - "
                f"{self.market_key} - {self.outcome_name} @ {self.target_odds:g}"
            ),
            "min_acceptable_odds": self.target_odds,
        }


class TheOddsApiClient:
    def __init__(
        self,
        *,
        api_key: str,
        timeout: float = 10.0,
        cache_dir: Path | None = None,
        cache_ttl_seconds: int = 0,
    ) -> None:
        self.api_key = api_key
        self.http = httpx.Client(base_url=THE_ODDS_API_BASE, timeout=timeout)
        self.cache_dir = cache_dir
        self.cache_ttl_seconds = cache_ttl_seconds

    def fetch_sports(self, *, all_sports: bool = False) -> list[dict[str, Any]]:
        response = self.http.get(
            "/sports",
            params={
                "apiKey": self.api_key,
                "all": str(all_sports).lower(),
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            remaining = response.headers.get("x-requests-remaining")
            used = response.headers.get("x-requests-used")
            raise TheOddsApiError(
                f"The Odds API sports request failed with HTTP {response.status_code}; "
                f"requests_used={used or 'unknown'}, requests_remaining={remaining or 'unknown'}"
            ) from None
        return response.json()

    def fetch_odds(
        self,
        *,
        sport: str,
        regions: str,
        markets: str,
        odds_format: str = "decimal",
    ) -> list[dict[str, Any]]:
        cache_path = self._cache_path(
            sport=sport,
            regions=regions,
            markets=markets,
            odds_format=odds_format,
        )
        if cache_path:
            cached = self._read_cache(cache_path)
            if cached is not None:
                return cached

        response = self.http.get(
            f"/sports/{sport}/odds",
            params={
                "apiKey": self.api_key,
                "regions": regions,
                "markets": markets,
                "oddsFormat": odds_format,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            remaining = response.headers.get("x-requests-remaining")
            used = response.headers.get("x-requests-used")
            raise TheOddsApiError(
                f"The Odds API request failed with HTTP {response.status_code} "
                f"for sport={sport!r}, markets={markets!r}, regions={regions!r}; "
                f"requests_used={used or 'unknown'}, requests_remaining={remaining or 'unknown'}"
            ) from None
        payload = response.json()
        if cache_path:
            self._write_cache(cache_path, payload)
        return payload

    def fetch_historical_odds(
        self,
        *,
        sport: str,
        regions: str = "",
        markets: str,
        date: datetime,
        bookmakers: str = "",
        odds_format: str = "decimal",
    ) -> dict[str, Any]:
        params = {
            "apiKey": self.api_key,
            "markets": markets,
            "oddsFormat": odds_format,
            "date": date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if bookmakers:
            params["bookmakers"] = bookmakers
        else:
            params["regions"] = regions
        response = self.http.get(
            f"/historical/sports/{sport}/odds",
            params=params,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            remaining = response.headers.get("x-requests-remaining")
            used = response.headers.get("x-requests-used")
            raise TheOddsApiError(
                f"The Odds API historical request failed with HTTP {response.status_code} "
                f"for sport={sport!r}, markets={markets!r}, regions={regions!r}, date={date!s}; "
                f"requests_used={used or 'unknown'}, requests_remaining={remaining or 'unknown'}"
            ) from None
        return response.json()

    def fetch_scores(
        self,
        *,
        sport: str,
        days_from: int = 3,
    ) -> list[dict[str, Any]]:
        response = self.http.get(
            f"/sports/{sport}/scores",
            params={
                "apiKey": self.api_key,
                "daysFrom": days_from,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            remaining = response.headers.get("x-requests-remaining")
            used = response.headers.get("x-requests-used")
            raise TheOddsApiError(
                f"The Odds API scores request failed with HTTP {response.status_code} "
                f"for sport={sport!r}; "
                f"requests_used={used or 'unknown'}, requests_remaining={remaining or 'unknown'}"
            ) from None
        return response.json()

    def _cache_path(
        self,
        *,
        sport: str,
        regions: str,
        markets: str,
        odds_format: str,
    ) -> Path | None:
        if not self.cache_dir or self.cache_ttl_seconds <= 0:
            return None
        key = json.dumps(
            {
                "sport": sport,
                "regions": regions,
                "markets": markets,
                "odds_format": odds_format,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, path: Path) -> list[dict[str, Any]] | None:
        if not path.exists():
            return None
        try:
            item = json.loads(path.read_text())
            fetched_at = _parse_time(item["fetched_at"])
            age = datetime.now(timezone.utc) - fetched_at
            if age.total_seconds() > self.cache_ttl_seconds:
                return None
            payload = item["payload"]
            return payload if isinstance(payload, list) else None
        except (OSError, KeyError, json.JSONDecodeError, ValueError):
            return None

    def _write_cache(self, path: Path, payload: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "payload": payload,
                }
            )
        )


def normalise_odds_api_events(events: list[dict[str, Any]]) -> list[OutcomePrice]:
    prices: list[OutcomePrice] = []
    for event in events:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        event_name = f"{home} v {away}" if home and away else event.get("id", "")
        commence_time = _parse_time(event["commence_time"])

        for bookmaker in event.get("bookmakers", []):
            last_update = _parse_time(bookmaker["last_update"])
            lay_by_outcome = _exchange_lay_prices(bookmaker)
            rejected_exchange_backs = _wide_exchange_back_markets(bookmaker)
            for market in bookmaker.get("markets", []):
                if market.get("key") == "h2h_lay":
                    continue
                for outcome in market.get("outcomes", []):
                    outcome_name = outcome["name"]
                    point = outcome.get("point")
                    if (market["key"], outcome_name, point) in rejected_exchange_backs:
                        continue
                    prices.append(
                        OutcomePrice(
                            bookmaker_key=bookmaker["key"],
                            bookmaker_title=bookmaker["title"],
                            sport_key=event.get("sport_key", ""),
                            event_id=event["id"],
                            event_name=event_name,
                            commence_time=commence_time,
                            market_key=market["key"],
                            market_name=market["key"],
                            outcome_name=outcome_name,
                            point=point,
                            odds=float(outcome["price"]),
                            last_update=last_update,
                            exchange_lay_odds=lay_by_outcome.get(
                                (market["key"], outcome_name, point)
                            ),
                            exchange_spread_pct=_exchange_spread_or_none(
                                float(outcome["price"]),
                                lay_by_outcome.get((market["key"], outcome_name, point)),
                            ),
                        )
                    )
    return prices


def _exchange_lay_prices(bookmaker: dict[str, Any]) -> dict[tuple[str, str, float | None], float]:
    markets = bookmaker.get("markets", [])
    lay_by_outcome: dict[tuple[str, str, float | None], float] = {}
    for market in markets:
        if not str(market.get("key", "")).endswith("_lay"):
            continue
        back_market_key = str(market["key"]).removesuffix("_lay")
        for outcome in market.get("outcomes", []):
            lay_by_outcome[(back_market_key, outcome["name"], outcome.get("point"))] = float(
                outcome["price"]
            )
    return lay_by_outcome


def _wide_exchange_back_markets(bookmaker: dict[str, Any]) -> set[tuple[str, str, float | None]]:
    markets = bookmaker.get("markets", [])
    lay_by_outcome = _exchange_lay_prices(bookmaker)
    rejected: set[tuple[str, str, float | None]] = set()
    for market in markets:
        market_key = str(market.get("key", ""))
        if market_key.endswith("_lay"):
            continue
        for outcome in market.get("outcomes", []):
            key = (market_key, outcome["name"], outcome.get("point"))
            lay_odds = lay_by_outcome.get(key)
            if lay_odds is None:
                continue
            back_odds = float(outcome["price"])
            if _exchange_spread(back_odds, lay_odds) > MAX_EXCHANGE_BACK_LAY_SPREAD:
                rejected.add(key)
    return rejected


def _exchange_spread(back_odds: float, lay_odds: float) -> float:
    if back_odds <= 1 or lay_odds <= 1:
        return float("inf")
    midpoint = (back_odds + lay_odds) / 2
    return (lay_odds - back_odds) / midpoint


def _exchange_spread_or_none(back_odds: float, lay_odds: float | None) -> float | None:
    if lay_odds is None:
        return None
    return _exchange_spread(back_odds, lay_odds)


def find_value_opportunities(
    prices: list[OutcomePrice],
    *,
    target_bookmakers: set[str],
    reference_bookmakers: set[str] | None,
    min_edge: float,
    max_age_seconds: int,
    min_reference_books: int,
    include_started: bool = False,
    allow_target_bookmakers_as_references: bool = False,
    reference_weights: dict[str, float] | None = None,
    target_commission_rates: dict[str, float] | None = None,
    reference_aggregation: str = "mean",
    poisson_total_conversion: bool = False,
    poisson_total_max_line_distance: float = 0.5,
    target_lay_bookmakers: set[str] | None = None,
    now: datetime | None = None,
) -> list[ValueSignal]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=max_age_seconds)
    fresh_prices = [
        price
        for price in prices
        if price.last_update >= cutoff and (include_started or price.commence_time > now)
    ]

    signals: list[ValueSignal] = []
    if poisson_total_conversion:
        signals.extend(
            _poisson_total_value_opportunities(
                fresh_prices,
                target_bookmakers=target_bookmakers,
                reference_bookmakers=reference_bookmakers,
                min_edge=min_edge,
                min_reference_books=min_reference_books,
                allow_target_bookmakers_as_references=allow_target_bookmakers_as_references,
                target_commission_rates=target_commission_rates,
                reference_aggregation=reference_aggregation,
                max_line_distance=poisson_total_max_line_distance,
            )
        )

    exact_prices = [
        price
        for price in fresh_prices
        if not (poisson_total_conversion and price.market_key == "totals")
    ]
    grouped: dict[tuple[str, str, float | None], list[OutcomePrice]] = {}
    for price in exact_prices:
        grouped.setdefault(
            (price.event_id, price.market_key, _market_line_key(price)),
            [],
        ).append(price)

    for market_prices in grouped.values():
        target_prices = _best_target_prices(
            price for price in market_prices if _bookmaker_matches(price, target_bookmakers)
        )
        base_reference_prices = [
            price
            for price in market_prices
            if (
                allow_target_bookmakers_as_references
                or not _bookmaker_matches(price, target_bookmakers)
            )
            and (reference_bookmakers is None or _bookmaker_matches(price, reference_bookmakers))
        ]
        if not target_prices or not base_reference_prices:
            continue

        expected_outcomes = _expected_outcome_count(market_prices)
        for target in target_prices:
            reference_prices = [
                price
                for price in base_reference_prices
                if _bookmaker_identity(price) != _bookmaker_identity(target)
            ]
            target_venue_fair_odds = _target_venue_fair_odds(target)
            external_fair_probabilities = _fair_probabilities(
                reference_prices,
                min_reference_books,
                expected_outcomes=expected_outcomes,
                reference_weights=reference_weights,
                aggregation=reference_aggregation,
            )
            if target.comparable_outcome_name not in external_fair_probabilities:
                continue
            reference_diagnostics = _reference_diagnostics(
                reference_prices,
                target.comparable_outcome_name,
                expected_outcomes=expected_outcomes,
            )
            reference_prices.extend(
                _target_venue_fair_value_reference_prices(
                    market_prices,
                    target,
                    reference_weights=reference_weights,
                )
            )
            fair_probabilities = _fair_probabilities(
                reference_prices,
                min_reference_books,
                expected_outcomes=expected_outcomes,
                reference_weights=reference_weights,
                aggregation=reference_aggregation,
            )
            fair_probability = fair_probabilities.get(target.comparable_outcome_name)
            if fair_probability is None:
                continue

            target_effective_odds = effective_decimal_odds(
                target.odds,
                _target_commission_rate(target, target_commission_rates),
            )
            edge = (target_effective_odds * fair_probability) - 1
            references = tuple(
                sorted(
                    {
                        price.bookmaker_title
                        for price in reference_prices
                        if price.comparable_outcome_name == target.comparable_outcome_name
                    }
                )
            )
            if edge >= min_edge:
                signals.append(
                    ValueSignal(
                        sport_key=target.sport_key,
                        event_id=target.event_id,
                        event_name=target.event_name,
                        commence_time=target.commence_time,
                        market_key=target.market_key,
                        outcome_name=target.comparable_outcome_name,
                        target_bookmaker=target.bookmaker_title,
                        target_odds=target.odds,
                        target_effective_odds=target_effective_odds,
                        reference_fair_odds=1 / fair_probability,
                        reference_probability=fair_probability,
                        edge=edge,
                        reference_bookmakers=references,
                        betfair_fair_odds=target_venue_fair_odds,
                        betfair_fair_edge=_betfair_fair_edge(
                            target_effective_odds,
                            target_venue_fair_odds,
                        ),
                        betfair_back_lay_spread_pct=target.exchange_spread_pct,
                        reference_fair_odds_by_bookmaker=reference_diagnostics[
                            "fair_odds_by_bookmaker"
                        ],
                        reference_spread_pct_by_bookmaker=reference_diagnostics[
                            "spread_pct_by_bookmaker"
                        ],
                        reference_last_update_by_bookmaker=reference_diagnostics[
                            "last_update_by_bookmaker"
                        ],
                        reference_disagreement_pct=reference_diagnostics[
                            "disagreement_pct"
                        ],
                        reference_max_spread_pct=reference_diagnostics["max_spread_pct"],
                        reference_avg_spread_pct=reference_diagnostics["avg_spread_pct"],
                    )
                )

            if target.market_key == "h2h" and target.exchange_lay_odds is not None:
                lay_bookmakers = target_lay_bookmakers or set()
                if lay_bookmakers and _bookmaker_matches(target, lay_bookmakers):
                    lay_edge = lay_edge_per_liability(
                        lay_odds=target.exchange_lay_odds,
                        fair_probability=fair_probability,
                        commission_rate=_target_commission_rate(target, target_commission_rates),
                    )
                    if lay_edge >= min_edge:
                        signals.append(
                            ValueSignal(
                                sport_key=target.sport_key,
                                event_id=target.event_id,
                                event_name=target.event_name,
                                commence_time=target.commence_time,
                                market_key=target.market_key,
                                outcome_name=target.comparable_outcome_name,
                                target_bookmaker=target.bookmaker_title,
                                target_odds=target.exchange_lay_odds,
                                target_effective_odds=target.exchange_lay_odds,
                                reference_fair_odds=1 / fair_probability,
                                reference_probability=fair_probability,
                                edge=lay_edge,
                                reference_bookmakers=references,
                                bet_side="lay",
                                betfair_fair_odds=target_venue_fair_odds,
                                betfair_fair_edge=None,
                                betfair_back_lay_spread_pct=target.exchange_spread_pct,
                                reference_fair_odds_by_bookmaker=reference_diagnostics[
                                    "fair_odds_by_bookmaker"
                                ],
                                reference_spread_pct_by_bookmaker=reference_diagnostics[
                                    "spread_pct_by_bookmaker"
                                ],
                                reference_last_update_by_bookmaker=reference_diagnostics[
                                    "last_update_by_bookmaker"
                                ],
                                reference_disagreement_pct=reference_diagnostics[
                                    "disagreement_pct"
                                ],
                                reference_max_spread_pct=reference_diagnostics[
                                    "max_spread_pct"
                                ],
                                reference_avg_spread_pct=reference_diagnostics[
                                    "avg_spread_pct"
                                ],
                            )
                        )

    return sorted(signals, key=lambda signal: signal.edge, reverse=True)


def _poisson_total_value_opportunities(
    prices: list[OutcomePrice],
    *,
    target_bookmakers: set[str],
    reference_bookmakers: set[str] | None,
    min_edge: float,
    min_reference_books: int,
    allow_target_bookmakers_as_references: bool,
    target_commission_rates: dict[str, float] | None,
    reference_aggregation: str,
    max_line_distance: float,
) -> list[ValueSignal]:
    by_event: dict[str, list[OutcomePrice]] = {}
    for price in prices:
        if price.market_key == "totals" and price.point is not None:
            by_event.setdefault(price.event_id, []).append(price)

    signals: list[ValueSignal] = []
    for event_prices in by_event.values():
        target_prices = _best_target_prices(
            price for price in event_prices if _bookmaker_matches(price, target_bookmakers)
        )
        if not target_prices:
            continue
        base_reference_prices = [
            price
            for price in event_prices
            if (
                allow_target_bookmakers_as_references
                or not _bookmaker_matches(price, target_bookmakers)
            )
            and (reference_bookmakers is None or _bookmaker_matches(price, reference_bookmakers))
        ]
        if not base_reference_prices:
            continue

        for target in target_prices:
            reference_fits = _poisson_total_reference_fits(
                [
                    price
                    for price in base_reference_prices
                    if _bookmaker_identity(price) != _bookmaker_identity(target)
                    and abs(float(price.point) - float(target.point)) <= max_line_distance
                ],
                aggregation=reference_aggregation,
            )
            if len(reference_fits) < min_reference_books:
                continue
            lambda_total = _aggregate_poisson_lambdas(
                [fit.lambda_total for fit in reference_fits],
                aggregation=reference_aggregation,
            )
            fair_probability = _poisson_total_probability(
                lambda_total,
                side=target.outcome_name,
                line=float(target.point),
            )
            if fair_probability <= 0:
                continue
            target_effective_odds = effective_decimal_odds(
                target.odds,
                _target_commission_rate(target, target_commission_rates),
            )
            edge = (target_effective_odds * fair_probability) - 1
            if edge < min_edge:
                continue
            references = tuple(sorted({fit.bookmaker_title for fit in reference_fits}))
            signals.append(
                ValueSignal(
                    sport_key=target.sport_key,
                    event_id=target.event_id,
                    event_name=target.event_name,
                    commence_time=target.commence_time,
                    market_key=target.market_key,
                    outcome_name=target.comparable_outcome_name,
                    target_bookmaker=target.bookmaker_title,
                    target_odds=target.odds,
                    target_effective_odds=target_effective_odds,
                    reference_fair_odds=1 / fair_probability,
                    reference_probability=fair_probability,
                    edge=edge,
                    reference_bookmakers=references,
                    betfair_fair_odds=_target_venue_fair_odds(target),
                    betfair_fair_edge=_betfair_fair_edge(
                        target_effective_odds,
                        _target_venue_fair_odds(target),
                    ),
                    betfair_back_lay_spread_pct=target.exchange_spread_pct,
                )
            )
    return signals


@dataclass(frozen=True)
class _PoissonTotalFit:
    bookmaker_key: str
    bookmaker_title: str
    line: float
    lambda_total: float


def _poisson_total_reference_fits(
    prices: list[OutcomePrice],
    *,
    aggregation: str,
) -> list[_PoissonTotalFit]:
    by_book_line: dict[tuple[str, float], dict[str, OutcomePrice]] = {}
    for price in prices:
        by_book_line.setdefault((_bookmaker_identity(price), float(price.point)), {})[
            price.outcome_name.casefold()
        ] = price

    line_fits: list[_PoissonTotalFit] = []
    for (bookmaker_key, line), outcomes in by_book_line.items():
        over = outcomes.get("over")
        under = outcomes.get("under")
        if over is None or under is None:
            continue
        overround = (1 / over.odds) + (1 / under.odds)
        if overround <= 0:
            continue
        fair_over_probability = (1 / over.odds) / overround
        lambda_total = _solve_poisson_total_lambda(
            line=line,
            fair_over_probability=fair_over_probability,
        )
        if lambda_total is None:
            continue
        line_fits.append(
            _PoissonTotalFit(
                bookmaker_key=bookmaker_key,
                bookmaker_title=over.bookmaker_title,
                line=line,
                lambda_total=lambda_total,
            )
        )

    by_bookmaker: dict[str, list[_PoissonTotalFit]] = {}
    for fit in line_fits:
        by_bookmaker.setdefault(fit.bookmaker_key, []).append(fit)

    fits = []
    for bookmaker_fits in by_bookmaker.values():
        fits.append(
            _PoissonTotalFit(
                bookmaker_key=bookmaker_fits[0].bookmaker_key,
                bookmaker_title=bookmaker_fits[0].bookmaker_title,
                line=_aggregate_poisson_lambdas(
                    [fit.line for fit in bookmaker_fits],
                    aggregation=aggregation,
                ),
                lambda_total=_aggregate_poisson_lambdas(
                    [fit.lambda_total for fit in bookmaker_fits],
                    aggregation=aggregation,
                ),
            )
        )
    return fits


def _aggregate_poisson_lambdas(values: list[float], *, aggregation: str) -> float:
    if aggregation == "median":
        return median(values)
    if aggregation == "mean":
        return sum(values) / len(values)
    raise ValueError(f"Unsupported reference aggregation: {aggregation}")


def _solve_poisson_total_lambda(
    *,
    line: float,
    fair_over_probability: float,
) -> float | None:
    if not 0 < fair_over_probability < 1:
        return None
    low = 0.01
    high = 20.0
    for _ in range(80):
        mid = (low + high) / 2
        probability = _poisson_total_probability(mid, side="Over", line=line)
        if probability < fair_over_probability:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def _poisson_total_probability(lambda_total: float, *, side: str, line: float) -> float:
    win_units, push_units = _poisson_total_win_push_units(lambda_total, side=side, line=line)
    if win_units <= 0:
        return 0.0
    fair_odds = (1 - push_units) / win_units
    if fair_odds <= 0:
        return 0.0
    return 1 / fair_odds


def _poisson_total_win_push_units(
    lambda_total: float,
    *,
    side: str,
    line: float,
) -> tuple[float, float]:
    components = _asian_total_components(line)
    win_units = 0.0
    push_units = 0.0
    for component in components:
        win, push = _poisson_total_single_line_win_push(lambda_total, side=side, line=component)
        win_units += win / len(components)
        push_units += push / len(components)
    return win_units, push_units


def _asian_total_components(line: float) -> tuple[float, ...]:
    doubled = round(line * 2)
    if abs(line * 2 - doubled) < 1e-9:
        return (line,)
    return (line - 0.25, line + 0.25)


def _poisson_total_single_line_win_push(
    lambda_total: float,
    *,
    side: str,
    line: float,
) -> tuple[float, float]:
    integer_line = round(line)
    has_push = abs(line - integer_line) < 1e-9
    threshold = int(math.floor(line))
    if side.casefold() == "over":
        win = 1 - _poisson_cdf(threshold, lambda_total)
    elif side.casefold() == "under":
        if has_push:
            win = _poisson_cdf(integer_line - 1, lambda_total)
        else:
            win = _poisson_cdf(threshold, lambda_total)
    else:
        return 0.0, 0.0
    push = _poisson_pmf(integer_line, lambda_total) if has_push else 0.0
    return win, push


def _poisson_cdf(k: int, lambda_total: float) -> float:
    if k < 0:
        return 0.0
    probability = math.exp(-lambda_total)
    total = probability
    for value in range(1, k + 1):
        probability *= lambda_total / value
        total += probability
    return total


def _poisson_pmf(k: int, lambda_total: float) -> float:
    if k < 0:
        return 0.0
    return math.exp(-lambda_total) * (lambda_total**k) / math.factorial(k)


def betfair_top_of_book_fair_odds(back_odds: float, lay_odds: float) -> float | None:
    if back_odds <= 1 or lay_odds <= 1:
        return None
    fair_probability = ((1 / back_odds) + (1 / lay_odds)) / 2
    if fair_probability <= 0:
        return None
    return 1 / fair_probability


def _target_venue_fair_odds(price: OutcomePrice) -> float | None:
    if price.exchange_lay_odds is None:
        return price.odds
    return betfair_top_of_book_fair_odds(price.odds, price.exchange_lay_odds)


def _target_venue_fair_value_reference_prices(
    market_prices: list[OutcomePrice],
    target: OutcomePrice,
    *,
    reference_weights: dict[str, float] | None,
) -> list[OutcomePrice]:
    if not reference_weights or "target venue fair value" not in reference_weights:
        return []
    synthetic_prices = []
    for price in market_prices:
        if _bookmaker_identity(price) != _bookmaker_identity(target):
            continue
        fair_odds = _target_venue_fair_odds(price)
        if fair_odds is None:
            continue
        synthetic_prices.append(
            OutcomePrice(
                bookmaker_key="target_venue_fair_value",
                bookmaker_title="Target Venue Fair Value",
                sport_key=price.sport_key,
                event_id=price.event_id,
                event_name=price.event_name,
                commence_time=price.commence_time,
                market_key=price.market_key,
                market_name=price.market_name,
                outcome_name=price.outcome_name,
                point=price.point,
                odds=fair_odds,
                last_update=price.last_update,
            )
        )
    return synthetic_prices


def _betfair_fair_edge(effective_odds: float, fair_odds: float | None) -> float | None:
    if fair_odds is None:
        return None
    return (effective_odds / fair_odds) - 1


def effective_decimal_odds(decimal_odds: float, commission_rate: float = 0.0) -> float:
    if commission_rate <= 0:
        return decimal_odds
    if commission_rate >= 1:
        raise ValueError("commission_rate must be below 1")
    if decimal_odds <= 1:
        return decimal_odds
    return 1 + ((decimal_odds - 1) * (1 - commission_rate))


def lay_edge_per_liability(
    *,
    lay_odds: float,
    fair_probability: float,
    commission_rate: float = 0.0,
) -> float:
    if lay_odds <= 1:
        return float("-inf")
    liability = lay_odds - 1
    win_probability = 1 - fair_probability
    expected_profit = (win_probability * (1 - commission_rate)) - (
        fair_probability * liability
    )
    return expected_profit / liability


def _target_commission_rate(
    price: OutcomePrice,
    commission_rates: dict[str, float] | None,
) -> float:
    if not commission_rates:
        return 0.0
    for key in (_bookmaker_identity(price), price.bookmaker_key.lower()):
        if key in commission_rates:
            return commission_rates[key]
    return commission_rates.get("*", 0.0)


def _fair_probabilities(
    prices: list[OutcomePrice],
    min_reference_books: int,
    *,
    expected_outcomes: int,
    reference_weights: dict[str, float] | None = None,
    aggregation: str = "mean",
) -> dict[str, float]:
    by_bookmaker: dict[str, dict[str, OutcomePrice]] = {}
    for price in prices:
        by_bookmaker.setdefault(_bookmaker_identity(price), {})[
            price.comparable_outcome_name
        ] = price

    normalised_probs: dict[str, list[tuple[float, float]]] = {}
    for bookmaker_identity, bookmaker_prices in by_bookmaker.items():
        if len(bookmaker_prices) != expected_outcomes:
            continue
        overround = sum(
            _reference_implied_probability(price) for price in bookmaker_prices.values()
        )
        if overround <= 0:
            continue
        weight = _reference_weight(bookmaker_identity, reference_weights)
        for outcome_name, price in bookmaker_prices.items():
            normalised_probs.setdefault(outcome_name, []).append(
                (_reference_implied_probability(price) / overround, weight)
            )

    if aggregation == "median":
        return _median_probabilities(
            normalised_probs,
            min_reference_books=min_reference_books,
            expected_outcomes=expected_outcomes,
        )
    if aggregation != "mean":
        raise ValueError(f"Unsupported reference aggregation: {aggregation}")
    return _mean_probabilities(normalised_probs, min_reference_books=min_reference_books)


def _reference_diagnostics(
    prices: list[OutcomePrice],
    outcome_name: str,
    *,
    expected_outcomes: int,
) -> dict[str, Any]:
    by_bookmaker: dict[str, dict[str, OutcomePrice]] = {}
    for price in prices:
        by_bookmaker.setdefault(_bookmaker_identity(price), {})[
            price.comparable_outcome_name
        ] = price

    fair_odds_by_bookmaker: list[tuple[str, float]] = []
    spread_pct_by_bookmaker: list[tuple[str, float]] = []
    last_update_by_bookmaker: list[tuple[str, str]] = []
    for bookmaker_prices in by_bookmaker.values():
        if len(bookmaker_prices) != expected_outcomes or outcome_name not in bookmaker_prices:
            continue
        overround = sum(
            _reference_implied_probability(price) for price in bookmaker_prices.values()
        )
        if overround <= 0:
            continue
        selected = bookmaker_prices[outcome_name]
        probability = _reference_implied_probability(selected) / overround
        if probability <= 0:
            continue
        fair_odds_by_bookmaker.append((selected.bookmaker_title, 1 / probability))
        last_update_by_bookmaker.append(
            (selected.bookmaker_title, selected.last_update.isoformat())
        )
        if selected.exchange_spread_pct is not None:
            spread_pct_by_bookmaker.append(
                (selected.bookmaker_title, selected.exchange_spread_pct)
            )

    fair_odds = [odds for _, odds in fair_odds_by_bookmaker]
    spread_pct = [spread for _, spread in spread_pct_by_bookmaker]
    return {
        "fair_odds_by_bookmaker": tuple(sorted(fair_odds_by_bookmaker)),
        "spread_pct_by_bookmaker": tuple(sorted(spread_pct_by_bookmaker)),
        "last_update_by_bookmaker": tuple(sorted(last_update_by_bookmaker)),
        "disagreement_pct": _range_pct(fair_odds),
        "max_spread_pct": max(spread_pct) if spread_pct else None,
        "avg_spread_pct": sum(spread_pct) / len(spread_pct) if spread_pct else None,
    }


def _reference_implied_probability(price: OutcomePrice) -> float:
    fair_odds = _target_venue_fair_odds(price)
    if fair_odds is None or fair_odds <= 1:
        return 0.0
    return 1 / fair_odds


def _range_pct(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    midpoint = median(values)
    if midpoint <= 0:
        return None
    return (max(values) - min(values)) / midpoint


def _json_diagnostic(items: tuple[tuple[str, float | str], ...]) -> str:
    if not items:
        return ""
    return json.dumps(dict(items), sort_keys=True, separators=(",", ":"))


def _reference_weight(
    bookmaker_identity: str,
    reference_weights: dict[str, float] | None,
) -> float:
    if not reference_weights:
        return 1.0
    return reference_weights.get(bookmaker_identity, reference_weights.get("*", 1.0))


def _weighted_average(probabilities: list[tuple[float, float]]) -> float:
    weight_sum = sum(weight for _, weight in probabilities)
    return sum(probability * weight for probability, weight in probabilities) / weight_sum


def _mean_probabilities(
    normalised_probs: dict[str, list[tuple[float, float]]],
    *,
    min_reference_books: int,
) -> dict[str, float]:
    return {
        outcome_name: _weighted_average(probabilities)
        for outcome_name, probabilities in normalised_probs.items()
        if len(probabilities) >= min_reference_books and sum(weight for _, weight in probabilities) > 0
    }


def _median_probabilities(
    normalised_probs: dict[str, list[tuple[float, float]]],
    *,
    min_reference_books: int,
    expected_outcomes: int,
) -> dict[str, float]:
    medians = {
        outcome_name: median(probability for probability, _ in probabilities)
        for outcome_name, probabilities in normalised_probs.items()
        if len(probabilities) >= min_reference_books
    }
    if len(medians) != expected_outcomes:
        return {}
    total = sum(medians.values())
    if total <= 0:
        return {}
    return {outcome_name: probability / total for outcome_name, probability in medians.items()}


def _expected_outcome_count(prices: list[OutcomePrice]) -> int:
    by_bookmaker: dict[str, set[str]] = {}
    for price in prices:
        by_bookmaker.setdefault(price.bookmaker_key, set()).add(price.comparable_outcome_name)
    return max((len(outcomes) for outcomes in by_bookmaker.values()), default=0)


def _market_line_key(price: OutcomePrice) -> float | None:
    if price.point is None:
        return None
    if price.market_key == "spreads":
        return abs(float(price.point))
    if price.market_key == "totals":
        return float(price.point)
    return None


def _best_target_prices(prices: Iterable[OutcomePrice]) -> list[OutcomePrice]:
    best: dict[tuple[str, str, str, str], OutcomePrice] = {}
    for price in prices:
        key = (
            price.event_id,
            price.market_key,
            price.comparable_outcome_name,
            price.bookmaker_title.casefold(),
        )
        existing = best.get(key)
        if existing is None or price.odds > existing.odds:
            best[key] = price
    return list(best.values())


def _bookmaker_matches(price: OutcomePrice, bookmakers: set[str]) -> bool:
    return price.bookmaker_key in bookmakers or price.bookmaker_title.casefold() in bookmakers


def bookmaker_url(bookmaker: str) -> str:
    key = (
        bookmaker.casefold()
        .replace(" ", "")
        .replace("(", "")
        .replace(")", "")
        .replace("-", "")
        .replace("_", "")
    )
    aliases = {
        "ladbrokesuk": "ladbrokes",
        "unibetuk": "unibet_uk",
        "williamhill": "williamhill",
        "betvictor": "betvictor",
        "paddypower": "paddypower",
        "skybet": "skybet",
        "livescorebet": "livescorebet",
        "virginbet": "virginbet",
    }
    return BOOKMAKER_URLS.get(aliases.get(key, key), "")


def bookmaker_event_search_url(bookmaker: str, event_name: str) -> str:
    base_url = bookmaker_url(bookmaker)
    if not base_url:
        return ""
    query = quote_plus(f"{bookmaker} {event_name}")
    return f"https://www.google.com/search?q={query}"


def h2h_winners_from_scores(scores_payloads: list[list[dict[str, Any]]]) -> dict[str, str]:
    winners: dict[str, str] = {}
    for payload in scores_payloads:
        for event in payload:
            if not event.get("completed"):
                continue
            scores = event.get("scores") or []
            if len(scores) < 2:
                continue
            parsed_scores = []
            for item in scores:
                score = _parse_score(item.get("score"))
                if score is None:
                    parsed_scores = []
                    break
                parsed_scores.append((item.get("name", ""), score))
            if len(parsed_scores) < 2:
                continue
            best_score = max(score for _, score in parsed_scores)
            winners_for_score = [name for name, score in parsed_scores if score == best_score]
            if len(winners_for_score) == 1:
                winners[event["id"]] = winners_for_score[0]
            else:
                winners[event["id"]] = "Draw"
    return winners


def _parse_score(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bookmaker_identity(price: OutcomePrice) -> str:
    return price.bookmaker_title.casefold() or price.bookmaker_key


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
