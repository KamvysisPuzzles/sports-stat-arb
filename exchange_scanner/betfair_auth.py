from __future__ import annotations

from pathlib import Path

import httpx

BETFAIR_CERT_LOGIN_URL = "https://identitysso-cert.betfair.com/api/certlogin"


def certificate_login(
    *,
    username: str,
    password: str,
    app_key: str,
    cert_file: Path,
    key_file: Path,
    timeout: float = 20.0,
) -> str:
    with httpx.Client(cert=(str(cert_file), str(key_file)), timeout=timeout) as client:
        response = client.post(
            BETFAIR_CERT_LOGIN_URL,
            data={
                "username": username,
                "password": password,
            },
            headers={
                "X-Application": app_key,
                "Accept": "application/json",
            },
        )
    response.raise_for_status()
    payload = response.json()
    if payload.get("loginStatus") != "SUCCESS" or not payload.get("sessionToken"):
        status = payload.get("loginStatus") or "UNKNOWN"
        raise RuntimeError(f"Betfair certificate login failed: {status}")
    return str(payload["sessionToken"])
