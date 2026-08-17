from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from exchange_scanner.the_odds_api import ValueSignal

LIQUIDITY_FIELDS = [
    "matchbook_event_id",
    "matchbook_market_id",
    "matchbook_runner_id",
    "liquidity_status",
    "available_at_or_above_target",
    "best_back_odds",
    "best_back_available",
    "best_lay_odds",
    "best_lay_available",
    "back_lay_spread_pct",
]


@dataclass(frozen=True)
class DynamoPaperLogResult:
    attempted: int
    inserted: int
    duplicates: int


def log_signals_to_dynamodb(
    table: Any,
    signals: list[ValueSignal],
    *,
    stake: float,
    logged_at: datetime | None = None,
    liquidity_by_key: dict[tuple[str, str, str, str], dict[str, str]] | None = None,
) -> DynamoPaperLogResult:
    logged_at = logged_at or datetime.now(timezone.utc)
    inserted = 0
    duplicates = 0
    for signal in signals:
        liquidity = (liquidity_by_key or {}).get(signal_key(signal), {})
        item = paper_item(signal, stake=stake, logged_at=logged_at, liquidity=liquidity)
        try:
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(trade_id)",
            )
            inserted += 1
        except Exception as exc:
            if _is_conditional_check_failed(exc):
                duplicates += 1
                continue
            raise
    return DynamoPaperLogResult(
        attempted=len(signals),
        inserted=inserted,
        duplicates=duplicates,
    )


def paper_item(
    signal: ValueSignal,
    *,
    stake: float,
    logged_at: datetime,
    liquidity: dict[str, str] | None = None,
) -> dict[str, Any]:
    liquidity = liquidity or {}
    item: dict[str, Any] = {
        "trade_id": trade_id(signal),
        "logged_at": logged_at.isoformat(),
        "sport_key": signal.sport_key,
        "event_id": signal.event_id,
        "event_name": signal.event_name,
        "commence_time": signal.commence_time.isoformat(),
        "market": signal.market_key,
        "outcome_name": signal.outcome_name,
        "target_bookmaker": signal.target_bookmaker,
        "target_odds": _decimal(signal.target_odds),
        "target_effective_odds": _decimal(signal.effective_odds),
        "reference_fair_odds": _decimal(signal.reference_fair_odds),
        "reference_probability": _decimal(signal.reference_probability),
        "edge": _decimal(signal.edge),
        "reference_bookmakers": list(signal.reference_bookmakers),
        "reference_bookmakers_text": ", ".join(signal.reference_bookmakers),
        "stake": _decimal(stake),
        "status": "open",
        "execution_mode": "paper",
    }
    for field in LIQUIDITY_FIELDS:
        value = liquidity.get(field)
        if value in {None, ""}:
            continue
        item[field] = _maybe_decimal(value)
    return item


def trade_id(signal: ValueSignal) -> str:
    key = (
        f"{signal.event_id.casefold()}|"
        f"{signal.market_key.casefold()}|"
        f"{signal.outcome_name.casefold()}"
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"paper#{digest}"


def signal_key(signal: ValueSignal) -> tuple[str, str, str, str]:
    return (
        signal.event_id.casefold(),
        signal.market_key.casefold(),
        signal.outcome_name.casefold(),
        signal.target_bookmaker.casefold(),
    )


def _maybe_decimal(value: str) -> str | Decimal:
    try:
        return _decimal(float(value))
    except ValueError:
        return value


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _is_conditional_check_failed(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        return code == "ConditionalCheckFailedException"
    return exc.__class__.__name__ == "ConditionalCheckFailedException"
