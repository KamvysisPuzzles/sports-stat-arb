from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from exchange_scanner.the_odds_api import (
    ValueSignal,
    find_value_opportunities,
    normalise_odds_api_events,
)


@dataclass(frozen=True)
class HistoricalSnapshot:
    fetched_at: datetime
    events: list[dict[str, Any]]


@dataclass(frozen=True)
class BacktestBet:
    signal: ValueSignal
    snapshot_time: datetime
    stake: float
    result: str
    closing_snapshot_time: datetime | None = None
    closing_target_odds: float | None = None
    closing_reference_fair_odds: float | None = None

    @property
    def won(self) -> bool:
        return self.result.casefold() == self.signal.outcome_name.casefold()

    @property
    def profit(self) -> float:
        if self.won:
            return self.stake * (self.signal.target_odds - 1)
        return -self.stake

    @property
    def target_clv(self) -> float | None:
        if not self.closing_target_odds:
            return None
        return (self.signal.target_odds / self.closing_target_odds) - 1

    @property
    def closing_fair_edge(self) -> float | None:
        if not self.closing_reference_fair_odds:
            return None
        return (self.signal.target_odds / self.closing_reference_fair_odds) - 1


def run_backtest(
    *,
    historical_odds_path: Path,
    results_path: Path,
    target_bookmakers: set[str],
    reference_bookmakers: set[str],
    markets: set[str],
    min_edge: float,
    max_age_seconds: int,
    min_reference_books: int,
    include_started: bool,
    max_event_days: float,
    unique_events: bool,
    stake: float,
    daily_decision_time: str | None = "22:00",
    allow_rebet_same_event: bool = False,
    allow_target_bookmakers_as_references: bool = False,
    reference_weights: dict[str, float] | None = None,
) -> list[BacktestBet]:
    results = load_results(results_path)
    snapshots = load_historical_snapshots(historical_odds_path)
    decision_snapshots = (
        _daily_decision_snapshots(snapshots, daily_decision_time)
        if daily_decision_time
        else snapshots
    )
    bets: list[BacktestBet] = []
    seen_bets: set[tuple[datetime, str, str, str, str]] = set()
    seen_events: set[tuple[str, str]] = set()

    for snapshot in decision_snapshots:
        prices = normalise_odds_api_events(snapshot.events)
        prices = [price for price in prices if price.market_key in markets]
        prices = [
            price
            for price in prices
            if max_event_days < 0
            or price.commence_time <= snapshot.fetched_at + _days_as_timedelta(max_event_days)
        ]
        signals = find_value_opportunities(
            prices,
            target_bookmakers=target_bookmakers,
            reference_bookmakers=reference_bookmakers,
            min_edge=min_edge,
            max_age_seconds=max_age_seconds,
            min_reference_books=min_reference_books,
            include_started=include_started,
            allow_target_bookmakers_as_references=allow_target_bookmakers_as_references,
            reference_weights=reference_weights,
            now=snapshot.fetched_at,
        )
        if unique_events:
            signals = _unique_event_signals(signals)

        for signal in signals:
            result = _result_for_signal(signal, results)
            if result is None:
                continue
            event_key = _result_key(signal.event_id, signal.market_key)
            if not allow_rebet_same_event and event_key in seen_events:
                continue
            dedupe_key = (
                snapshot.fetched_at,
                signal.sport_key,
                signal.event_name,
                signal.market_key,
                signal.outcome_name,
            )
            if dedupe_key in seen_bets:
                continue
            seen_bets.add(dedupe_key)
            seen_events.add(event_key)
            closing_line = _closing_line_for_signal(
                signal,
                snapshots=snapshots,
                target_bookmakers=target_bookmakers,
                reference_bookmakers=reference_bookmakers,
                max_age_seconds=max_age_seconds,
                min_reference_books=min_reference_books,
                allow_target_bookmakers_as_references=allow_target_bookmakers_as_references,
                reference_weights=reference_weights,
            )
            bets.append(
                BacktestBet(
                    signal=signal,
                    snapshot_time=snapshot.fetched_at,
                    stake=stake,
                    result=result,
                    closing_snapshot_time=closing_line.snapshot_time,
                    closing_target_odds=closing_line.target_odds,
                    closing_reference_fair_odds=closing_line.reference_fair_odds,
                )
            )

    return bets


def load_historical_snapshots(path: Path) -> list[HistoricalSnapshot]:
    files = sorted([*path.glob("*.json"), *path.glob("*.jsonl")]) if path.is_dir() else [path]
    snapshots: list[HistoricalSnapshot] = []
    for file_path in files:
        if file_path.suffix == ".jsonl":
            snapshots.extend(_load_jsonl_snapshots(file_path))
            continue
        snapshots.extend(_snapshots_from_payload(json.loads(file_path.read_text()), file_path))
    return sorted(snapshots, key=lambda snapshot: snapshot.fetched_at)


def load_results(path: Path) -> dict[tuple[str, str], str]:
    if path.suffix == ".json":
        return _load_json_results(path)
    return _load_csv_results(path)


def backtest_summary(bets: list[BacktestBet]) -> dict[str, float | int]:
    staked = sum(bet.stake for bet in bets)
    profit = sum(bet.profit for bet in bets)
    wins = sum(1 for bet in bets if bet.won)
    clv_bets = [bet for bet in bets if bet.target_clv is not None]
    fair_edge_bets = [bet for bet in bets if bet.closing_fair_edge is not None]
    positive_clv = sum(1 for bet in clv_bets if bet.target_clv and bet.target_clv > 0)
    positive_fair_edge = sum(
        1 for bet in fair_edge_bets if bet.closing_fair_edge and bet.closing_fair_edge > 0
    )
    return {
        "bets": len(bets),
        "wins": wins,
        "losses": len(bets) - wins,
        "staked": staked,
        "profit": profit,
        "roi": profit / staked if staked else 0.0,
        "win_rate": wins / len(bets) if bets else 0.0,
        "clv_bets": len(clv_bets),
        "beat_closing_line": positive_clv,
        "beat_closing_line_rate": positive_clv / len(clv_bets) if clv_bets else 0.0,
        "average_target_clv": (
            sum(bet.target_clv or 0.0 for bet in clv_bets) / len(clv_bets)
            if clv_bets
            else 0.0
        ),
        "closing_fair_edge_bets": len(fair_edge_bets),
        "positive_closing_fair_edge": positive_fair_edge,
        "positive_closing_fair_edge_rate": (
            positive_fair_edge / len(fair_edge_bets) if fair_edge_bets else 0.0
        ),
        "average_closing_fair_edge": (
            sum(bet.closing_fair_edge or 0.0 for bet in fair_edge_bets) / len(fair_edge_bets)
            if fair_edge_bets
            else 0.0
        ),
    }


def _load_jsonl_snapshots(path: Path) -> list[HistoricalSnapshot]:
    snapshots = []
    for line in path.read_text().splitlines():
        if line.strip():
            snapshots.extend(_snapshots_from_payload(json.loads(line), path))
    return snapshots


def _snapshots_from_payload(payload: Any, source_path: Path) -> list[HistoricalSnapshot]:
    if isinstance(payload, dict) and "snapshots" in payload:
        snapshots = []
        for item in payload["snapshots"]:
            snapshots.extend(_snapshots_from_payload(item, source_path))
        return snapshots

    if isinstance(payload, dict) and "payload" in payload:
        events = payload["payload"]
        fetched_at = _parse_snapshot_time(payload.get("fetched_at"), events)
        return [HistoricalSnapshot(fetched_at=fetched_at, events=events)]

    if isinstance(payload, list):
        return [HistoricalSnapshot(fetched_at=_parse_snapshot_time(None, payload), events=payload)]

    raise ValueError(f"Unsupported historical odds payload in {source_path}")


def _load_json_results(path: Path) -> dict[tuple[str, str], str]:
    payload = json.loads(path.read_text())
    rows = payload["results"] if isinstance(payload, dict) and "results" in payload else payload
    results: dict[tuple[str, str], str] = {}
    for row in rows:
        results[_result_key(row["event_id"], row.get("market", row.get("market_key", "h2h")))] = row[
            "winner"
        ]
    return results


def _load_csv_results(path: Path) -> dict[tuple[str, str], str]:
    results: dict[tuple[str, str], str] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            results[
                _result_key(
                    row["event_id"],
                    row.get("market") or row.get("market_key") or "h2h",
                )
            ] = row["winner"]
    return results


def _result_for_signal(
    signal: ValueSignal,
    results: dict[tuple[str, str], str],
) -> str | None:
    return results.get(_result_key(signal.event_id, signal.market_key))


def _result_key(event_id_or_name: str, market_key: str) -> tuple[str, str]:
    return (event_id_or_name.casefold().strip(), market_key.casefold().strip())


def _parse_snapshot_time(value: str | None, events: list[dict[str, Any]]) -> datetime:
    if value:
        return _parse_time(value)
    latest_update = max(
        (
            _parse_time(bookmaker["last_update"])
            for event in events
            for bookmaker in event.get("bookmakers", [])
            if bookmaker.get("last_update")
        ),
        default=None,
    )
    if latest_update is None:
        return datetime.now(timezone.utc)
    return latest_update


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _days_as_timedelta(days: float):
    return timedelta(days=days)


def _unique_event_signals(signals: list[ValueSignal]) -> list[ValueSignal]:
    best_by_event: dict[tuple[str, str, datetime], ValueSignal] = {}
    for signal in signals:
        key = (signal.sport_key, signal.event_name, signal.commence_time)
        if key not in best_by_event:
            best_by_event[key] = signal
    return list(best_by_event.values())


@dataclass(frozen=True)
class _ClosingLine:
    snapshot_time: datetime | None = None
    target_odds: float | None = None
    reference_fair_odds: float | None = None


def _closing_line_for_signal(
    signal: ValueSignal,
    *,
    snapshots: list[HistoricalSnapshot],
    target_bookmakers: set[str],
    reference_bookmakers: set[str],
    max_age_seconds: int,
    min_reference_books: int,
    allow_target_bookmakers_as_references: bool = False,
    reference_weights: dict[str, float] | None = None,
) -> _ClosingLine:
    candidate_snapshots = [
        snapshot
        for snapshot in snapshots
        if snapshot.fetched_at <= signal.commence_time
        and any(event.get("id") == signal.event_id for event in snapshot.events)
    ]
    for snapshot in sorted(candidate_snapshots, key=lambda item: item.fetched_at, reverse=True):
        prices = normalise_odds_api_events(
            [event for event in snapshot.events if event.get("id") == signal.event_id]
        )
        closing_signals = find_value_opportunities(
            prices,
            target_bookmakers=target_bookmakers,
            reference_bookmakers=reference_bookmakers,
            min_edge=-999,
            max_age_seconds=max_age_seconds,
            min_reference_books=min_reference_books,
            include_started=False,
            allow_target_bookmakers_as_references=allow_target_bookmakers_as_references,
            reference_weights=reference_weights,
            now=snapshot.fetched_at,
        )
        for closing_signal in closing_signals:
            if (
                closing_signal.market_key == signal.market_key
                and closing_signal.outcome_name == signal.outcome_name
                and closing_signal.target_bookmaker.casefold() == signal.target_bookmaker.casefold()
            ):
                return _ClosingLine(
                    snapshot_time=snapshot.fetched_at,
                    target_odds=closing_signal.target_odds,
                    reference_fair_odds=closing_signal.reference_fair_odds,
                )

    return _ClosingLine()


def _daily_decision_snapshots(
    snapshots: list[HistoricalSnapshot],
    decision_time: str,
) -> list[HistoricalSnapshot]:
    hour, minute = (int(part) for part in decision_time.split(":", maxsplit=1))
    by_day: dict[datetime.date, HistoricalSnapshot] = {}
    for snapshot in snapshots:
        cutoff = snapshot.fetched_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if snapshot.fetched_at > cutoff:
            continue
        day = snapshot.fetched_at.date()
        existing = by_day.get(day)
        if existing is None or snapshot.fetched_at > existing.fetched_at:
            by_day[day] = snapshot
    return [by_day[day] for day in sorted(by_day)]
