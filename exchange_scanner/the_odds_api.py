from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    target_effective_odds: float | None = None

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
            "target_bookmaker_url": bookmaker_url(self.target_bookmaker),
            "event_search_url": bookmaker_event_search_url(self.target_bookmaker, self.event_name),
            "copy_search": f"{self.event_name} {self.outcome_name}",
            "copy_bet_instruction": (
                f"{self.target_bookmaker}: {self.event_name} - "
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
                        )
                    )
    return prices


def _wide_exchange_back_markets(bookmaker: dict[str, Any]) -> set[tuple[str, str, float | None]]:
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
    now: datetime | None = None,
) -> list[ValueSignal]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=max_age_seconds)
    fresh_prices = [
        price
        for price in prices
        if price.last_update >= cutoff and (include_started or price.commence_time > now)
    ]

    grouped: dict[tuple[str, str], list[OutcomePrice]] = {}
    for price in fresh_prices:
        grouped.setdefault((price.event_id, price.market_key), []).append(price)

    signals: list[ValueSignal] = []
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
            fair_probabilities = _fair_probabilities(
                reference_prices,
                min_reference_books,
                expected_outcomes=expected_outcomes,
                reference_weights=reference_weights,
            )
            fair_probability = fair_probabilities.get(target.comparable_outcome_name)
            if fair_probability is None:
                continue

            target_effective_odds = effective_decimal_odds(
                target.odds,
                _target_commission_rate(target, target_commission_rates),
            )
            edge = (target_effective_odds * fair_probability) - 1
            if edge >= min_edge:
                references = tuple(
                    sorted(
                        {
                            price.bookmaker_title
                            for price in reference_prices
                            if price.comparable_outcome_name == target.comparable_outcome_name
                        }
                    )
                )
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
                    )
                )

    return sorted(signals, key=lambda signal: signal.edge, reverse=True)


def effective_decimal_odds(decimal_odds: float, commission_rate: float = 0.0) -> float:
    if commission_rate <= 0:
        return decimal_odds
    if commission_rate >= 1:
        raise ValueError("commission_rate must be below 1")
    if decimal_odds <= 1:
        return decimal_odds
    return 1 + ((decimal_odds - 1) * (1 - commission_rate))


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
        overround = sum(1 / price.odds for price in bookmaker_prices.values())
        if overround <= 0:
            continue
        weight = _reference_weight(bookmaker_identity, reference_weights)
        for outcome_name, price in bookmaker_prices.items():
            normalised_probs.setdefault(outcome_name, []).append(
                ((1 / price.odds) / overround, weight)
            )

    return {
        outcome_name: _weighted_average(probabilities)
        for outcome_name, probabilities in normalised_probs.items()
        if len(probabilities) >= min_reference_books and sum(weight for _, weight in probabilities) > 0
    }


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


def _expected_outcome_count(prices: list[OutcomePrice]) -> int:
    by_bookmaker: dict[str, set[str]] = {}
    for price in prices:
        by_bookmaker.setdefault(price.bookmaker_key, set()).add(price.comparable_outcome_name)
    return max((len(outcomes) for outcomes in by_bookmaker.values()), default=0)


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
