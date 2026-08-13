from __future__ import annotations

import argparse
from pathlib import Path

from exchange_scanner.matchbook_paper import log_enriched_opportunities


def main() -> None:
    args = parse_args()
    inserted = log_enriched_opportunities(
        paper_db=args.paper_db,
        opportunities_csv=args.opportunities_csv,
        min_liquidity=args.min_liquidity,
    )
    print(f"Logged {inserted} executable Matchbook paper trades.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Log enriched Matchbook opportunities using visible liquidity as paper stake."
    )
    parser.add_argument("--paper-db", type=Path, required=True)
    parser.add_argument("--opportunities-csv", type=Path, required=True)
    parser.add_argument("--min-liquidity", type=float, default=0.01)
    return parser.parse_args()


if __name__ == "__main__":
    main()
