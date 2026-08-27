from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from exchange_scanner.the_odds_api import OutcomePrice


@dataclass(frozen=True)
class SharpnessWeight:
    bookmaker_identity: str
    bookmaker_title: str
    sport_key: str
    market_key: str
    sample_count: int
    avg_abs_error: float
    avg_squared_error: float
    weight: float
    updated_at: datetime


def init_market_db(path: Path) -> None:
    with _connect(path) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS odds_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_time TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                market_key TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                bookmaker_identity TEXT NOT NULL,
                outcome_name TEXT NOT NULL,
                point_key TEXT NOT NULL,
                odds REAL NOT NULL,
                exchange_lay_odds REAL,
                exchange_spread_pct REAL,
                last_update TEXT NOT NULL,
                UNIQUE (
                    snapshot_time,
                    event_id,
                    market_key,
                    bookmaker_key,
                    outcome_name,
                    point_key
                )
            )
            """
        )
        _ensure_column(db, "odds_snapshots", "exchange_lay_odds", "REAL")
        _ensure_column(db, "odds_snapshots", "exchange_spread_pct", "REAL")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS sharpness_weights (
                bookmaker_identity TEXT NOT NULL,
                bookmaker_title TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                market_key TEXT NOT NULL,
                sample_count INTEGER NOT NULL,
                avg_abs_error REAL NOT NULL,
                avg_squared_error REAL NOT NULL,
                weight REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (bookmaker_identity, sport_key, market_key)
            )
            """
        )


def store_odds_snapshot(
    path: Path,
    prices: list[OutcomePrice],
    *,
    snapshot_time: datetime | None = None,
) -> int:
    init_market_db(path)
    snapshot_time = snapshot_time or datetime.now(timezone.utc)
    inserted = 0
    with _connect(path) as db:
        for price in prices:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO odds_snapshots (
                    snapshot_time,
                    sport_key,
                    event_id,
                    event_name,
                    commence_time,
                    market_key,
                    bookmaker_key,
                    bookmaker_title,
                    bookmaker_identity,
                    outcome_name,
                    point_key,
                    odds,
                    exchange_lay_odds,
                    exchange_spread_pct,
                    last_update
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_time.isoformat(),
                    price.sport_key,
                    price.event_id,
                    price.event_name,
                    price.commence_time.isoformat(),
                    price.market_key,
                    price.bookmaker_key,
                    price.bookmaker_title,
                    _bookmaker_identity(price.bookmaker_key, price.bookmaker_title),
                    price.comparable_outcome_name,
                    _point_key(price.point),
                    price.odds,
                    price.exchange_lay_odds,
                    price.exchange_spread_pct,
                    price.last_update.isoformat(),
                ),
            )
            inserted += cursor.rowcount
    return inserted


def recompute_sharpness_weights(
    path: Path,
    *,
    benchmark_bookmakers: set[str],
    min_samples: int = 25,
    now: datetime | None = None,
) -> list[SharpnessWeight]:
    init_market_db(path)
    now = now or datetime.now(timezone.utc)
    with _connect(path) as db:
        rows = db.execute(
            """
            SELECT *
            FROM odds_snapshots
            WHERE commence_time <= ?
            ORDER BY event_id, market_key, snapshot_time
            """,
            (now.isoformat(),),
        ).fetchall()

    by_event_market: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_event_market[(row["event_id"], row["market_key"])].append(row)

    errors: dict[tuple[str, str, str], list[tuple[str, float]]] = defaultdict(list)
    for event_rows in by_event_market.values():
        closing_time = _closing_snapshot_time(event_rows)
        if closing_time is None:
            continue
        closing_rows = [row for row in event_rows if row["snapshot_time"] == closing_time]
        closing_probs = _consensus_probabilities(
            closing_rows,
            bookmaker_filter=benchmark_bookmakers,
        )
        if not closing_probs:
            continue

        for snapshot_time in sorted({row["snapshot_time"] for row in event_rows}):
            if snapshot_time > closing_time:
                continue
            snapshot_rows = [row for row in event_rows if row["snapshot_time"] == snapshot_time]
            for bookmaker_rows in _rows_by_bookmaker(snapshot_rows).values():
                probabilities = _bookmaker_probabilities(bookmaker_rows)
                if not probabilities:
                    continue
                first = bookmaker_rows[0]
                key = (
                    first["bookmaker_identity"],
                    first["sport_key"],
                    first["market_key"],
                )
                for outcome_name, probability in probabilities.items():
                    closing_probability = closing_probs.get(outcome_name)
                    if closing_probability is None:
                        continue
                    errors[key].append(
                        (first["bookmaker_title"], abs(probability - closing_probability))
                    )

    weights = _weights_from_errors(errors, min_samples=min_samples, updated_at=now)
    _replace_sharpness_weights(path, weights)
    return weights


def list_sharpness_weights(path: Path) -> list[SharpnessWeight]:
    init_market_db(path)
    with _connect(path) as db:
        rows = db.execute(
            """
            SELECT *
            FROM sharpness_weights
            ORDER BY sport_key, market_key, weight DESC, bookmaker_title
            """
        ).fetchall()
    return [_weight_from_row(row) for row in rows]


def sharpness_weight_mapping(
    path: Path,
    *,
    sport_keys: set[str] | None = None,
    market_keys: set[str] | None = None,
) -> dict[str, float]:
    weights = list_sharpness_weights(path)
    grouped: dict[str, list[float]] = defaultdict(list)
    for weight in weights:
        if sport_keys is not None and weight.sport_key not in sport_keys:
            continue
        if market_keys is not None and weight.market_key not in market_keys:
            continue
        grouped[weight.bookmaker_identity].append(weight.weight)
    return {
        bookmaker_identity: sum(values) / len(values)
        for bookmaker_identity, values in grouped.items()
        if values
    }


def write_sharpness_weights_csv(weights: list[SharpnessWeight], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "bookmaker_identity",
                "bookmaker_title",
                "sport_key",
                "market_key",
                "sample_count",
                "avg_abs_error",
                "avg_squared_error",
                "weight",
                "updated_at",
            ],
        )
        writer.writeheader()
        for weight in weights:
            writer.writerow(
                {
                    "bookmaker_identity": weight.bookmaker_identity,
                    "bookmaker_title": weight.bookmaker_title,
                    "sport_key": weight.sport_key,
                    "market_key": weight.market_key,
                    "sample_count": weight.sample_count,
                    "avg_abs_error": f"{weight.avg_abs_error:.6f}",
                    "avg_squared_error": f"{weight.avg_squared_error:.6f}",
                    "weight": f"{weight.weight:.6f}",
                    "updated_at": weight.updated_at.isoformat(),
                }
            )


def _weights_from_errors(
    errors: dict[tuple[str, str, str], list[tuple[str, float]]],
    *,
    min_samples: int,
    updated_at: datetime,
) -> list[SharpnessWeight]:
    raw_weights: list[tuple[tuple[str, str, str], str, int, float, float, float]] = []
    for key, values in errors.items():
        if len(values) < min_samples:
            continue
        bookmaker_title = values[-1][0]
        absolute_errors = [value for _, value in values]
        avg_abs_error = sum(absolute_errors) / len(absolute_errors)
        avg_squared_error = sum(value * value for value in absolute_errors) / len(absolute_errors)
        raw_weight = 1 / (avg_abs_error + 0.01)
        raw_weights.append(
            (key, bookmaker_title, len(values), avg_abs_error, avg_squared_error, raw_weight)
        )

    max_weight = max((item[-1] for item in raw_weights), default=1)
    weights = []
    for key, bookmaker_title, sample_count, avg_abs_error, avg_squared_error, raw_weight in raw_weights:
        bookmaker_identity, sport_key, market_key = key
        weights.append(
            SharpnessWeight(
                bookmaker_identity=bookmaker_identity,
                bookmaker_title=bookmaker_title,
                sport_key=sport_key,
                market_key=market_key,
                sample_count=sample_count,
                avg_abs_error=avg_abs_error,
                avg_squared_error=avg_squared_error,
                weight=max(0.05, raw_weight / max_weight),
                updated_at=updated_at,
            )
        )
    return sorted(weights, key=lambda item: (item.sport_key, item.market_key, -item.weight))


def _replace_sharpness_weights(path: Path, weights: list[SharpnessWeight]) -> None:
    init_market_db(path)
    with _connect(path) as db:
        db.execute("DELETE FROM sharpness_weights")
        db.executemany(
            """
            INSERT INTO sharpness_weights (
                bookmaker_identity,
                bookmaker_title,
                sport_key,
                market_key,
                sample_count,
                avg_abs_error,
                avg_squared_error,
                weight,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    weight.bookmaker_identity,
                    weight.bookmaker_title,
                    weight.sport_key,
                    weight.market_key,
                    weight.sample_count,
                    weight.avg_abs_error,
                    weight.avg_squared_error,
                    weight.weight,
                    weight.updated_at.isoformat(),
                )
                for weight in weights
            ],
        )


def _closing_snapshot_time(rows: list[sqlite3.Row]) -> str | None:
    commence_time = rows[0]["commence_time"]
    candidates = [row["snapshot_time"] for row in rows if row["snapshot_time"] <= commence_time]
    return max(candidates, default=None)


def _consensus_probabilities(
    rows: list[sqlite3.Row],
    *,
    bookmaker_filter: set[str],
) -> dict[str, float]:
    by_outcome: dict[str, list[float]] = defaultdict(list)
    for bookmaker_rows in _rows_by_bookmaker(rows).values():
        first = bookmaker_rows[0]
        if first["bookmaker_identity"] not in bookmaker_filter:
            continue
        probabilities = _bookmaker_probabilities(bookmaker_rows)
        for outcome_name, probability in probabilities.items():
            by_outcome[outcome_name].append(probability)
    return {
        outcome_name: sum(probabilities) / len(probabilities)
        for outcome_name, probabilities in by_outcome.items()
        if probabilities
    }


def _bookmaker_probabilities(rows: list[sqlite3.Row]) -> dict[str, float]:
    expected_outcomes = max(
        len(grouped_rows)
        for grouped_rows in _rows_by_bookmaker(rows).values()
    )
    if len(rows) != expected_outcomes:
        return {}
    overround = sum(1 / row["odds"] for row in rows)
    if overround <= 0:
        return {}
    return {row["outcome_name"]: (1 / row["odds"]) / overround for row in rows}


def _rows_by_bookmaker(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[row["bookmaker_identity"]].append(row)
    return grouped


def _weight_from_row(row: sqlite3.Row) -> SharpnessWeight:
    return SharpnessWeight(
        bookmaker_identity=row["bookmaker_identity"],
        bookmaker_title=row["bookmaker_title"],
        sport_key=row["sport_key"],
        market_key=row["market_key"],
        sample_count=row["sample_count"],
        avg_abs_error=row["avg_abs_error"],
        avg_squared_error=row["avg_squared_error"],
        weight=row["weight"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _point_key(point: float | None) -> str:
    return "" if point is None else f"{point:g}"


def _bookmaker_identity(bookmaker_key: str, bookmaker_title: str) -> str:
    return bookmaker_title.casefold() or bookmaker_key


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def _ensure_column(db: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
