from __future__ import annotations

import argparse
from pathlib import Path

from exchange_scanner.odds_parquet import export_latest_snapshot_parquet


def main() -> None:
    args = parse_args()
    try:
        output_path, s3_key, snapshot_time, row_count = export_latest_snapshot_parquet(
            args.market_db,
            args.output_dir,
            s3_prefix=args.s3_prefix,
        )
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Exported {row_count} odds rows to {output_path}")
    print(f"S3 key: {s3_key}")
    if args.github_output:
        args.github_output.write_text(
            f"local_path={output_path}\n"
            f"s3_key={s3_key}\n"
            f"snapshot_time={snapshot_time.isoformat()}\n",
            encoding="utf-8",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the latest stored odds snapshot to a compressed Parquet file."
    )
    parser.add_argument("--market-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--s3-prefix", default="odds_snapshots")
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    main()
