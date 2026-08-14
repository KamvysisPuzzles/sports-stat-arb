from __future__ import annotations

import json
import os
import re
import textwrap
from pathlib import Path

from exchange_scanner.betfair_auth import certificate_login
from exchange_scanner.betfair_liquidity import BetfairLiquidityClient, enrich_opportunities_csv


def lambda_handler(event, context):
    csv_payload = event.get("csv") if isinstance(event, dict) else None
    if not csv_payload:
        return _response(400, {"error": "missing csv payload"})

    input_csv = Path("/tmp/betfair-input.csv")
    output_csv = Path("/tmp/betfair-output.csv")
    input_csv.write_text(csv_payload, encoding="utf-8")

    app_key = (
        os.getenv("BETFAIR_APP_KEY_DELAYED")
        or os.getenv("BETFAIR_APP_KEY")
        or os.getenv("BETFAIR_APP_KEY_LIVE")
        or ""
    )
    session_token, auth_error = _session_token(app_key)
    client = (
        BetfairLiquidityClient(app_key=app_key, session_token=session_token)
        if app_key and session_token
        else None
    )

    enrich_opportunities_csv(
        opportunities_csv=input_csv,
        output_csv=output_csv,
        client=client,
    )

    body = {
        "csv": output_csv.read_text(encoding="utf-8"),
        "betfair_configured": client is not None,
    }
    if auth_error:
        body["auth_error_type"] = auth_error
    return _response(200, body)


def _session_token(app_key: str) -> tuple[str, str | None]:
    username = os.getenv("BETFAIR_USERNAME", "")
    password = os.getenv("BETFAIR_PASSWORD", "")
    cert_pem = os.getenv("BETFAIR_CERT_PEM", "")
    key_pem = os.getenv("BETFAIR_KEY_PEM", "")
    if app_key and username and password and cert_pem and key_pem:
        cert_file = Path("/tmp/betfair-client.crt")
        key_file = Path("/tmp/betfair-client.key")
        cert_file.write_text(_normalise_pem(cert_pem), encoding="utf-8")
        key_file.write_text(_normalise_pem(key_pem), encoding="utf-8")
        try:
            return (
                certificate_login(
                    username=username,
                    password=password,
                    app_key=app_key,
                    cert_file=cert_file,
                    key_file=key_file,
                ),
                None,
            )
        except Exception as exc:
            return "", type(exc).__name__
    return os.getenv("BETFAIR_SESSION_TOKEN", ""), None


def _normalise_pem(value: str) -> str:
    value = value.strip().replace("\\n", "\n")
    if "\n" in value:
        return value + "\n"

    match = re.fullmatch(
        r"-----BEGIN ([^-]+)-----\s+(.+?)\s+-----END \1-----",
        value,
    )
    if not match:
        return value + "\n"

    label = match.group(1)
    body = re.sub(r"\s+", "", match.group(2))
    wrapped_body = "\n".join(textwrap.wrap(body, width=64))
    return f"-----BEGIN {label}-----\n{wrapped_body}\n-----END {label}-----\n"


def _response(status_code: int, body: dict[str, object]) -> dict[str, object]:
    return {
        "statusCode": status_code,
        "body": json.dumps(body),
    }
