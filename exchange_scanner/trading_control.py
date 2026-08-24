from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

CONTROL_TRADE_ID = "control#trading"


def trading_control_state(table: Any) -> dict[str, Any]:
    item = _get_control_item(table)
    paused = _truthy(item.get("paused")) if item else False
    return {
        "paused": paused,
        "enabled": not paused,
        "updated_at": item.get("updated_at") if item else "",
        "updated_by": item.get("updated_by") if item else "",
    }


def set_trading_paused(
    table: Any,
    *,
    paused: bool,
    updated_by: str = "dashboard",
    updated_at: datetime | None = None,
) -> dict[str, Any]:
    updated_at = updated_at or datetime.now(timezone.utc)
    item = {
        "trade_id": CONTROL_TRADE_ID,
        "status": "control",
        "control_type": "trading",
        "paused": paused,
        "updated_at": updated_at.isoformat(),
        "updated_by": updated_by,
    }
    table.put_item(Item=item)
    return trading_control_state(table)


def is_control_item(item: dict[str, Any]) -> bool:
    return str(item.get("trade_id") or "") == CONTROL_TRADE_ID or str(
        item.get("status") or ""
    ).casefold() == "control"


def _get_control_item(table: Any) -> dict[str, Any]:
    if hasattr(table, "get_item"):
        response = table.get_item(Key={"trade_id": CONTROL_TRADE_ID})
        return response.get("Item") or {}
    for item in _scan_all(table):
        if is_control_item(item):
            return item
    return {}


def _scan_all(table: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {}
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return items


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).casefold() in {"1", "true", "yes", "on", "paused"}
