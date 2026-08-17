from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def export_latest_snapshot_parquet(
    market_db: Path,
    output_dir: Path,
    *,
    s3_prefix: str = "odds_snapshots",
) -> tuple[Path, str, datetime, int]:
    snapshot_time = latest_snapshot_time(market_db)
    if snapshot_time is None:
        raise ValueError(f"No odds snapshots found in {market_db}")
    rows = snapshot_rows(market_db, snapshot_time)
    if not rows:
        raise ValueError(f"No odds rows found for snapshot_time={snapshot_time}")

    parsed_snapshot_time = parse_time(snapshot_time)
    output_path = output_path_for_snapshot(
        output_dir,
        s3_prefix=s3_prefix,
        snapshot_time=parsed_snapshot_time,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_parquet(rows, output_path, snapshot_time=parsed_snapshot_time)

    s3_key = "/".join(output_path.relative_to(output_dir).parts)
    return output_path, s3_key, parsed_snapshot_time, len(rows)


def latest_snapshot_time(db_path: Path) -> str | None:
    with _connect(db_path) as db:
        row = db.execute("SELECT MAX(snapshot_time) AS snapshot_time FROM odds_snapshots").fetchone()
    return row["snapshot_time"] if row and row["snapshot_time"] else None


def snapshot_rows(db_path: Path, snapshot_time: str) -> list[dict[str, Any]]:
    with _connect(db_path) as db:
        rows = db.execute(
            """
            SELECT
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
                last_update
            FROM odds_snapshots
            WHERE snapshot_time = ?
            ORDER BY sport_key, event_id, market_key, bookmaker_key, outcome_name, point_key
            """,
            (snapshot_time,),
        ).fetchall()
    return [parquet_row(row) for row in rows]


def parquet_row(row: sqlite3.Row) -> dict[str, Any]:
    snapshot_time = parse_time(row["snapshot_time"])
    commence_time = parse_time(row["commence_time"])
    odds = float(row["odds"])
    return {
        "snapshot_time": snapshot_time,
        "sport_key": row["sport_key"],
        "event_id": row["event_id"],
        "event_name": row["event_name"],
        "commence_time": commence_time,
        "market": row["market_key"],
        "outcome_name": row["outcome_name"],
        "point": point_value(row["point_key"]),
        "bookmaker_key": row["bookmaker_key"],
        "bookmaker_title": row["bookmaker_title"],
        "bookmaker_identity": row["bookmaker_identity"],
        "odds": odds,
        "implied_probability": 1 / odds if odds else None,
        "last_update": parse_time(row["last_update"]),
        "days_to_start": (commence_time - snapshot_time).total_seconds() / 86400,
    }


def write_parquet(
    rows: list[dict[str, Any]],
    output_path: Path,
    *,
    snapshot_time: datetime,
) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            f"Missing or unloadable pyarrow. Install/package it with: pip install pyarrow. "
            f"Import error: {exc}"
        ) from exc

    table = pa.Table.from_pylist(rows)
    metadata = {
        b"snapshot_time": snapshot_time.isoformat().encode("utf-8"),
        b"source": b"the-odds-api",
    }
    table = table.replace_schema_metadata(metadata)
    pq.write_table(table, output_path, compression="zstd")


def output_path_for_snapshot(output_dir: Path, *, s3_prefix: str, snapshot_time: datetime) -> Path:
    date_part = snapshot_time.date().isoformat()
    hour_part = f"{snapshot_time.hour:02d}"
    timestamp_part = snapshot_time.strftime("%Y%m%dT%H%M%SZ")
    return (
        output_dir
        / s3_prefix.strip("/")
        / f"snapshot_date={date_part}"
        / f"hour={hour_part}"
        / f"odds_{timestamp_part}.parquet"
    )


def point_value(value: str) -> float | None:
    return float(value) if value else None


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db
