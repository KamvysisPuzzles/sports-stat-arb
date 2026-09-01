from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

import httpx

from exchange_scanner.dynamodb_paper import _filter_stacked_positive_exposure_signals, signal_key
from exchange_scanner.the_odds_api import ValueSignal
from exchange_scanner.trading_control import is_control_item


@dataclass(frozen=True)
class LiveExecutionConfig:
    enabled: bool = False
    dry_run: bool = True
    order_table_name: str = "sports-stat-arb-live-orders"
    allowed_sport_prefixes: tuple[str, ...] = ("soccer_",)
    allowed_bookmakers: tuple[str, ...] = ("matchbook", "betfair", "smarkets")
    allowed_bet_sides: tuple[str, ...] = ("back", "lay")
    max_reference_disagreement_pct: float = 0.03
    sizing_method: str = "kelly"
    flat_order_risk: float = 1.0
    bankroll: float = 1000.0
    kelly_fraction: float = 0.10
    max_order_risk_pct: float = 0.005
    max_daily_risk_pct: float = 0.0
    min_order_risk: float = 1.0
    max_order_risk: float = 10.0
    require_confirmed_liquidity: bool = True
    min_confirmed_liquidity: float = 5.0
    allow_unconfirmed_liquidity_bookmakers: tuple[str, ...] = ("betfair",)
    prevent_stacked_event_exposure: bool = True
    prevent_cross_venue_event_exposure: bool = True


@dataclass(frozen=True)
class LiveOrderIntent:
    order_id: str
    paper_trade_id: str
    signal: ValueSignal
    limit_odds: float
    stake: float
    liability: float
    sizing_method: str
    flat_order_risk: float
    kelly_fraction: float
    full_kelly_fraction: float
    bankroll: float
    available_at_target: float | None
    dry_run: bool
    venue_metadata: dict[str, Any]


@dataclass(frozen=True)
class LiveOrderResult:
    order_id: str
    status: str
    venue_order_id: str | None = None
    matched_size: float | None = None
    avg_matched_odds: float | None = None
    remaining_size: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class LiveOrderStatus:
    order_id: str
    status: str
    venue_order_id: str | None = None
    matched_size: float | None = None
    avg_matched_odds: float | None = None
    remaining_size: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class LiveExecutionResult:
    enabled: bool
    dry_run: bool
    candidates: int
    eligible: int
    sized: int
    submitted: int
    recorded: int
    skipped: dict[str, int]


class LiveVenueExecutor(Protocol):
    def place_limit_order(self, intent: LiveOrderIntent) -> LiveOrderResult:
        ...

    def fetch_order_status(self, order: dict[str, Any]) -> LiveOrderStatus:
        ...


class DryRunVenueExecutor:
    def place_limit_order(self, intent: LiveOrderIntent) -> LiveOrderResult:
        return LiveOrderResult(
            order_id=intent.order_id,
            status="dry_run",
            matched_size=0,
            remaining_size=intent.stake,
        )

    def fetch_order_status(self, order: dict[str, Any]) -> LiveOrderStatus:
        return LiveOrderStatus(
            order_id=str(order["order_id"]),
            status=str(order.get("status") or "dry_run"),
            venue_order_id=str(order.get("venue_order_id") or "") or None,
            matched_size=_float(order.get("matched_size")),
            avg_matched_odds=_float(order.get("avg_matched_odds")) or None,
            remaining_size=_float(order.get("remaining_size")),
        )


def execute_live_signals(
    table: Any,
    signals: list[ValueSignal],
    *,
    config: LiveExecutionConfig,
    logged_at: datetime | None = None,
    liquidity_by_key: dict[tuple[str, str, str, str, str], dict[str, Any]] | None = None,
    executors: dict[str, LiveVenueExecutor] | None = None,
) -> LiveExecutionResult:
    logged_at = logged_at or datetime.now(timezone.utc)
    skipped: dict[str, int] = {}
    if not config.enabled:
        return LiveExecutionResult(
            enabled=False,
            dry_run=config.dry_run,
            candidates=len(signals),
            eligible=0,
            sized=0,
            submitted=0,
            recorded=0,
            skipped={"disabled": len(signals)},
        )

    existing_items = list_live_orders(table)
    existing_keys = {str(item.get("signal_key")) for item in existing_items}
    candidate_signals = _filter_stacked_live_exposure(
        signals,
        existing_items=existing_items,
        config=config,
    )
    allowed_signal_keys = {signal_key(signal) for signal in candidate_signals}
    daily_risk = _open_or_today_risk(existing_items, logged_at=logged_at)
    daily_risk_cap = (
        config.bankroll * config.max_daily_risk_pct
        if config.max_daily_risk_pct > 0
        else None
    )
    executor_map = executors or {}
    fallback_executor = DryRunVenueExecutor() if config.dry_run else None
    eligible = 0
    sized = 0
    submitted = 0
    recorded = 0

    for signal in signals:
        if signal_key(signal) not in allowed_signal_keys:
            _count(skipped, "stacked_event_exposure")
            continue
        reason = live_filter_reject_reason(signal, config=config)
        if reason is not None:
            _count(skipped, reason)
            continue
        key = "|".join(signal_key(signal))
        if key in existing_keys:
            _count(skipped, "duplicate_live_signal")
            continue
        liquidity = (liquidity_by_key or {}).get(signal_key(signal), {})
        reason = liquidity_reject_reason(signal, liquidity, config=config)
        if reason is not None:
            _count(skipped, reason)
            continue
        if daily_risk_cap is not None and daily_risk >= daily_risk_cap:
            _count(skipped, "daily_risk_cap")
            continue
        eligible += 1
        remaining_daily_risk = (
            max(0.0, daily_risk_cap - daily_risk)
            if daily_risk_cap is not None
            else None
        )
        intent = size_live_order(
            signal,
            config=config,
            liquidity=liquidity,
            dry_run=config.dry_run,
            max_risk=remaining_daily_risk,
        )
        if intent is None:
            _count(skipped, "below_min_size")
            continue
        sized += 1
        executor = executor_map.get(signal.target_bookmaker.casefold()) or fallback_executor
        if executor is None:
            result = LiveOrderResult(
                order_id=intent.order_id,
                status="failed",
                error="missing_live_executor",
            )
        else:
            try:
                result = executor.place_limit_order(intent)
            except Exception as exc:  # noqa: BLE001 - record failed order attempts.
                result = LiveOrderResult(
                    order_id=intent.order_id,
                    status="failed",
                    error=_format_live_order_error(exc),
                )
        submitted += 1
        if record_live_order(table, intent, result, logged_at=logged_at):
            recorded += 1
            existing_keys.add(key)
            daily_risk += intent.liability

    return LiveExecutionResult(
        enabled=True,
        dry_run=config.dry_run,
        candidates=len(signals),
        eligible=eligible,
        sized=sized,
        submitted=submitted,
        recorded=recorded,
        skipped=skipped,
    )


def live_filter_reject_reason(
    signal: ValueSignal,
    *,
    config: LiveExecutionConfig,
) -> str | None:
    sport_key = signal.sport_key.casefold()
    if not any(sport_key.startswith(prefix.casefold()) for prefix in config.allowed_sport_prefixes):
        return "sport_not_allowed"
    if signal.target_bookmaker.casefold() not in {
        bookmaker.casefold() for bookmaker in config.allowed_bookmakers
    }:
        return "bookmaker_not_allowed"
    if signal.bet_side.casefold() not in {side.casefold() for side in config.allowed_bet_sides}:
        return "side_not_allowed"
    if signal.reference_disagreement_pct is None:
        return "missing_reference_disagreement"
    if signal.reference_disagreement_pct > config.max_reference_disagreement_pct:
        return "reference_disagreement_too_high"
    if signal.edge <= 0:
        return "non_positive_edge"
    return None


def _filter_stacked_live_exposure(
    signals: list[ValueSignal],
    *,
    existing_items: list[dict[str, Any]],
    config: LiveExecutionConfig,
) -> list[ValueSignal]:
    if not config.prevent_stacked_event_exposure:
        return signals
    active_items = [
        item
        for item in existing_items
        if str(item.get("status") or "").casefold()
        in {"dry_run", "submitted", "matched", "partially_matched", "open"}
    ]
    if not config.prevent_cross_venue_event_exposure:
        return _filter_stacked_positive_exposure_signals(
            signals,
            existing_items=active_items,
        )
    scoped_existing = [_without_venue_scope(item) for item in active_items]
    scoped_signals = [_signal_without_venue_scope(signal) for signal in signals]
    kept_scoped = _filter_stacked_positive_exposure_signals(
        scoped_signals,
        existing_items=scoped_existing,
    )
    kept_ids = {id(signal) for signal in kept_scoped}
    return [signal for signal, scoped in zip(signals, scoped_signals) if id(scoped) in kept_ids]


def _without_venue_scope(item: dict[str, Any]) -> dict[str, Any]:
    scoped = dict(item)
    scoped["target_bookmaker"] = "__all_live_venues__"
    return scoped


def _signal_without_venue_scope(signal: ValueSignal) -> ValueSignal:
    return ValueSignal(
        sport_key=signal.sport_key,
        event_id=signal.event_id,
        event_name=signal.event_name,
        commence_time=signal.commence_time,
        market_key=signal.market_key,
        outcome_name=signal.outcome_name,
        target_bookmaker="__all_live_venues__",
        bet_side=signal.bet_side,
        target_odds=signal.target_odds,
        target_effective_odds=signal.target_effective_odds,
        reference_fair_odds=signal.reference_fair_odds,
        reference_probability=signal.reference_probability,
        edge=signal.edge,
        reference_bookmakers=signal.reference_bookmakers,
        betfair_fair_odds=signal.betfair_fair_odds,
        betfair_fair_edge=signal.betfair_fair_edge,
        betfair_back_lay_spread_pct=signal.betfair_back_lay_spread_pct,
        reference_fair_odds_by_bookmaker=signal.reference_fair_odds_by_bookmaker,
        reference_spread_pct_by_bookmaker=signal.reference_spread_pct_by_bookmaker,
        reference_last_update_by_bookmaker=signal.reference_last_update_by_bookmaker,
        reference_disagreement_pct=signal.reference_disagreement_pct,
        reference_max_spread_pct=signal.reference_max_spread_pct,
        reference_avg_spread_pct=signal.reference_avg_spread_pct,
    )


def liquidity_reject_reason(
    signal: ValueSignal,
    liquidity: dict[str, Any],
    *,
    config: LiveExecutionConfig,
) -> str | None:
    if not config.require_confirmed_liquidity:
        return _execution_identifier_reject_reason(liquidity, config=config)
    if signal.target_bookmaker.casefold() in {
        bookmaker.casefold() for bookmaker in config.allow_unconfirmed_liquidity_bookmakers
    }:
        return _execution_identifier_reject_reason(liquidity, config=config)
    if str(liquidity.get("liquidity_status") or "").casefold() != "available":
        return "liquidity_unavailable"
    if _float(liquidity.get("available_at_or_above_target")) <= 0:
        return "liquidity_unavailable"
    if _float(liquidity.get("available_at_or_above_target")) < config.min_confirmed_liquidity:
        return "confirmed_liquidity_below_minimum"
    return _execution_identifier_reject_reason(liquidity, config=config)


def size_live_order(
    signal: ValueSignal,
    *,
    config: LiveExecutionConfig,
    liquidity: dict[str, Any],
    dry_run: bool,
    max_risk: float | None = None,
) -> LiveOrderIntent | None:
    if config.bankroll <= 0:
        return None
    if signal.target_odds <= 1:
        return None
    liability_per_unit = signal.target_odds - 1
    available = _float(liquidity.get("available_at_or_above_target"))
    sizing_method = config.sizing_method.casefold()

    full_kelly = _full_kelly_fraction(signal)
    if sizing_method == "flat":
        if config.flat_order_risk <= 0:
            return None
        liability = config.flat_order_risk
        if signal.bet_side.casefold() == "lay":
            stake = liability / liability_per_unit
        else:
            stake = liability
    elif sizing_method == "kelly":
        if config.kelly_fraction <= 0:
            return None
        if signal.bet_side.casefold() == "lay":
            raw_liability = config.bankroll * full_kelly * config.kelly_fraction
            stake = raw_liability / liability_per_unit
            liability = stake * liability_per_unit
        else:
            stake = config.bankroll * full_kelly * config.kelly_fraction
            liability = stake
    else:
        return None

    if available > 0:
        stake = min(stake, available)
        liability = stake * liability_per_unit if signal.bet_side.casefold() == "lay" else stake

    risk_cap = min(config.bankroll * config.max_order_risk_pct, config.max_order_risk)
    if max_risk is not None:
        risk_cap = min(risk_cap, max_risk)
    if liability > risk_cap and liability > 0:
        scale = risk_cap / liability
        liability *= scale
        stake *= scale
    if liability < config.min_order_risk:
        return None

    paper_trade_id = _paper_trade_id(signal)
    order_id = live_order_id(signal, dry_run=dry_run)
    return LiveOrderIntent(
        order_id=order_id,
        paper_trade_id=paper_trade_id,
        signal=signal,
        limit_odds=signal.target_odds,
        stake=stake,
        liability=liability,
        sizing_method=sizing_method,
        flat_order_risk=config.flat_order_risk,
        kelly_fraction=config.kelly_fraction,
        full_kelly_fraction=full_kelly,
        bankroll=config.bankroll,
        available_at_target=available if available > 0 else None,
        dry_run=dry_run,
        venue_metadata=_venue_metadata_from_liquidity(liquidity),
    )


def _full_kelly_fraction(signal: ValueSignal) -> float:
    liability_per_unit = signal.target_odds - 1
    if signal.bet_side.casefold() == "lay":
        return signal.edge
    return signal.edge / liability_per_unit


def live_order_id(signal: ValueSignal, *, dry_run: bool) -> str:
    namespace = "dryrun" if dry_run else "live"
    digest = hashlib.sha256("|".join(signal_key(signal)).encode("utf-8")).hexdigest()[:24]
    return f"{namespace}#{digest}"


def record_live_order(
    table: Any,
    intent: LiveOrderIntent,
    result: LiveOrderResult,
    *,
    logged_at: datetime,
) -> bool:
    item = live_order_item(intent, result, logged_at=logged_at)
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(order_id)")
    except Exception as exc:
        if _is_conditional_check_failed(exc):
            return False
        raise
    return True


@dataclass(frozen=True)
class LiveOrderMonitorResult:
    monitored: int
    updated: int
    skipped: dict[str, int]


def reconcile_live_orders(
    table: Any,
    *,
    executors: dict[str, LiveVenueExecutor],
    checked_at: datetime | None = None,
    statuses: tuple[str, ...] = ("submitted", "matched", "partially_matched", "open"),
) -> LiveOrderMonitorResult:
    checked_at = checked_at or datetime.now(timezone.utc)
    monitored = 0
    updated = 0
    skipped: dict[str, int] = {}
    active_statuses = {status.casefold() for status in statuses}
    for order in list_live_orders(table):
        status = str(order.get("status") or "").casefold()
        if status not in active_statuses:
            continue
        monitored += 1
        bookmaker = str(order.get("target_bookmaker") or "").casefold()
        executor = executors.get(bookmaker)
        if executor is None:
            _count(skipped, "missing_executor")
            continue
        try:
            order_status = executor.fetch_order_status(order)
        except Exception as exc:  # noqa: BLE001 - persist reconciliation failures.
            order_status = LiveOrderStatus(
                order_id=str(order["order_id"]),
                status="status_check_failed",
                venue_order_id=str(order.get("venue_order_id") or "") or None,
                error=_format_live_order_error(exc),
            )
        if update_live_order_status(table, order_status, checked_at=checked_at):
            updated += 1
    return LiveOrderMonitorResult(monitored=monitored, updated=updated, skipped=skipped)


def update_live_order_status(
    table: Any,
    status: LiveOrderStatus,
    *,
    checked_at: datetime,
) -> bool:
    response = table.update_item(
        Key={"order_id": status.order_id},
        UpdateExpression=(
            "SET #status = :status, "
            "venue_order_id = :venue_order_id, "
            "matched_size = :matched_size, "
            "avg_matched_odds = :avg_matched_odds, "
            "remaining_size = :remaining_size, "
            "last_status_checked_at = :checked_at, "
            "#error = :error"
        ),
        ExpressionAttributeNames={
            "#status": "status",
            "#error": "error",
        },
        ExpressionAttributeValues={
            ":status": status.status,
            ":venue_order_id": status.venue_order_id or "",
            ":matched_size": _decimal(status.matched_size or 0),
            ":avg_matched_odds": _decimal(status.avg_matched_odds or 0),
            ":remaining_size": _decimal(status.remaining_size or 0),
            ":checked_at": checked_at.isoformat(),
            ":error": status.error or "",
        },
    )
    return response.get("ResponseMetadata", {}).get("HTTPStatusCode", 200) < 300


def live_order_item(
    intent: LiveOrderIntent,
    result: LiveOrderResult,
    *,
    logged_at: datetime,
) -> dict[str, Any]:
    signal = intent.signal
    return {
        "order_id": intent.order_id,
        "paper_trade_id": intent.paper_trade_id,
        "signal_key": "|".join(signal_key(signal)),
        "logged_at": logged_at.isoformat(),
        "sport_key": signal.sport_key,
        "event_id": signal.event_id,
        "event_name": signal.event_name,
        "commence_time": signal.commence_time.isoformat(),
        "market": signal.market_key,
        "outcome_name": signal.outcome_name,
        "target_bookmaker": signal.target_bookmaker,
        "bet_side": signal.bet_side,
        "limit_odds": _decimal(intent.limit_odds),
        "stake": _decimal(intent.stake),
        "liability": _decimal(intent.liability),
        "sizing_method": intent.sizing_method,
        "flat_order_risk": _decimal(intent.flat_order_risk),
        "bankroll_snapshot": _decimal(intent.bankroll),
        "kelly_fraction": _decimal(intent.kelly_fraction),
        "full_kelly_fraction": _decimal(intent.full_kelly_fraction),
        "edge": _decimal(signal.edge),
        "reference_disagreement_pct": _decimal(signal.reference_disagreement_pct or 0),
        "available_at_target": _decimal(intent.available_at_target or 0),
        "remaining_size": _decimal(result.remaining_size if result.remaining_size is not None else intent.stake),
        "exchange_event_id": str(intent.venue_metadata.get("event_id") or ""),
        "exchange_market_id": str(intent.venue_metadata.get("market_id") or ""),
        "exchange_runner_id": str(intent.venue_metadata.get("runner_id") or ""),
        "exchange_side": str(intent.venue_metadata.get("side") or signal.bet_side),
        "execution_mode": "dry_run" if intent.dry_run else "live",
        "status": result.status,
        "venue_order_id": result.venue_order_id or "",
        "matched_size": _decimal(result.matched_size or 0),
        "avg_matched_odds": _decimal(result.avg_matched_odds or 0),
        "error": result.error or "",
    }


def _format_live_order_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        response_text = exc.response.text.strip()
        if response_text:
            message = f"{message} | response={response_text[:1000]}"
    return message


def list_live_orders(table: Any) -> list[dict[str, Any]]:
    items = []
    scan_kwargs: dict[str, Any] = {}
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return [item for item in items if not is_control_item(item)]


def result_dict(result: LiveExecutionResult) -> dict[str, Any]:
    return {
        "enabled": result.enabled,
        "dry_run": result.dry_run,
        "candidates": result.candidates,
        "eligible": result.eligible,
        "sized": result.sized,
        "submitted": result.submitted,
        "recorded": result.recorded,
        "skipped": dict(result.skipped),
    }


def monitor_result_dict(result: LiveOrderMonitorResult) -> dict[str, Any]:
    return {
        "monitored": result.monitored,
        "updated": result.updated,
        "skipped": dict(result.skipped),
    }


def _open_or_today_risk(items: list[dict[str, Any]], *, logged_at: datetime) -> float:
    day = logged_at.date().isoformat()
    risk = 0.0
    for item in items:
        status = str(item.get("status") or "").casefold()
        logged_day = str(item.get("logged_at") or "")[:10]
        if status in {"submitted", "matched", "partially_matched", "dry_run"} or logged_day == day:
            risk += _float(item.get("liability"))
    return risk


def _paper_trade_id(signal: ValueSignal) -> str:
    try:
        from exchange_scanner.dynamodb_paper import trade_id

        return trade_id(signal)
    except Exception:  # noqa: BLE001 - fallback keeps this helper usable in isolation.
        digest = hashlib.sha256("|".join(signal_key(signal)).encode("utf-8")).hexdigest()[:24]
        return f"paper#{digest}"


def _float(value: Any) -> float:
    try:
        if value in {None, ""}:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _venue_metadata_from_liquidity(liquidity: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": liquidity.get("matchbook_event_id") or "",
        "market_id": liquidity.get("matchbook_market_id") or "",
        "runner_id": liquidity.get("matchbook_runner_id") or "",
    }


def _execution_identifier_reject_reason(
    liquidity: dict[str, Any],
    *,
    config: LiveExecutionConfig,
) -> str | None:
    if config.dry_run:
        return None
    if liquidity.get("matchbook_market_id") in {None, ""}:
        return "missing_execution_market_id"
    if liquidity.get("matchbook_runner_id") in {None, ""}:
        return "missing_execution_runner_id"
    return None


def _count(values: dict[str, int], key: str) -> None:
    values[key] = values.get(key, 0) + 1


def _is_conditional_check_failed(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    return exc.__class__.__name__ == "ConditionalCheckFailedException"
