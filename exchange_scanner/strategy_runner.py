from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from exchange_scanner.cli import (
    SPORT_PROFILES,
    STRATEGIES,
    _filter_prices_by_event_horizon,
    _filter_signals_by_max_edge,
    _unique_bet_signals,
)
from exchange_scanner.dynamodb_paper import (
    LIQUIDITY_FIELDS,
    DynamoPaperLogResult,
    log_signals_to_dynamodb,
    signal_key,
)
from exchange_scanner.matchbook_liquidity import (
    MatchbookLiquidityClient,
    unavailable_liquidity,
)
from exchange_scanner.matchbook_liquidity import match_liquidity as match_matchbook_liquidity
from exchange_scanner.odds_parquet import export_latest_snapshot_parquet
from exchange_scanner.sharpness import store_odds_snapshot
from exchange_scanner.the_odds_api import (
    TheOddsApiClient,
    ValueSignal,
    find_value_opportunities,
    normalise_odds_api_events,
)


@dataclass(frozen=True)
class StrategyRunnerConfig:
    mode: str
    odds_api_key: str
    dynamodb_table_name: str
    odds_s3_bucket: str
    aws_region: str = "eu-west-2"
    odds_s3_prefix: str = "odds_snapshots"
    sports_profile: str = "matchbook-h2h-expanded"
    markets: str = "h2h"
    regions: str = "uk,eu"
    strategy: str = "exchange-clv"
    max_api_requests: int = 80
    min_edge: float = 0.025
    max_edge: float = 0.10
    min_reference_books: int = 5
    max_age_seconds: int = 900
    max_event_days: float = 2.0
    paper_stake: float = 1.0
    matchbook_currency: str = "GBP"
    matchbook_minimum_liquidity: float = 2.0
    betfair_lambda_function_name: str = ""
    use_betfair_lambda: bool = True


def config_from_event(event: dict[str, Any] | None) -> StrategyRunnerConfig:
    event = event or {}
    env = os.environ
    return StrategyRunnerConfig(
        mode=str(event.get("mode") or env.get("STRATEGY_RUNNER_MODE") or "paper-log"),
        odds_api_key=str(event.get("odds_api_key") or env.get("THE_ODDS_API_KEY") or ""),
        dynamodb_table_name=str(
            event.get("dynamodb_table_name")
            or env.get("PAPER_TRADES_TABLE")
            or "sports-stat-arb-paper-trades"
        ),
        odds_s3_bucket=str(event.get("odds_s3_bucket") or env.get("ODDS_S3_BUCKET") or ""),
        aws_region=str(event.get("aws_region") or env.get("AWS_REGION") or "eu-west-2"),
        odds_s3_prefix=str(
            event.get("odds_s3_prefix") or env.get("ODDS_S3_PREFIX") or "odds_snapshots"
        ),
        sports_profile=str(
            event.get("sports_profile") or env.get("SPORTS_PROFILE") or "matchbook-h2h-expanded"
        ),
        markets=str(event.get("markets") or env.get("MARKETS") or "h2h"),
        regions=str(event.get("regions") or env.get("REGIONS") or "uk,eu"),
        strategy=str(event.get("strategy") or env.get("STRATEGY") or "exchange-clv"),
        max_api_requests=int(event.get("max_api_requests") or env.get("MAX_API_REQUESTS") or 80),
        min_edge=float(event.get("min_edge") or env.get("MIN_EDGE") or 0.025),
        max_edge=float(event.get("max_edge") or env.get("MAX_EDGE") or 0.10),
        min_reference_books=int(
            event.get("min_reference_books") or env.get("MIN_REFERENCE_BOOKS") or 5
        ),
        max_age_seconds=int(event.get("max_age_seconds") or env.get("MAX_AGE_SECONDS") or 900),
        max_event_days=float(event.get("max_event_days") or env.get("MAX_EVENT_DAYS") or 2.0),
        paper_stake=float(event.get("paper_stake") or env.get("PAPER_STAKE") or 1.0),
        matchbook_currency=str(
            event.get("matchbook_currency") or env.get("MATCHBOOK_CURRENCY") or "GBP"
        ),
        matchbook_minimum_liquidity=float(
            event.get("matchbook_minimum_liquidity")
            or env.get("MATCHBOOK_MINIMUM_LIQUIDITY")
            or 2.0
        ),
        betfair_lambda_function_name=str(
            event.get("betfair_lambda_function_name")
            or env.get("BETFAIR_LAMBDA_FUNCTION_NAME")
            or ""
        ),
        use_betfair_lambda=_bool(
            event.get("use_betfair_lambda", env.get("USE_BETFAIR_LAMBDA", "true"))
        ),
    )


def run_strategy_mode(
    event: dict[str, Any] | None = None,
    *,
    odds_client: Any | None = None,
    matchbook_client: Any | None = None,
    dynamodb_table: Any | None = None,
    s3_client: Any | None = None,
    lambda_client: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = config_from_event(event)
    if config.mode != "paper-log":
        raise ValueError(f"Unsupported strategy runner mode: {config.mode}")
    return run_paper_log(
        config,
        odds_client=odds_client,
        matchbook_client=matchbook_client,
        dynamodb_table=dynamodb_table,
        s3_client=s3_client,
        lambda_client=lambda_client,
        now=now,
    )


def run_paper_log(
    config: StrategyRunnerConfig,
    *,
    odds_client: Any | None = None,
    matchbook_client: Any | None = None,
    dynamodb_table: Any | None = None,
    s3_client: Any | None = None,
    lambda_client: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    _validate_config(config)
    now = now or datetime.now(timezone.utc)
    odds_client = odds_client or TheOddsApiClient(api_key=config.odds_api_key)
    sports = SPORT_PROFILES[config.sports_profile]
    if len(sports) > config.max_api_requests:
        raise ValueError(
            f"Refusing to make {len(sports)} The Odds API requests; "
            f"MAX_API_REQUESTS is {config.max_api_requests}"
        )

    events = []
    for sport in sports:
        events.extend(
            odds_client.fetch_odds(
                sport=sport,
                regions=config.regions,
                markets=config.markets,
            )
        )
    prices = normalise_odds_api_events(events)
    snapshot = _archive_odds_snapshot(config, prices, s3_client=s3_client, snapshot_time=now)

    signals = _find_signals(config, prices, now=now)
    rows = _signal_rows(signals)
    rows = _enrich_matchbook_rows(config, rows, matchbook_client=matchbook_client, now=now)
    rows = _enrich_betfair_rows(config, rows, lambda_client=lambda_client)
    executable_rows = [row for row in rows if row.get("liquidity_status") == "available"]
    executable_keys = {_row_key(row) for row in executable_rows}
    executable_signals = [
        signal for signal in signals if signal_key(signal) in executable_keys
    ]
    liquidity_by_key = {
        _row_key(row): {field: row.get(field, "") for field in LIQUIDITY_FIELDS}
        for row in executable_rows
    }
    table = dynamodb_table or _dynamodb_table(config)
    log_result = log_signals_to_dynamodb(
        table,
        executable_signals,
        stake=config.paper_stake,
        logged_at=now,
        liquidity_by_key=liquidity_by_key,
    )
    return {
        "mode": config.mode,
        "sports": len(sports),
        "odds_rows": len(prices),
        "snapshot": snapshot,
        "candidate_signals": len(signals),
        "liquidity_confirmed_signals": len(executable_signals),
        "paper_log": _log_result_dict(log_result),
    }


def _find_signals(
    config: StrategyRunnerConfig,
    prices,
    *,
    now: datetime,
) -> list[ValueSignal]:
    allowed_markets = {market.strip() for market in config.markets.split(",") if market.strip()}
    prices = [price for price in prices if price.market_key in allowed_markets]
    prices = _filter_prices_by_event_horizon(
        prices,
        max_event_days=config.max_event_days,
        now=now,
    )
    strategy = STRATEGIES[config.strategy]
    signals = find_value_opportunities(
        prices,
        target_bookmakers=strategy["target_bookmakers"],
        reference_bookmakers=strategy["reference_bookmakers"],
        min_edge=config.min_edge,
        max_age_seconds=config.max_age_seconds,
        min_reference_books=config.min_reference_books,
        allow_target_bookmakers_as_references=strategy["allow_target_bookmakers_as_references"],
        reference_weights=strategy["reference_weights"],
        target_commission_rates=strategy["target_commission_rates"],
        now=now,
    )
    signals = _filter_signals_by_max_edge(signals, max_edge=config.max_edge)
    return _unique_bet_signals(signals)


def _archive_odds_snapshot(
    config: StrategyRunnerConfig,
    prices,
    *,
    s3_client: Any | None,
    snapshot_time: datetime,
) -> dict[str, Any]:
    if not config.odds_s3_bucket:
        return {"uploaded": False, "reason": "missing_odds_s3_bucket"}
    s3_client = s3_client or _boto3_client("s3", config.aws_region)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        market_db = tmp_path / "market_snapshots.sqlite3"
        inserted = store_odds_snapshot(market_db, prices, snapshot_time=snapshot_time)
        parquet_path, s3_key, parsed_snapshot_time, row_count = export_latest_snapshot_parquet(
            market_db,
            tmp_path / "parquet",
            s3_prefix=config.odds_s3_prefix,
        )
        s3_client.upload_file(str(parquet_path), config.odds_s3_bucket, s3_key)
    return {
        "uploaded": True,
        "bucket": config.odds_s3_bucket,
        "key": s3_key,
        "snapshot_time": parsed_snapshot_time.isoformat(),
        "rows_inserted": inserted,
        "parquet_rows": row_count,
    }


def _enrich_matchbook_rows(
    config: StrategyRunnerConfig,
    rows: list[dict[str, str]],
    *,
    matchbook_client: Any | None,
    now: datetime,
) -> list[dict[str, str]]:
    if not rows:
        return []
    matchbook_client = matchbook_client or MatchbookLiquidityClient()
    end = now + timedelta(days=config.max_event_days)
    events = matchbook_client.fetch_events(
        start=now,
        end=end,
        currency=config.matchbook_currency,
        minimum_liquidity=config.matchbook_minimum_liquidity,
    )
    enriched = []
    for row in rows:
        output = dict(row)
        if row.get("target_bookmaker", "").casefold() != "matchbook":
            output.update(_liquidity_row(unavailable_liquidity("not_applicable")))
        else:
            match = match_matchbook_liquidity(
                events,
                event_name=row["event_name"],
                market_key=row.get("market", "h2h"),
                outcome_name=row["outcome_name"],
                target_odds=float(row["target_odds"]),
            )
            output.update(_liquidity_row(match))
        enriched.append(output)
    return enriched


def _enrich_betfair_rows(
    config: StrategyRunnerConfig,
    rows: list[dict[str, str]],
    *,
    lambda_client: Any | None,
) -> list[dict[str, str]]:
    if not rows or not config.use_betfair_lambda or not config.betfair_lambda_function_name:
        return rows
    if not any(row.get("target_bookmaker", "").casefold() == "betfair" for row in rows):
        return rows

    lambda_client = lambda_client or _boto3_client("lambda", config.aws_region)
    response = lambda_client.invoke(
        FunctionName=config.betfair_lambda_function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps({"csv": _rows_csv(rows)}).encode("utf-8"),
    )
    payload = _read_lambda_payload(response)
    status_code = int(payload.get("statusCode", 500))
    body = json.loads(payload.get("body") or "{}")
    if status_code >= 400:
        raise RuntimeError(f"Betfair enrichment Lambda returned {status_code}: {body}")
    return _read_rows_csv(str(body["csv"]))


def _signal_rows(signals: list[ValueSignal]) -> list[dict[str, str]]:
    return [_signal_row(signal) for signal in signals]


def _signal_row(signal: ValueSignal) -> dict[str, str]:
    return {
        "sport_key": signal.sport_key,
        "event_id": signal.event_id,
        "event_name": signal.event_name,
        "commence_time": signal.commence_time.isoformat(),
        "market": signal.market_key,
        "outcome_name": signal.outcome_name,
        "bet_to_place": (
            f"Back {signal.outcome_name} with {signal.target_bookmaker} at {signal.target_odds:g}"
        ),
        "target_bookmaker": signal.target_bookmaker,
        "target_odds": f"{signal.target_odds:.4f}",
        "target_effective_odds": f"{signal.effective_odds:.4f}",
        "reference_fair_odds": f"{signal.reference_fair_odds:.4f}",
        "reference_probability": f"{signal.reference_probability:.4f}",
        "edge": f"{signal.edge:.4f}",
        "reference_bookmakers": ", ".join(signal.reference_bookmakers),
    }


def _liquidity_row(match) -> dict[str, str]:
    return {
        "matchbook_event_id": str(match.matchbook_event_id or ""),
        "matchbook_market_id": str(match.matchbook_market_id or ""),
        "matchbook_runner_id": str(match.matchbook_runner_id or ""),
        "liquidity_status": match.liquidity_status,
        "available_at_or_above_target": f"{match.available_at_or_above_target:.2f}",
        "best_back_odds": _format_optional(match.best_back_odds),
        "best_back_available": f"{match.best_back_available:.2f}",
        "best_lay_odds": _format_optional(match.best_lay_odds),
        "best_lay_available": f"{match.best_lay_available:.2f}",
        "back_lay_spread_pct": (
            f"{match.back_lay_spread_pct:.4f}" if match.back_lay_spread_pct is not None else ""
        ),
    }


def _rows_csv(rows: list[dict[str, str]]) -> str:
    if not rows:
        return ""
    fieldnames = list(dict.fromkeys(field for row in rows for field in row))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _read_rows_csv(value: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(value))) if value else []


def _read_lambda_payload(response: dict[str, Any]) -> dict[str, Any]:
    payload = response.get("Payload")
    if hasattr(payload, "read"):
        raw = payload.read()
    else:
        raw = payload or b"{}"
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw or "{}")


def _row_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        row["event_id"].casefold(),
        row.get("market", "h2h").casefold(),
        row["outcome_name"].casefold(),
        row["target_bookmaker"].casefold(),
    )


def _dynamodb_table(config: StrategyRunnerConfig):
    import boto3

    return boto3.resource("dynamodb", region_name=config.aws_region).Table(
        config.dynamodb_table_name
    )


def _boto3_client(service: str, region: str):
    import boto3

    return boto3.client(service, region_name=region)


def _validate_config(config: StrategyRunnerConfig) -> None:
    if not config.odds_api_key:
        raise ValueError("Missing THE_ODDS_API_KEY")
    if not config.dynamodb_table_name:
        raise ValueError("Missing PAPER_TRADES_TABLE")
    if config.sports_profile not in SPORT_PROFILES:
        raise ValueError(f"Unknown sports profile: {config.sports_profile}")
    if config.strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {config.strategy}")


def _log_result_dict(result: DynamoPaperLogResult) -> dict[str, int]:
    return {
        "attempted": result.attempted,
        "inserted": result.inserted,
        "duplicates": result.duplicates,
    }


def _format_optional(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {"0", "false", "no", "off", ""}
