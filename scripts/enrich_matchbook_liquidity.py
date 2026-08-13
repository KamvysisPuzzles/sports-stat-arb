from __future__ import annotations

import argparse
from pathlib import Path

from exchange_scanner.matchbook_liquidity import (
    MatchbookLiquidityClient,
    enrich_opportunities_csv,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append Matchbook order-book liquidity to Matchbook opportunity CSVs."
    )
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--currency", default="GBP")
    parser.add_argument("--minimum-liquidity", type=float, default=2)
    args = parser.parse_args()

    client = MatchbookLiquidityClient()
    events = client.fetch_events(
        currency=args.currency,
        minimum_liquidity=args.minimum_liquidity,
    )
    enrich_opportunities_csv(
        opportunities_csv=args.input_csv,
        output_csv=args.output_csv,
        events=events,
    )
    print(f"Wrote {args.output_csv}")


if __name__ == "__main__":
    main()
