from __future__ import annotations

import json

from exchange_scanner.strategy_runner import run_strategy_mode


def lambda_handler(event, context):
    try:
        result = run_strategy_mode(event if isinstance(event, dict) else {})
        return {
            "statusCode": 200,
            "body": json.dumps(result),
        }
    except Exception as exc:  # noqa: BLE001 - Lambda should return structured failure JSON.
        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            ),
        }
