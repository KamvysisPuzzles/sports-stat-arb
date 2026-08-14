from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

BETFAIR_KEEP_ALIVE_URL = "https://identitysso.betfair.com/api/keepAlive"


def main() -> None:
    load_dotenv()
    app_key = (
        os.getenv("BETFAIR_APP_KEY_DELAYED")
        or os.getenv("BETFAIR_APP_KEY")
        or os.getenv("BETFAIR_APP_KEY_LIVE")
        or ""
    )
    session_token = os.getenv("BETFAIR_SESSION_TOKEN", "")
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


if __name__ == "__main__":
    main()
