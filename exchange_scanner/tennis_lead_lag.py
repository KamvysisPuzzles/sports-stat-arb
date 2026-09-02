from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from exchange_scanner.the_odds_api import OutcomePrice, ValueSignal, effective_decimal_odds

TENNIS_LEAD_LAG_STRATEGY = "tennis-lead-lag-v1"
TENNIS_LEAD_LAG_VERSION = "tennis_lead_lag_v1"
_STATE_PREFIX = "control#tennis-lead-lag#"
_SHARP_BOOKMAKERS = {"pinnacle", "betfair", "matchbook", "smarkets"}
# Smarkets remains a confirmer until its direct liquidity mapper supports tennis events.
_TARGET_BOOKMAKERS = {"betfair", "matchbook"}
_COMMISSION_RATE = 0.02


@dataclass(frozen=True)
class TennisLeadLagConfig:
    lookback_min_seconds: int = 180
    lookback_max_seconds: int = 600
    preferred_lookback_seconds: int = 360
    history_retention_seconds: int = 720
    max_reference_age_seconds: int = 30
    max_hours_to_start: float = 6.0
    min_anchor_move_probability: float = 0.01
    min_confirmation_move_probability: float = 0.005
    min_target_lag_probability: float = 0.005
    min_edge: float = 0.01
    max_edge: float = 0.03
    min_target_odds: float = 1.35
    max_target_odds: float = 3.50
    max_target_spread_pct: float = 0.06


@dataclass(frozen=True)
class TennisLeadLagRun:
    signals: tuple[ValueSignal, ...]
    sports_with_prices: int
    sports_with_history: int
    state_updates: int
    state_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class _BookMarket:
    identity: str
    title: str
    last_update: datetime
    prices: dict[str, OutcomePrice]
    fair_probabilities: dict[str, float]


def evaluate_and_record_tennis_lead_lag(
    table: Any,
    prices: list[OutcomePrice],
    *,
    now: datetime,
    config: TennisLeadLagConfig | None = None,
) -> TennisLeadLagRun:
    config = config or TennisLeadLagConfig()
    by_sport: dict[str, list[OutcomePrice]] = {}
    for price in prices:
        if price.sport_key.startswith(("tennis_atp_", "tennis_wta_")) and price.market_key == "h2h":
            by_sport.setdefault(price.sport_key, []).append(price)

    signals: list[ValueSignal] = []
    sports_with_history = 0
    state_updates = 0
    errors: list[str] = []
    for sport_key, sport_prices in by_sport.items():
        try:
            history = _load_history(table, sport_key)
        except Exception as exc:  # noqa: BLE001 - experimental state must not block trading.
            history = []
            errors.append(f"load:{sport_key}:{type(exc).__name__}:{exc}")

        baseline = _baseline_snapshot(history, now=now, config=config)
        if baseline is not None:
            sports_with_history += 1
            signals.extend(
                find_tennis_lead_lag_signals(
                    sport_prices,
                    baseline=baseline,
                    now=now,
                    config=config,
                )
            )

        updated_history = _updated_history(
            history,
            sport_prices,
            now=now,
            retention_seconds=config.history_retention_seconds,
        )
        try:
            _store_history(table, sport_key, updated_history, now=now)
            state_updates += 1
        except Exception as exc:  # noqa: BLE001 - experimental state must not block trading.
            errors.append(f"store:{sport_key}:{type(exc).__name__}:{exc}")

    return TennisLeadLagRun(
        signals=tuple(_unique_signals(signals)),
        sports_with_prices=len(by_sport),
        sports_with_history=sports_with_history,
        state_updates=state_updates,
        state_errors=tuple(errors),
    )


def find_tennis_lead_lag_signals(
    prices: list[OutcomePrice],
    *,
    baseline: dict[str, Any],
    now: datetime,
    config: TennisLeadLagConfig | None = None,
) -> list[ValueSignal]:
    config = config or TennisLeadLagConfig()
    current_books = _book_markets(prices)
    old_probabilities = _snapshot_probabilities(baseline)
    observed_at = _parse_time(str(baseline.get("observed_at") or ""))
    lookback_seconds = (now - observed_at).total_seconds() if observed_at else 0.0
    signals: list[ValueSignal] = []

    event_markets = sorted({(event_id, market) for event_id, market, _ in current_books})
    for event_id, market_key in event_markets:
        books = {
            identity: book
            for (book_event_id, book_market, identity), book in current_books.items()
            if book_event_id == event_id and book_market == market_key
        }
        pinnacle = books.get("pinnacle")
        if pinnacle is None or not _fresh_book(pinnacle, now=now, config=config):
            continue

        for outcome_name, anchor_probability in pinnacle.fair_probabilities.items():
            old_anchor = old_probabilities.get((event_id, market_key, "pinnacle", outcome_name))
            if old_anchor is None:
                continue
            anchor_move = anchor_probability - old_anchor
            if anchor_move < config.min_anchor_move_probability:
                continue

            for target_identity in sorted(_TARGET_BOOKMAKERS & books.keys()):
                target = books[target_identity]
                target_price = target.prices.get(outcome_name)
                target_probability = target.fair_probabilities.get(outcome_name)
                if target_price is None or target_probability is None:
                    continue
                if not _eligible_target(target_price, target, now=now, config=config):
                    continue

                confirmations: list[_BookMarket] = []
                confirmation_moves: list[float] = []
                for identity, confirmer in books.items():
                    if identity in {"pinnacle", target_identity}:
                        continue
                    if not _fresh_book(confirmer, now=now, config=config):
                        continue
                    current_probability = confirmer.fair_probabilities.get(outcome_name)
                    old_probability = old_probabilities.get(
                        (event_id, market_key, identity, outcome_name)
                    )
                    if current_probability is None or old_probability is None:
                        continue
                    move = current_probability - old_probability
                    if move >= config.min_confirmation_move_probability:
                        confirmations.append(confirmer)
                        confirmation_moves.append(move)
                if not confirmations:
                    continue

                reference_probabilities = [anchor_probability]
                reference_probabilities.extend(
                    confirmer.fair_probabilities[outcome_name] for confirmer in confirmations
                )
                fair_probability = median(reference_probabilities)
                target_lag_probability = fair_probability - target_probability
                if target_lag_probability < config.min_target_lag_probability:
                    continue

                effective_odds = effective_decimal_odds(target_price.odds, _COMMISSION_RATE)
                edge = (effective_odds * fair_probability) - 1
                if edge < config.min_edge or edge > config.max_edge:
                    continue

                references = [pinnacle, *confirmations]
                signals.append(
                    ValueSignal(
                        sport_key=target_price.sport_key,
                        event_id=target_price.event_id,
                        event_name=target_price.event_name,
                        commence_time=target_price.commence_time,
                        market_key=target_price.market_key,
                        outcome_name=target_price.comparable_outcome_name,
                        target_bookmaker=target_price.bookmaker_title,
                        target_odds=target_price.odds,
                        target_effective_odds=effective_odds,
                        reference_fair_odds=1 / fair_probability,
                        reference_probability=fair_probability,
                        edge=edge,
                        reference_bookmakers=tuple(sorted({book.title for book in references})),
                        bet_side="back",
                        betfair_back_lay_spread_pct=target_price.exchange_spread_pct,
                        reference_fair_odds_by_bookmaker=tuple(
                            sorted(
                                (book.title, 1 / book.fair_probabilities[outcome_name])
                                for book in references
                            )
                        ),
                        reference_last_update_by_bookmaker=tuple(
                            sorted((book.title, book.last_update.isoformat()) for book in references)
                        ),
                        strategy_name=TENNIS_LEAD_LAG_STRATEGY,
                        strategy_version=TENNIS_LEAD_LAG_VERSION,
                        strategy_diagnostics=(
                            ("lookback_seconds", lookback_seconds),
                            ("pinnacle_move_probability", anchor_move),
                            ("confirmation_count", float(len(confirmations))),
                            ("max_confirmation_move_probability", max(confirmation_moves)),
                            ("target_lag_probability", target_lag_probability),
                            ("target_fair_probability", target_probability),
                        ),
                    )
                )

    return sorted(signals, key=lambda signal: signal.edge, reverse=True)


def _eligible_target(
    price: OutcomePrice,
    book: _BookMarket,
    *,
    now: datetime,
    config: TennisLeadLagConfig,
) -> bool:
    if price.commence_time <= now:
        return False
    if price.commence_time > now + timedelta(hours=config.max_hours_to_start):
        return False
    if not _fresh_book(book, now=now, config=config):
        return False
    if book.identity == "betfair" and price.bookmaker_key.casefold() != "betfair_ex_uk":
        return False
    if "/" in price.event_name or "/" in price.outcome_name:
        return False
    if not config.min_target_odds <= price.odds <= config.max_target_odds:
        return False
    return (
        price.exchange_spread_pct is not None
        and price.exchange_spread_pct <= config.max_target_spread_pct
    )


def _fresh_book(
    book: _BookMarket,
    *,
    now: datetime,
    config: TennisLeadLagConfig,
) -> bool:
    return book.last_update >= now - timedelta(seconds=config.max_reference_age_seconds)


def _book_markets(prices: Iterable[OutcomePrice]) -> dict[tuple[str, str, str], _BookMarket]:
    grouped: dict[tuple[str, str, str], list[OutcomePrice]] = {}
    for price in prices:
        identity = _bookmaker_identity(price.bookmaker_key)
        if identity not in _SHARP_BOOKMAKERS or price.market_key != "h2h":
            continue
        grouped.setdefault((price.event_id, price.market_key, identity), []).append(price)

    books: dict[tuple[str, str, str], _BookMarket] = {}
    for key, rows in grouped.items():
        best_by_outcome: dict[str, OutcomePrice] = {}
        for row in rows:
            name = row.comparable_outcome_name
            current = best_by_outcome.get(name)
            if current is None or row.odds > current.odds:
                best_by_outcome[name] = row
        if len(best_by_outcome) != 2:
            continue
        raw_probabilities = {name: 1 / row.odds for name, row in best_by_outcome.items()}
        overround = sum(raw_probabilities.values())
        if overround <= 0:
            continue
        books[key] = _BookMarket(
            identity=key[2],
            title=next(iter(best_by_outcome.values())).bookmaker_title,
            last_update=min(row.last_update for row in best_by_outcome.values()),
            prices=best_by_outcome,
            fair_probabilities={
                name: probability / overround for name, probability in raw_probabilities.items()
            },
        )
    return books


def _snapshot(prices: list[OutcomePrice], *, observed_at: datetime) -> dict[str, Any]:
    rows: list[list[Any]] = []
    for (event_id, market_key, identity), book in sorted(_book_markets(prices).items()):
        for outcome_name, probability in sorted(book.fair_probabilities.items()):
            rows.append([event_id, market_key, identity, outcome_name, probability])
    return {"observed_at": observed_at.isoformat(), "probabilities": rows}


def _snapshot_probabilities(
    snapshot: dict[str, Any],
) -> dict[tuple[str, str, str, str], float]:
    probabilities: dict[tuple[str, str, str, str], float] = {}
    for row in snapshot.get("probabilities") or []:
        if not isinstance(row, list) or len(row) != 5:
            continue
        event_id, market_key, identity, outcome_name, probability = row
        probabilities[(str(event_id), str(market_key), str(identity), str(outcome_name))] = float(
            probability
        )
    return probabilities


def _baseline_snapshot(
    history: list[dict[str, Any]],
    *,
    now: datetime,
    config: TennisLeadLagConfig,
) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for snapshot in history:
        observed_at = _parse_time(str(snapshot.get("observed_at") or ""))
        if observed_at is None:
            continue
        age = (now - observed_at).total_seconds()
        if config.lookback_min_seconds <= age <= config.lookback_max_seconds:
            candidates.append((abs(age - config.preferred_lookback_seconds), snapshot))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _updated_history(
    history: list[dict[str, Any]],
    prices: list[OutcomePrice],
    *,
    now: datetime,
    retention_seconds: int,
) -> list[dict[str, Any]]:
    cutoff = now - timedelta(seconds=retention_seconds)
    retained = []
    for snapshot in history:
        observed_at = _parse_time(str(snapshot.get("observed_at") or ""))
        if observed_at is not None and observed_at >= cutoff and observed_at != now:
            retained.append(snapshot)
    retained.append(_snapshot(prices, observed_at=now))
    return sorted(retained, key=lambda item: str(item.get("observed_at") or ""))


def _load_history(table: Any, sport_key: str) -> list[dict[str, Any]]:
    response = table.get_item(Key={"trade_id": _state_id(sport_key)})
    item = response.get("Item") or {}
    payload = json.loads(str(item.get("state_json") or "[]"))
    return payload if isinstance(payload, list) else []


def _store_history(
    table: Any,
    sport_key: str,
    history: list[dict[str, Any]],
    *,
    now: datetime,
) -> None:
    table.put_item(
        Item={
            "trade_id": _state_id(sport_key),
            "status": "control",
            "control_type": TENNIS_LEAD_LAG_STRATEGY,
            "sport_key": sport_key,
            "updated_at": now.isoformat(),
            "state_json": json.dumps(history, separators=(",", ":")),
        }
    )


def _state_id(sport_key: str) -> str:
    digest = hashlib.sha256(sport_key.casefold().encode("utf-8")).hexdigest()[:20]
    return f"{_STATE_PREFIX}{digest}"


def _bookmaker_identity(bookmaker_key: str) -> str:
    identity = bookmaker_key.casefold()
    if identity == "betfair" or identity.startswith("betfair_ex_"):
        return "betfair"
    return identity


def _unique_signals(signals: list[ValueSignal]) -> list[ValueSignal]:
    unique: dict[tuple[str, str, str, str], ValueSignal] = {}
    for signal in signals:
        key = (
            signal.event_id.casefold(),
            signal.market_key.casefold(),
            signal.outcome_name.casefold(),
            signal.target_bookmaker.casefold(),
        )
        current = unique.get(key)
        if current is None or signal.edge > current.edge:
            unique[key] = signal
    return sorted(unique.values(), key=lambda signal: signal.edge, reverse=True)


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
