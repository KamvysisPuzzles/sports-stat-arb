from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def main() -> None:
    args = parse_args()
    source = args.cache_dir
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(
        path
        for path in source.glob(f"{args.sport_prefix}*.json")
        if path.name != "errors.json"
    )

    with sqlite3.connect(output) as db:
        init_db(db)
        imported_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        bad_files = 0
        for path in files:
            try:
                import_file(db, path, imported_at=imported_at)
            except (OSError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error):
                bad_files += 1
        summary = {
            "source": str(source),
            "sqlite": str(output),
            "files_seen": len(files),
            "snapshots_in_db": db.execute(
                "SELECT COUNT(*) FROM odds_api_snapshots"
            ).fetchone()[0],
            "events_in_db": db.execute(
                "SELECT COUNT(*) FROM odds_api_snapshot_events"
            ).fetchone()[0],
            "bad_files": bad_files,
            "size_mb": round(output.stat().st_size / 1024 / 1024, 2),
            "imported_at": imported_at,
        }

    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import raw The Odds API cache JSON files into a compressed SQLite archive."
    )
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--sport-prefix", default="", help="Optional filename prefix, e.g. soccer_.")
    return parser.parse_args()


def init_db(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS odds_api_snapshots (
          file_name TEXT PRIMARY KEY,
          sport_key TEXT NOT NULL,
          regions TEXT NOT NULL,
          markets TEXT NOT NULL,
          requested_time TEXT NOT NULL,
          actual_timestamp TEXT NOT NULL,
          previous_timestamp TEXT,
          next_timestamp TEXT,
          event_count INTEGER NOT NULL,
          bookmaker_count INTEGER NOT NULL,
          payload_gzip BLOB NOT NULL,
          imported_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS odds_api_snapshot_events (
          file_name TEXT NOT NULL,
          event_id TEXT NOT NULL,
          sport_key TEXT NOT NULL,
          commence_time TEXT NOT NULL,
          home_team TEXT,
          away_team TEXT,
          bookmaker_count INTEGER NOT NULL,
          PRIMARY KEY (file_name, event_id)
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_odds_api_snapshots_sport_time "
        "ON odds_api_snapshots (sport_key, actual_timestamp)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_odds_api_snapshots_requested "
        "ON odds_api_snapshots (requested_time)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_odds_api_snapshot_events_event "
        "ON odds_api_snapshot_events (event_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_odds_api_snapshot_events_commence "
        "ON odds_api_snapshot_events (commence_time)"
    )


def import_file(db: sqlite3.Connection, path: Path, *, imported_at: str) -> None:
    raw = path.read_bytes()
    payload = json.loads(raw)
    if "timestamp" not in payload or "data" not in payload:
        raise ValueError(f"{path} is not a historical odds snapshot")

    sport_key, regions, markets, requested_time = parts_from_name(path.name)
    events = payload.get("data") or []
    bookmaker_count = sum(len(event.get("bookmakers", []) or []) for event in events)
    db.execute(
        """
        INSERT OR REPLACE INTO odds_api_snapshots (
          file_name, sport_key, regions, markets, requested_time, actual_timestamp,
          previous_timestamp, next_timestamp, event_count, bookmaker_count,
          payload_gzip, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            path.name,
            sport_key,
            regions,
            markets,
            requested_time,
            payload["timestamp"],
            payload.get("previous_timestamp"),
            payload.get("next_timestamp"),
            len(events),
            bookmaker_count,
            gzip.compress(raw),
            imported_at,
        ),
    )
    db.execute("DELETE FROM odds_api_snapshot_events WHERE file_name = ?", (path.name,))
    db.executemany(
        """
        INSERT OR REPLACE INTO odds_api_snapshot_events (
          file_name, event_id, sport_key, commence_time, home_team, away_team, bookmaker_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                path.name,
                event.get("id", ""),
                event.get("sport_key", sport_key),
                event.get("commence_time", ""),
                event.get("home_team"),
                event.get("away_team"),
                len(event.get("bookmakers", []) or []),
            )
            for event in events
            if event.get("id")
        ],
    )


def parts_from_name(name: str) -> tuple[str, str, str, str]:
    return name.removesuffix(".json").rsplit("__", 3)


if __name__ == "__main__":
    main()
