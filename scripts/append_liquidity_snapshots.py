from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT_FIELDS = [
    "snapshot_time",
    "sport_key",
    "event_id",
    "event_name",
    "commence_time",
    "market",
    "outcome_name",
    "target_bookmaker",
    "target_odds",
    "target_effective_odds",
    "reference_fair_odds",
    "edge",
    "reference_bookmakers",
    "matchbook_event_id",
    "matchbook_market_id",
    "matchbook_runner_id",
    "best_back_odds",
    "best_back_available",
    "available_at_or_above_target",
    "best_lay_odds",
    "best_lay_available",
    "back_lay_spread_pct",
    "liquidity_status",
]


def main() -> None:
    args = parse_args()
    rows = read_csv(args.input_csv)
    append_snapshots(
        input_rows=rows,
        output_csv=args.output_csv,
        snapshot_time=args.snapshot_time or datetime.now(timezone.utc).isoformat(),
    )
    print(f"Appended {len(rows)} liquidity snapshot rows to {args.output_csv}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append enriched Matchbook opportunity liquidity rows to a historical CSV."
    )
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--snapshot-time", default="")
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def append_snapshots(
    *,
    input_rows: list[dict[str, str]],
    output_csv: Path,
    snapshot_time: str,
) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    exists = output_csv.exists()
    with output_csv.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in input_rows:
            output = dict(row)
            output["snapshot_time"] = snapshot_time
            writer.writerow(output)


if __name__ == "__main__":
    main()
