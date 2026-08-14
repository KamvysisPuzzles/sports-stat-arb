from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from exchange_scanner.betfair_auth import certificate_login

BETFAIR_KEEP_ALIVE_URL = "https://identitysso.betfair.com/api/keepAlive"


def main() -> None:
    load_dotenv()
    app_key = (
        os.getenv("BETFAIR_APP_KEY_DELAYED")
        or os.getenv("BETFAIR_APP_KEY")
        or os.getenv("BETFAIR_APP_KEY_LIVE")
        or ""
    )
    session_token = _session_token(app_key)
    if not app_key or not session_token:
        print("Betfair keepAlive skipped: app key or session token is not configured.")
        return

    response = httpx.post(
        BETFAIR_KEEP_ALIVE_URL,
        headers={
            "X-Application": app_key,
            "X-Authentication": session_token,
            "Accept": "application/json",
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    status = payload.get("status")
    if status != "SUCCESS":
        error = payload.get("error") or "unknown"
        print(f"Betfair keepAlive failed: {error}", file=sys.stderr)
        raise SystemExit(1)
    print("Betfair keepAlive succeeded.")


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
