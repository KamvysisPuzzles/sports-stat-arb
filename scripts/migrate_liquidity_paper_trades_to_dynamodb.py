from __future__ import annotations

import argparse
import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from exchange_scanner.dynamodb_paper import trade_id
from exchange_scanner.the_odds_api import ValueSignal


def main() -> None:
    args = parse_args()
    rows = read_csv(args.paper_csv)
    selected = [
        row
        for row in rows
        if row.get("liquidity_status") == "available"
        and (not args.status or row.get("status") == args.status)
    ]
    items = [item_from_row(row) for row in selected]
    if args.dry_run:
        print(
            f"Dry run: would migrate {len(items)} liquidity-confirmed trades "
            f"from {len(rows)} paper rows."
        )
        return

    table = dynamodb_table(args.table, region=args.region)
    inserted = 0
    duplicates = 0
    for item in items:
        try:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(trade_id)")
            inserted += 1
        except Exception as exc:
            if is_conditional_check_failed(exc):
                duplicates += 1
                continue
            raise
    print(
        f"Migrated liquidity-confirmed trades: read={len(rows)} "
        f"selected={len(items)} inserted={inserted} duplicates={duplicates}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate liquidity-confirmed paper trades from CSV to DynamoDB."
    )
    parser.add_argument("--paper-csv", type=Path, default=Path("data/paper_trades.csv"))
    parser.add_argument("--table", default="sports-stat-arb-paper-trades")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument(
        "--status",
        choices=["open", "settled"],
        help="Optionally migrate only one trade status.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def item_from_row(row: dict[str, str]) -> dict[str, Any]:
    signal = signal_from_row(row)
    item: dict[str, Any] = {
        "trade_id": trade_id(signal),
        "legacy_id": maybe_decimal(row.get("id")),
        "logged_at": row["logged_at"],
        "sport_key": row["sport_key"],
        "event_id": row["event_id"],
        "event_name": row["event_name"],
        "commence_time": row["commence_time"],
        "market": row.get("market") or row.get("market_key", "h2h"),
        "outcome_name": row["outcome_name"],
        "target_bookmaker": row["target_bookmaker"],
        "target_odds": decimal(row["target_odds"]),
        "target_effective_odds": maybe_decimal(row.get("target_effective_odds")),
        "reference_fair_odds": decimal(row["reference_fair_odds"]),
        "reference_probability": maybe_decimal(row.get("reference_probability")),
        "edge": decimal(row["edge"]),
        "reference_bookmakers_text": row.get("reference_bookmakers", ""),
        "reference_bookmakers": [
            value.strip()
            for value in row.get("reference_bookmakers", "").split(",")
            if value.strip()
        ],
        "stake": decimal(row["stake"]),
        "status": row.get("status") or "open",
        "execution_mode": "paper",
        "migrated_from": "data/paper_trades.csv",
    }
    for field in [
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
        "closing_checked_at",
        "closing_target_odds",
        "target_clv",
        "beat_closing_line",
        "closing_reference_fair_odds",
        "closing_edge",
        "positive_closing_edge",
        "result",
        "profit",
    ]:
        value = row.get(field)
        if value in {None, ""}:
            continue
        item[field] = maybe_decimal(value)
    return {key: value for key, value in item.items() if value is not None}


def signal_from_row(row: dict[str, str]) -> ValueSignal:
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
        target_effective_odds=optional_float(row.get("target_effective_odds")),
        reference_fair_odds=reference_fair_odds,
        reference_probability=float(row.get("reference_probability") or 1 / reference_fair_odds),
        edge=float(row["edge"]),
        reference_bookmakers=tuple(
            item.strip() for item in row.get("reference_bookmakers", "").split(",") if item.strip()
        ),
    )


def dynamodb_table(name: str, *, region: str):
    import boto3

    return boto3.resource("dynamodb", region_name=region).Table(name)


def is_conditional_check_failed(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    return exc.__class__.__name__ == "ConditionalCheckFailedException"


def maybe_decimal(value: str | None) -> str | Decimal | None:
    if value in {None, ""}:
        return None
    lowered = str(value).casefold()
    if lowered == "true":
        return "true"
    if lowered == "false":
        return "false"
    try:
        return decimal(str(value))
    except ValueError:
        return str(value)


def decimal(value: str) -> Decimal:
    return Decimal(str(float(value)))


def optional_float(value: str | None) -> float | None:
    return float(value) if value else None


if __name__ == "__main__":
    main()
