from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from exchange_scanner.dynamodb_paper import list_all_trades

ACCOUNT_VENUES = (
    ("Betfair", "betfair"),
    ("Matchbook", "matchbook"),
    ("Smarkets", "smarkets"),
)


@dataclass(frozen=True)
class AccountRefreshResult:
    checked: int
    updated: int
    failed: dict[str, str]


@dataclass(frozen=True)
class SettlementRefreshResult:
    checked: int
    confirmed: int
    pending: int
    failed: dict[str, str]


def refresh_account_state(
    table: Any,
    executors: dict[str, Any],
    *,
    checked_at: datetime | None = None,
) -> AccountRefreshResult:
    checked_at = _as_utc(checked_at or datetime.now(timezone.utc))
    updated = 0
    failed: dict[str, str] = {}
    for venue, executor_key in ACCOUNT_VENUES:
        executor = executors.get(executor_key)
        if executor is None:
            error = "venue_executor_unavailable"
            failed[venue] = error
            _record_account_error(table, venue=venue, checked_at=checked_at, error=error)
            continue
        try:
            snapshot = executor.fetch_account_snapshot()
            _record_account_snapshot(
                table,
                venue=venue,
                snapshot=snapshot,
                checked_at=checked_at,
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001 - one unavailable venue must not hide the others.
            error = _safe_error(exc)
            failed[venue] = error
            _record_account_error(table, venue=venue, checked_at=checked_at, error=error)
    return AccountRefreshResult(checked=len(ACCOUNT_VENUES), updated=updated, failed=failed)


def account_refresh_dict(result: AccountRefreshResult) -> dict[str, Any]:
    return {
        "checked": result.checked,
        "updated": result.updated,
        "failed": dict(result.failed),
    }


def refresh_order_settlements(
    table: Any,
    executors: dict[str, Any],
    *,
    checked_at: datetime | None = None,
) -> SettlementRefreshResult:
    checked_at = _as_utc(checked_at or datetime.now(timezone.utc))
    candidates = [
        item
        for item in list_all_trades(table)
        if str(item.get("execution_mode") or "live").casefold() == "live"
        and str(item.get("status") or "").casefold() == "settled"
        and str(item.get("pnl_status") or "").casefold() == "estimated"
        and _float(item.get("matched_size")) > 0
    ]
    confirmed = 0
    pending = 0
    failed: dict[str, str] = {}
    for order in candidates:
        order_id = str(order.get("order_id") or "")
        venue_order_id = str(order.get("venue_order_id") or "")
        if not venue_order_id:
            failed[order_id] = "missing_venue_order_id"
            continue
        executor = executors.get(_executor_key(order.get("target_bookmaker")))
        if executor is None or not hasattr(executor, "fetch_order_settlement"):
            failed[order_id] = "venue_settlement_executor_unavailable"
            continue
        try:
            settlement = executor.fetch_order_settlement(order)
            if settlement is None:
                pending += 1
                continue
            _confirm_order_settlement(
                table,
                order=order,
                settlement=settlement,
                checked_at=checked_at,
            )
            confirmed += 1
        except Exception as exc:  # noqa: BLE001 - one venue must not block the others.
            failed[order_id] = _safe_error(exc)
    return SettlementRefreshResult(
        checked=len(candidates),
        confirmed=confirmed,
        pending=pending,
        failed=failed,
    )


def settlement_refresh_dict(result: SettlementRefreshResult) -> dict[str, Any]:
    return {
        "checked": result.checked,
        "confirmed": result.confirmed,
        "pending": result.pending,
        "failed": dict(result.failed),
    }


def _confirm_order_settlement(
    table: Any,
    *,
    order: dict[str, Any],
    settlement: dict[str, Any],
    checked_at: datetime,
) -> None:
    gross_profit = _decimal(settlement.get("gross_profit"))
    commission = _decimal(settlement.get("commission"))
    net_profit = _decimal(settlement.get("net_profit"))
    table.update_item(
        Key={"order_id": order["order_id"]},
        UpdateExpression=(
            "SET pnl_status = :confirmed, settlement_source = :source, "
            "gross_profit = :gross_profit, commission = :commission, "
            "net_profit = :net_profit, profit = :net_profit, "
            "venue_result = :venue_result, venue_settled_at = :venue_settled_at, "
            "settlement_confirmed_at = :confirmed_at REMOVE settlement_reconciliation_error"
        ),
        ExpressionAttributeValues={
            ":confirmed": "confirmed",
            ":source": str(settlement.get("settlement_source") or "venue_api"),
            ":gross_profit": gross_profit,
            ":commission": commission,
            ":net_profit": net_profit,
            ":venue_result": str(settlement.get("venue_result") or ""),
            ":venue_settled_at": str(settlement.get("venue_settled_at") or ""),
            ":confirmed_at": checked_at.isoformat(),
        },
    )


def _executor_key(value: Any) -> str:
    key = str(value or "").casefold()
    if key.startswith("betfair"):
        return "betfair"
    return key


def _record_account_snapshot(
    table: Any,
    *,
    venue: str,
    snapshot: dict[str, Any],
    checked_at: datetime,
) -> None:
    table.put_item(
        Item={
            "venue": venue,
            "currency": str(snapshot.get("currency") or "GBP"),
            "balance": _decimal(snapshot.get("balance")),
            "available_funds": _decimal(snapshot.get("available_funds")),
            "exposure": _decimal(snapshot.get("exposure")),
            "retained_commission": _decimal(snapshot.get("retained_commission")),
            "status": "ok",
            "checked_at": checked_at.isoformat(),
            "last_success_at": checked_at.isoformat(),
            "error": "",
        }
    )


def _record_account_error(
    table: Any,
    *,
    venue: str,
    checked_at: datetime,
    error: str,
) -> None:
    previous = table.get_item(Key={"venue": venue}).get("Item") or {"venue": venue}
    table.put_item(
        Item={
            **previous,
            "venue": venue,
            "status": "error",
            "checked_at": checked_at.isoformat(),
            "error": error,
        }
    )


def _safe_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    return message[:500]


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001 - account payloads are third-party JSON.
        return Decimal(0)


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
