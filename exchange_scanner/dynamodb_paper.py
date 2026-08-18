from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from exchange_scanner.the_odds_api import (
    MATCHBOOK_COMMISSION_RATE,
    ValueSignal,
    effective_decimal_odds,
)

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


@dataclass(frozen=True)
class DynamoClosingUpdateResult:
    open_trades: int
    matched: int
    updated: int


@dataclass(frozen=True)
class DynamoSettlementResult:
    open_trades: int
    matched_results: int
    settled: int


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


def update_closing_values_in_dynamodb(
    table: Any,
    signals: list[ValueSignal],
    *,
    checked_at: datetime | None = None,
) -> DynamoClosingUpdateResult:
    checked_at = checked_at or datetime.now(timezone.utc)
    by_key = {signal_key(signal): signal for signal in signals}
    open_items = list_open_trades(table)
    matched = 0
    updated = 0
    for item in open_items:
        key = item_key(item)
        signal = by_key.get(key)
        if signal is None:
            continue
        matched += 1
        target_odds = float(item["target_odds"])
        closing_target_odds = signal.target_odds
        closing_reference_fair_odds = signal.reference_fair_odds
        closing_edge = (
            effective_decimal_odds(target_odds, _commission_rate_for_bookmaker(item["target_bookmaker"]))
            / closing_reference_fair_odds
        ) - 1
        target_clv = (target_odds / closing_target_odds) - 1
        response = table.update_item(
            Key={"trade_id": item["trade_id"]},
            UpdateExpression=(
                "SET closing_checked_at = :checked_at, "
                "closing_target_odds = :closing_target_odds, "
                "target_clv = :target_clv, "
                "beat_closing_line = :beat_closing_line, "
                "closing_reference_fair_odds = :closing_reference_fair_odds, "
                "closing_edge = :closing_edge, "
                "positive_closing_edge = :positive_closing_edge"
            ),
            ExpressionAttributeValues={
                ":checked_at": checked_at.isoformat(),
                ":closing_target_odds": _decimal(closing_target_odds),
                ":target_clv": _decimal(target_clv),
                ":beat_closing_line": target_clv > 0,
                ":closing_reference_fair_odds": _decimal(closing_reference_fair_odds),
                ":closing_edge": _decimal(closing_edge),
                ":positive_closing_edge": closing_edge > 0,
            },
        )
        if response.get("ResponseMetadata", {}).get("HTTPStatusCode", 200) < 300:
            updated += 1
    return DynamoClosingUpdateResult(
        open_trades=len(open_items),
        matched=matched,
        updated=updated,
    )


def settle_results_in_dynamodb(
    table: Any,
    winners: dict[str, str],
) -> DynamoSettlementResult:
    open_items = list_open_trades(table)
    matched_results = 0
    settled = 0
    for item in open_items:
        winner = winners.get(str(item["event_id"]))
        if winner is None:
            continue
        matched_results += 1
        won = winner.casefold() == str(item["outcome_name"]).casefold()
        target_odds = float(item["target_odds"])
        stake = float(item["stake"])
        effective_odds = effective_decimal_odds(
            target_odds,
            _commission_rate_for_bookmaker(str(item["target_bookmaker"])),
        )
        profit = stake * (effective_odds - 1) if won else -stake
        response = table.update_item(
            Key={"trade_id": item["trade_id"]},
            UpdateExpression="SET #status = :settled, #result = :result, profit = :profit",
            ExpressionAttributeNames={
                "#status": "status",
                "#result": "result",
            },
            ExpressionAttributeValues={
                ":settled": "settled",
                ":result": winner,
                ":profit": _decimal(profit),
            },
        )
        if response.get("ResponseMetadata", {}).get("HTTPStatusCode", 200) < 300:
            settled += 1
    return DynamoSettlementResult(
        open_trades=len(open_items),
        matched_results=matched_results,
        settled=settled,
    )


def list_open_trades(table: Any) -> list[dict[str, Any]]:
    return list_trades_by_status(table, status="open")


def list_trades_by_status(table: Any, *, status: str) -> list[dict[str, Any]]:
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": "#status = :open_status",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":open_status": status},
    }
    return scan_all(table, scan_kwargs)


def list_all_trades(table: Any) -> list[dict[str, Any]]:
    return scan_all(table)


def scan_all(table: Any, scan_kwargs: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    scan_kwargs = dict(scan_kwargs or {})
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return items


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
        "betfair_fair_odds": _decimal(signal.betfair_fair_odds)
        if signal.betfair_fair_odds is not None
        else "",
        "betfair_fair_edge": _decimal(signal.betfair_fair_edge)
        if signal.betfair_fair_edge is not None
        else "",
        "betfair_back_lay_spread_pct": _decimal(signal.betfair_back_lay_spread_pct)
        if signal.betfair_back_lay_spread_pct is not None
        else "",
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


def item_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item["event_id"]).casefold(),
        str(item.get("market") or item.get("market_key", "h2h")).casefold(),
        str(item["outcome_name"]).casefold(),
        str(item["target_bookmaker"]).casefold(),
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


def _commission_rate_for_bookmaker(bookmaker: str) -> float:
    if bookmaker.casefold() in {"matchbook", "smarkets", "betfair", "betfair_ex_uk", "betfair_ex_eu"}:
        return MATCHBOOK_COMMISSION_RATE
    return 0.0
