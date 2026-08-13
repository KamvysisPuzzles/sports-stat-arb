from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path

from exchange_scanner.paper import log_signals
from exchange_scanner.the_odds_api import ValueSignal

LIQUIDITY_FIELDS = [
    "matchbook_event_id",
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


def main() -> None:
    args = parse_args()
    rows = read_csv(args.opportunities_csv)
    signals = [_signal_from_row(row) for row in rows]
    liquidity_by_key = {
        _row_key(row): {field: row.get(field, "") for field in LIQUIDITY_FIELDS}
        for row in rows
    }
    inserted = log_signals(
        args.paper_db,
        signals,
        stake=args.paper_stake,
        liquidity_by_key=liquidity_by_key,
    )
    if args.inserted_count_out:
        args.inserted_count_out.write_text(f"{inserted}\n", encoding="utf-8")
    print(f"Logged {inserted} exchange paper trades.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Log exchange CLV opportunities with optional Matchbook liquidity fields."
    )
    parser.add_argument("--paper-db", type=Path, required=True)
    parser.add_argument("--opportunities-csv", type=Path, required=True)
    parser.add_argument("--paper-stake", type=float, default=1.0)
    parser.add_argument("--inserted-count-out", type=Path)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _signal_from_row(row: dict[str, str]) -> ValueSignal:
    reference_fair_odds = float(row["reference_fair_odds"])
    return ValueSignal(
        sport_key=row["sport_key"],
        event_id=row["event_id"],
        event_name=row["event_name"],
        commence_time=datetime.fromisoformat(row["commence_time"]),
        market_key=row.get("market") or row.get("market_key", "h2h"),
        outcome_name=row["outcome_name"],
        target_bookmaker=row["target_bookmaker"],
        target_odds=float(row["target_odds"]),
        target_effective_odds=_optional_float(row.get("target_effective_odds")),
        reference_fair_odds=reference_fair_odds,
        reference_probability=float(row.get("reference_probability") or 1 / reference_fair_odds),
        edge=float(row["edge"]),
        reference_bookmakers=tuple(
            item.strip() for item in row.get("reference_bookmakers", "").split(",") if item.strip()
        ),
    )


def _row_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        row["event_id"].casefold(),
        (row.get("market") or row.get("market_key", "h2h")).casefold(),
        row["outcome_name"].casefold(),
        row["target_bookmaker"].casefold(),
    )


def _optional_float(value: str | None) -> float | None:
    return float(value) if value else None


if __name__ == "__main__":
    main()
