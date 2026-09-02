from __future__ import annotations

import json
import traceback

from exchange_scanner.strategy_runner import run_strategy_mode


def lambda_handler(event, context):
    try:
        result = run_strategy_mode(event if isinstance(event, dict) else {})
        print(
            "STRATEGY_RUNNER_RESULT "
            + json.dumps(
                {
                    "mode": result.get("mode"),
                    "settlement": result.get("settlement"),
                    "paper_log": result.get("paper_log"),
                    "tennis_lead_lag": result.get("tennis_lead_lag"),
                    "live_execution": result.get("live_execution"),
                    "live_order_monitor": result.get("live_order_monitor"),
                    "portfolio_summary": {
                        key: result.get("portfolio_summary", {}).get(key)
                        for key in (
                            "total_trades",
                            "open_trades",
                            "settled_trades",
                            "settled_won",
                            "settled_lost",
                            "settled_profit",
                        )
                    },
                },
                default=str,
            )
        )
        return {
            "statusCode": 200,
            "body": json.dumps(result),
        }
    except Exception as exc:  # noqa: BLE001 - Lambda should return structured failure JSON.
        traceback.print_exc()
        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            ),
        }
