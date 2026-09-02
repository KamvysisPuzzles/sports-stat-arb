from __future__ import annotations

import os
from urllib.parse import parse_qs

from exchange_scanner.portfolio_dashboard import (
    portfolio_json,
    portfolio_payload,
    render_portfolio_html,
)


def lambda_handler(event, context):
    params = _query_params(event)
    token = _token(event, params)
    expected_token = os.getenv("PORTFOLIO_DASHBOARD_TOKEN") or os.getenv("DASHBOARD_TOKEN", "")
    if not expected_token:
        return _response(
            500,
            "PORTFOLIO_DASHBOARD_TOKEN is not configured",
            content_type="text/plain",
        )
    if token != expected_token:
        return _response(401, "Unauthorized", content_type="text/plain")

    region = os.getenv("AWS_REGION", "eu-west-2")
    orders_table = _dynamodb_table(
        os.getenv("LIVE_ORDER_TABLE", "sports-stat-arb-live-orders"),
        region=region,
    )
    account_table_name = os.getenv("LIVE_ACCOUNT_STATE_TABLE", "")
    account_table = (
        _dynamodb_table(account_table_name, region=region) if account_table_name else None
    )
    payload = portfolio_payload(
        orders_table,
        account_table=account_table,
        filters={
            "venue": _first_param(params.get("venue", "")),
            "days": _first_param(params.get("days", "")),
        },
    )
    view = _first_param(params.get("view", "overview")).casefold()
    if _first_param(params.get("format", "")).casefold() == "json":
        return _response(200, portfolio_json(payload), content_type="application/json")
    return _response(
        200,
        render_portfolio_html(payload, view=view, token=token),
        content_type="text/html; charset=utf-8",
    )


def _query_params(event) -> dict[str, object]:
    if not isinstance(event, dict):
        return {}
    parsed: dict[str, object] = {}
    if event.get("rawQueryString"):
        parsed.update(
            {
                str(key): [str(item) for item in values if item]
                for key, values in parse_qs(
                    str(event["rawQueryString"]), keep_blank_values=False
                ).items()
            }
        )
    params = event.get("queryStringParameters") or {}
    for key, value in params.items():
        if value is None or key in parsed:
            continue
        parsed[str(key)] = str(value)
    for key, value in list(parsed.items()):
        if isinstance(value, list) and len(value) == 1:
            parsed[key] = value[0]
    return parsed


def _token(event, params: dict[str, object]) -> str:
    if "token" in params:
        return _first_param(params["token"])
    headers = event.get("headers") if isinstance(event, dict) else {}
    for key, value in (headers or {}).items():
        if str(key).casefold() == "x-dashboard-token":
            return str(value)
    return ""


def _first_param(value: object) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _dynamodb_table(name: str, *, region: str):
    import boto3

    return boto3.resource("dynamodb", region_name=region).Table(name)


def _response(status_code: int, body: str, *, content_type: str) -> dict[str, object]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
        "body": body,
    }
