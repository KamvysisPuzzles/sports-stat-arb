from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from exchange_scanner.the_odds_api import (
    MATCHBOOK_COMMISSION_RATE,
    ValueSignal,
    effective_decimal_odds,
    lay_edge_per_liability,
)
from exchange_scanner.trading_control import is_control_item

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
    liquidity_by_key: dict[tuple[str, str, str, str, str], dict[str, str]] | None = None,
) -> DynamoPaperLogResult:
    logged_at = logged_at or datetime.now(timezone.utc)
    attempted = len(signals)
    signals = _filter_stacked_positive_exposure_signals(
        signals,
        existing_items=list_open_trades(table),
    )
    inserted = 0
    duplicates = 0
    for signal in signals:
        liquidity = (liquidity_by_key or {}).get(signal_key(signal), {})
        item = paper_item(
            signal,
            stake=_risk_normalized_stake(signal, risk=stake),
            logged_at=logged_at,
            liquidity=liquidity,
        )
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
        attempted=attempted,
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
        commission_rate = _commission_rate_for_bookmaker(item["target_bookmaker"])
        bet_side = str(item.get("bet_side") or "back").casefold()
        if bet_side == "lay":
            closing_edge = lay_edge_per_liability(
                lay_odds=target_odds,
                fair_probability=signal.reference_probability,
                commission_rate=commission_rate,
            )
            target_clv = (closing_target_odds / target_odds) - 1
        else:
            closing_edge = (
                effective_decimal_odds(target_odds, commission_rate)
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
        bet_side = str(item.get("bet_side") or "back").casefold()
        selection_won = winner.casefold() == str(item["outcome_name"]).casefold()
        won = not selection_won if bet_side == "lay" else selection_won
        target_odds = float(item["target_odds"])
        stake = float(item["stake"])
        commission_rate = _commission_rate_for_bookmaker(str(item["target_bookmaker"]))
        if bet_side == "lay":
            profit = stake * (1 - commission_rate) if won else -(stake * (target_odds - 1))
        else:
            effective_odds = effective_decimal_odds(target_odds, commission_rate)
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
    return [item for item in scan_all(table) if not is_control_item(item)]


def delete_all_trades(table: Any) -> int:
    deleted = 0
    for item in list_all_trades(table):
        table.delete_item(Key={"trade_id": item["trade_id"]})
        deleted += 1
    return deleted


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
        "bet_side": signal.bet_side,
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
        "reference_fair_odds_by_bookmaker": _json_diagnostic(
            signal.reference_fair_odds_by_bookmaker
        ),
        "reference_spread_pct_by_bookmaker": _json_diagnostic(
            signal.reference_spread_pct_by_bookmaker
        ),
        "reference_last_update_by_bookmaker": _json_diagnostic(
            signal.reference_last_update_by_bookmaker
        ),
        "reference_disagreement_pct": _decimal(signal.reference_disagreement_pct)
        if signal.reference_disagreement_pct is not None
        else "",
        "reference_max_spread_pct": _decimal(signal.reference_max_spread_pct)
        if signal.reference_max_spread_pct is not None
        else "",
        "reference_avg_spread_pct": _decimal(signal.reference_avg_spread_pct)
        if signal.reference_avg_spread_pct is not None
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
        f"{signal.outcome_name.casefold()}|"
        f"{signal.bet_side.casefold()}"
    )
    if signal.bet_side.casefold() == "back":
        key = (
            f"{signal.event_id.casefold()}|"
            f"{signal.market_key.casefold()}|"
            f"{signal.outcome_name.casefold()}"
        )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"paper#{digest}"


def signal_key(signal: ValueSignal) -> tuple[str, str, str, str, str]:
    return (
        signal.event_id.casefold(),
        signal.market_key.casefold(),
        signal.outcome_name.casefold(),
        signal.target_bookmaker.casefold(),
        signal.bet_side.casefold(),
    )


def item_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(item["event_id"]).casefold(),
        str(item.get("market") or item.get("market_key", "h2h")).casefold(),
        str(item["outcome_name"]).casefold(),
        str(item["target_bookmaker"]).casefold(),
        str(item.get("bet_side") or "back").casefold(),
    )


def _filter_stacked_positive_exposure_signals(
    signals: list[ValueSignal],
    *,
    existing_items: list[dict[str, Any]],
) -> list[ValueSignal]:
    kept: list[ValueSignal] = []
    existing_by_group: dict[tuple[str, str, str], list[dict[str, Any] | ValueSignal]] = {}
    for item in existing_items:
        if str(item.get("market") or item.get("market_key", "h2h")).casefold() != "h2h":
            continue
        existing_by_group.setdefault(_exposure_group_key(item), []).append(item)

    for signal in signals:
        if signal.market_key.casefold() != "h2h":
            kept.append(signal)
            continue
        group_key = _exposure_group_key(signal)
        group_items = existing_by_group.setdefault(group_key, [])
        universe = _event_outcome_universe([*group_items, signal])
        signal_positive = _positive_outcomes(signal, universe)
        existing_positive: set[str] = set()
        blocked = False
        for item in group_items:
            if _same_bet(item, signal):
                continue
            positive = _positive_outcomes(item, universe)
            if signal_positive & existing_positive:
                blocked = True
                break
            if signal_positive & positive:
                blocked = True
                break
            existing_positive.update(positive)
        if blocked:
            continue
        kept.append(signal)
        group_items.append(signal)
    return kept


def _exposure_group_key(item: dict[str, Any] | ValueSignal) -> tuple[str, str, str]:
    if isinstance(item, ValueSignal):
        return (
            item.event_id.casefold(),
            item.market_key.casefold(),
            item.target_bookmaker.casefold(),
        )
    return (
        str(item["event_id"]).casefold(),
        str(item.get("market") or item.get("market_key", "h2h")).casefold(),
        str(item["target_bookmaker"]).casefold(),
    )


def _event_outcome_universe(items: list[dict[str, Any] | ValueSignal]) -> set[str]:
    outcomes: set[str] = set()
    soccer = False
    for item in items:
        if isinstance(item, ValueSignal):
            outcomes.add(item.outcome_name.casefold())
            event_name = item.event_name
            sport_key = item.sport_key
        else:
            outcomes.add(str(item.get("outcome_name") or "").casefold())
            event_name = str(item.get("event_name") or "")
            sport_key = str(item.get("sport_key") or "")
        soccer = soccer or sport_key.casefold().startswith("soccer_")
        if " v " in event_name:
            home, away = event_name.split(" v ", 1)
            outcomes.update({home.casefold(), away.casefold()})
    if soccer:
        outcomes.add("draw")
    return {outcome for outcome in outcomes if outcome}


def _positive_outcomes(
    item: dict[str, Any] | ValueSignal,
    universe: set[str],
) -> set[str]:
    if isinstance(item, ValueSignal):
        outcome = item.outcome_name.casefold()
        bet_side = item.bet_side.casefold()
    else:
        outcome = str(item.get("outcome_name") or "").casefold()
        bet_side = str(item.get("bet_side") or "back").casefold()
    if bet_side == "lay":
        return {other for other in universe if other != outcome}
    return {outcome}


def _same_bet(item: dict[str, Any] | ValueSignal, signal: ValueSignal) -> bool:
    if isinstance(item, ValueSignal):
        return signal_key(item) == signal_key(signal)
    return item_key(item) == signal_key(signal)


def _maybe_decimal(value: str) -> str | Decimal:
    try:
        return _decimal(float(value))
    except ValueError:
        return value


def _json_diagnostic(items: tuple[tuple[str, float | str], ...]) -> str:
    if not items:
        return ""
    return json.dumps(dict(items), sort_keys=True, separators=(",", ":"))


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


def _risk_normalized_stake(signal: ValueSignal, *, risk: float) -> float:
    if signal.bet_side.casefold() != "lay":
        return risk
    liability_per_unit = signal.target_odds - 1
    if liability_per_unit <= 0:
        return 0.0
    return risk / liability_per_unit
