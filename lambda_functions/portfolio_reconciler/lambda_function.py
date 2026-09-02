from __future__ import annotations

import json
import os

from exchange_scanner.live_venues import executors_from_env
from exchange_scanner.portfolio_reconciliation import (
    account_refresh_dict,
    refresh_account_state,
)


def lambda_handler(event, context):
    table_name = os.getenv("LIVE_ACCOUNT_STATE_TABLE", "")
    if not table_name:
        return _response(500, {"error": "LIVE_ACCOUNT_STATE_TABLE is not configured"})
    region = os.getenv("AWS_REGION", "eu-west-2")
    table = _dynamodb_table(table_name, region=region)
    executors = executors_from_env()
    result = refresh_account_state(table, executors)
    return _response(200, account_refresh_dict(result))


def _dynamodb_table(name: str, *, region: str):
    import boto3

    return boto3.resource("dynamodb", region_name=region).Table(name)


def _response(status_code: int, payload: dict[str, object]) -> dict[str, object]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(payload, sort_keys=True),
    }
