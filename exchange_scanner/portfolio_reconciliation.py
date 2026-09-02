from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
