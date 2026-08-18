from __future__ import annotations

import json
import os
from urllib.parse import parse_qs

from exchange_scanner.dashboard import (
    dashboard_json,
    dashboard_payload,
    render_dashboard_html,
)


def lambda_handler(event, context):
    params = _query_params(event)
    token = _token(event, params)
    expected_token = os.getenv("DASHBOARD_TOKEN", "")
    if not expected_token:
        return _response(500, "DASHBOARD_TOKEN is not configured", content_type="text/plain")
    if token != expected_token:
        return _response(401, "Unauthorized", content_type="text/plain")

    table_name = os.getenv("PAPER_TRADES_TABLE", "sports-stat-arb-paper-trades")
    region = os.getenv("AWS_REGION", "eu-west-2")
    table = _dynamodb_table(table_name, region=region)
    filters = {
        "status": params.get("status", ""),
        "bookmaker": params.get("bookmaker", ""),
        "sport": params.get("sport", []),
        "league": params.get("league", []),
        "format": params.get("format", ""),
    }
    payload = dashboard_payload(table, filters=filters)
    payload["token"] = token
    if _first_param(params.get("format", "")).casefold() == "json":
        return _response(200, dashboard_json(payload), content_type="application/json")
    return _response(200, render_dashboard_html(payload), content_type="text/html; charset=utf-8")


def _query_params(event) -> dict[str, object]:
    if not isinstance(event, dict):
        return {}
    parsed: dict[str, object] = {}
    if event.get("rawQueryString"):
        parsed.update(
            {
                str(key): [str(item) for item in values if item]
                for key, values in parse_qs(str(event["rawQueryString"]), keep_blank_values=False).items()
            }
        )
    multi_params = event.get("multiValueQueryStringParameters") or {}
    for key, values in multi_params.items():
        parsed[str(key)] = [str(item) for item in values if item is not None]
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
    headers = headers or {}
    for key, value in headers.items():
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
        },
        "body": body if content_type != "application/json" else body,
    }
