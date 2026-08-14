from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from exchange_scanner.betfair_auth import certificate_login
from exchange_scanner.betfair_liquidity import (
    BetfairLiquidityClient,
    enrich_opportunities_csv,
)


def main() -> None:
    load_dotenv()
    args = parse_args()
    app_key = (
        os.getenv("BETFAIR_APP_KEY_DELAYED")
        or os.getenv("BETFAIR_APP_KEY")
        or os.getenv("BETFAIR_APP_KEY_LIVE")
        or ""
    )
    session_token = _session_token(app_key)
    client = (
        BetfairLiquidityClient(app_key=app_key, session_token=session_token)
        if app_key and session_token
        else None
    )
    enrich_opportunities_csv(
        opportunities_csv=args.input_csv,
        output_csv=args.output_csv,
        client=client,
    )
    if client is None:
        print(
            "Wrote "
            f"{args.output_csv}; Betfair liquidity marked betfair_not_configured "
            "because BETFAIR_APP_KEY/BETFAIR_SESSION_TOKEN were not set."
        )
    else:
        print(f"Wrote {args.output_csv}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append Betfair Exchange delayed-API liquidity to Betfair opportunity CSVs."
    )
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    return parser.parse_args()


def _session_token(app_key: str) -> str:
    username = os.getenv("BETFAIR_USERNAME", "")
    password = os.getenv("BETFAIR_PASSWORD", "")
    cert_file = os.getenv("BETFAIR_CERT_FILE", "")
    key_file = os.getenv("BETFAIR_KEY_FILE", "")
    if app_key and username and password and cert_file and key_file:
        return certificate_login(
            username=username,
            password=password,
            app_key=app_key,
            cert_file=Path(cert_file),
            key_file=Path(key_file),
        )
    return os.getenv("BETFAIR_SESSION_TOKEN", "")


if __name__ == "__main__":
    main()
