from __future__ import annotations

import csv
import json

from lambda_functions.betfair_enrichment import lambda_function


def test_lambda_marks_betfair_not_configured_without_credentials(monkeypatch) -> None:
    for name in (
        "BETFAIR_APP_KEY_DELAYED",
        "BETFAIR_APP_KEY",
        "BETFAIR_APP_KEY_LIVE",
        "BETFAIR_SESSION_TOKEN",
        "BETFAIR_USERNAME",
        "BETFAIR_PASSWORD",
        "BETFAIR_CERT_PEM",
        "BETFAIR_KEY_PEM",
    ):
        monkeypatch.delenv(name, raising=False)

    response = lambda_function.lambda_handler(
        {
            "csv": (
                "event_name,commence_time,market,outcome_name,target_bookmaker,target_odds\n"
                "Arsenal v Chelsea,2026-08-14T12:00:00+00:00,h2h,Chelsea,Betfair,5.1\n"
            )
        },
        None,
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    rows = list(csv.DictReader(body["csv"].splitlines()))
    assert body["betfair_configured"] is False
    assert rows[0]["liquidity_status"] == "betfair_not_configured"


def test_normalise_pem_preserves_multiline_value() -> None:
    pem = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"

    assert lambda_function._normalise_pem(pem) == pem


def test_normalise_pem_converts_literal_newlines() -> None:
    pem = "-----BEGIN CERTIFICATE-----\\nabc\\n-----END CERTIFICATE-----"

    assert (
        lambda_function._normalise_pem(pem)
        == "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"
    )


def test_normalise_pem_rebuilds_space_collapsed_value() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY----- abc def -----END RSA PRIVATE KEY-----"

    assert (
        lambda_function._normalise_pem(pem)
        == "-----BEGIN RSA PRIVATE KEY-----\nabcdef\n-----END RSA PRIVATE KEY-----\n"
    )
