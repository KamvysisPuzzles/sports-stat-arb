from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from exchange_scanner.cli import (
    ACTIVE_H2H_PROFILE,
    BETFAIR_TARGET_BOOKMAKERS,
    MATCHBOOK_DISCOVERY_H2H_PROFILE,
    MATCHBOOK_DISCOVERY_SPORT_KEYS,
    MATCHBOOK_DISCOVERY_SPORT_PREFIXES,
    SHARP_REFERENCE_BOOKMAKER_TITLES,
    SOCCER_H2H_PROFILE,
    SPORT_PROFILES,
    STRATEGIES,
    _filter_prices_by_event_horizon,
    _filter_signals_by_max_edge,
    _filter_sports_by_strategy_scope,
    _markets_for_sport,
    _unique_bet_signals,
    active_h2h_sports,
    find_strategy_value_opportunities,
)
from exchange_scanner.dynamodb_paper import (
    LIQUIDITY_FIELDS,
    DynamoClosingUpdateResult,
    DynamoPaperLogResult,
    DynamoSettlementResult,
    delete_all_trades,
    list_all_trades,
    list_open_trades,
    log_signals_to_dynamodb,
    settle_results_in_dynamodb,
    signal_key,
    update_closing_values_in_dynamodb,
)
from exchange_scanner.matchbook_liquidity import (
    MatchbookLiquidityClient,
    unavailable_liquidity as unavailable_matchbook_liquidity,
)
from exchange_scanner.matchbook_liquidity import match_liquidity as match_matchbook_liquidity
from exchange_scanner.odds_parquet import export_latest_snapshot_parquet
from exchange_scanner.sharpness import store_odds_snapshot
from exchange_scanner.smarkets_liquidity import (
    SmarketsLiquidityClient,
)
from exchange_scanner.smarkets_liquidity import match_liquidity as match_smarkets_liquidity
from exchange_scanner.smarkets_liquidity import (
    unavailable_liquidity as unavailable_smarkets_liquidity,
)
from exchange_scanner.the_odds_api import (
    TheOddsApiClient,
    ValueSignal,
    h2h_winners_from_scores,
    normalise_odds_api_events,
)
from exchange_scanner.trading_control import trading_control_state


@dataclass(frozen=True)
class StrategyRunnerConfig:
    mode: str
    odds_api_key: str
    dynamodb_table_name: str
    odds_s3_bucket: str
    aws_region: str = "eu-west-2"
    odds_s3_prefix: str = "odds_snapshots"
    sports_profile: str = SOCCER_H2H_PROFILE
    markets: str = "h2h,h2h_lay"
    regions: str = "uk,eu"
    strategy: str = "exchange-clv"
    max_api_requests: int = 100
    filter_inactive_sports: bool = True
    min_edge: float = 0.005
    max_edge: float = 0.10
    min_reference_books: int = 2
    max_age_seconds: int = 900
    max_event_days: float = 4.0
    closing_max_event_days: float = 7.0
    scores_days_from: int = 3
    summary_s3_prefix: str = "summaries"
    paper_stake: float = 1.0
    matchbook_currency: str = "GBP"
    matchbook_minimum_liquidity: float = 2.0
    smarkets_session_token: str = ""
    smarkets_username: str = ""
    smarkets_password: str = ""
    settle_finished_trades: bool = True
    enable_matchbook_discovery: bool = False
    betfair_lambda_function_name: str = ""
    use_betfair_lambda: bool = True
    max_betfair_spread_pct: float | None = None


def config_from_event(event: dict[str, Any] | None) -> StrategyRunnerConfig:
    event = event or {}
    env = os.environ
    strategy_name = str(event.get("strategy") or env.get("STRATEGY") or "exchange-clv")
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
            event.get("sports_profile") or env.get("SPORTS_PROFILE") or SOCCER_H2H_PROFILE
        ),
        markets=str(
            event.get("markets") or env.get("MARKETS") or "h2h,h2h_lay"
        ),
        regions=str(event.get("regions") or env.get("REGIONS") or "uk,eu"),
        strategy=strategy_name,
        max_api_requests=int(event.get("max_api_requests") or env.get("MAX_API_REQUESTS") or 100),
        filter_inactive_sports=_bool(
            event.get("filter_inactive_sports", env.get("FILTER_INACTIVE_SPORTS", "true"))
        ),
        min_edge=float(
            event.get("min_edge")
            or env.get("MIN_EDGE")
            or STRATEGIES.get(strategy_name, {}).get("default_min_edge")
            or 0.005
        ),
        max_edge=float(event.get("max_edge") or env.get("MAX_EDGE") or 0.10),
        min_reference_books=int(
            event.get("min_reference_books") or env.get("MIN_REFERENCE_BOOKS") or 2
        ),
        max_age_seconds=int(event.get("max_age_seconds") or env.get("MAX_AGE_SECONDS") or 900),
        max_event_days=float(event.get("max_event_days") or env.get("MAX_EVENT_DAYS") or 4.0),
        closing_max_event_days=float(
            event.get("closing_max_event_days") or env.get("CLOSING_MAX_EVENT_DAYS") or 7.0
        ),
        scores_days_from=int(event.get("scores_days_from") or env.get("SCORES_DAYS_FROM") or 3),
        summary_s3_prefix=str(
            event.get("summary_s3_prefix") or env.get("SUMMARY_S3_PREFIX") or "summaries"
        ),
        paper_stake=float(event.get("paper_stake") or env.get("PAPER_STAKE") or 1.0),
        matchbook_currency=str(
            event.get("matchbook_currency") or env.get("MATCHBOOK_CURRENCY") or "GBP"
        ),
        matchbook_minimum_liquidity=float(
            event.get("matchbook_minimum_liquidity")
            or env.get("MATCHBOOK_MINIMUM_LIQUIDITY")
            or 2.0
        ),
        smarkets_session_token=str(
            event.get("smarkets_session_token") or env.get("SMARKETS_SESSION_TOKEN") or ""
        ),
        smarkets_username=str(
            event.get("smarkets_username") or env.get("SMARKETS_USERNAME") or ""
        ),
        smarkets_password=str(
            event.get("smarkets_password") or env.get("SMARKETS_PASSWORD") or ""
        ),
        settle_finished_trades=_bool(
            event.get("settle_finished_trades", env.get("SETTLE_FINISHED_TRADES", "true"))
        ),
        enable_matchbook_discovery=_bool(
            event.get(
                "enable_matchbook_discovery",
                env.get("ENABLE_MATCHBOOK_DISCOVERY", "false"),
            )
        ),
        betfair_lambda_function_name=str(
            event.get("betfair_lambda_function_name")
            or env.get("BETFAIR_LAMBDA_FUNCTION_NAME")
            or ""
        ),
        use_betfair_lambda=_bool(
            event.get("use_betfair_lambda", env.get("USE_BETFAIR_LAMBDA", "true"))
        ),
        max_betfair_spread_pct=_optional_float(
            event.get("max_betfair_spread_pct") or env.get("MAX_BETFAIR_SPREAD_PCT")
        ),
    )


def run_strategy_mode(
    event: dict[str, Any] | None = None,
    *,
    odds_client: Any | None = None,
    matchbook_client: Any | None = None,
    smarkets_client: Any | None = None,
    dynamodb_table: Any | None = None,
    s3_client: Any | None = None,
    lambda_client: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = config_from_event(event)
    if config.mode == "clear-paper-trades":
        table = dynamodb_table or _dynamodb_table(config)
        return {
            "mode": config.mode,
            "table": config.dynamodb_table_name,
            "deleted": delete_all_trades(table),
        }
    if config.mode == "paper-log-combined":
        return run_combined_paper_log(
            config,
            odds_client=odds_client,
            matchbook_client=matchbook_client,
            smarkets_client=smarkets_client,
            dynamodb_table=dynamodb_table,
            s3_client=s3_client,
            lambda_client=lambda_client,
            now=now,
        )
    if config.mode != "paper-log":
        raise ValueError(f"Unsupported strategy runner mode: {config.mode}")
    return run_paper_log(
        config,
        odds_client=odds_client,
        matchbook_client=matchbook_client,
        smarkets_client=smarkets_client,
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
    smarkets_client: Any | None = None,
    dynamodb_table: Any | None = None,
    s3_client: Any | None = None,
    lambda_client: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    _validate_config(config)
    now = now or datetime.now(timezone.utc)
    smarkets_client = _ensure_smarkets_client(config, smarkets_client)
    smarkets_keepalive = _keep_smarkets_session_alive(config, smarkets_client)
    odds_client = odds_client or TheOddsApiClient(api_key=config.odds_api_key)
    sports = _sports_for_profile(
        config.sports_profile,
        odds_client,
        filter_inactive_sports=config.filter_inactive_sports,
    )
    strategy = STRATEGIES[config.strategy]
    sports = _filter_sports_by_strategy_scope(sports, strategy)
    if len(sports) > config.max_api_requests:
        raise ValueError(
            f"Refusing to make {len(sports)} The Odds API requests; "
            f"MAX_API_REQUESTS is {config.max_api_requests}"
        )

    events = []
    for sport in sports:
        markets = _markets_for_sport(sport, config.markets, strategy)
        if not markets:
            continue
        events.extend(
            odds_client.fetch_odds(
                sport=sport,
                regions=config.regions,
                markets=markets,
            )
        )
    prices = normalise_odds_api_events(events)
    snapshot = _archive_odds_snapshot(config, prices, s3_client=s3_client, snapshot_time=now)
    table = dynamodb_table or _dynamodb_table(config)
    closing_signals = _find_closing_signals(config, prices, now=now)
    closing_update = update_closing_values_in_dynamodb(
        table,
        closing_signals,
        checked_at=now,
    )
    settlement = (
        _settle_finished_trades(
            config,
            odds_client=odds_client,
            table=table,
        )
        if config.settle_finished_trades
        else DynamoSettlementResult(open_trades=0, matched_results=0, settled=0)
    )
    trading_control = trading_control_state(table)
    if trading_control["paused"]:
        result = {
            "mode": config.mode,
            "sports": len(sports),
            "odds_rows": len(prices),
            "snapshot": snapshot,
            "closing_update": _closing_result_dict(closing_update),
            "settlement": _settlement_result_dict(settlement),
            "smarkets_keepalive": smarkets_keepalive,
            "trading_control": trading_control,
            "candidate_signals": 0,
            "paper_eligible_signals": 0,
            "liquidity_confirmed_signals": 0,
            "paper_log": {"attempted": 0, "inserted": 0, "duplicates": 0},
        }
        portfolio_summary = build_portfolio_summary(table, generated_at=now)
        result["portfolio_summary"] = portfolio_summary
        result["summary"] = _write_latest_summary(
            config,
            s3_client=s3_client,
            run_result=result,
            generated_at=now,
        )
        return result

    signals = _find_signals(config, prices, now=now)
    signals = _filter_execution_signals_by_strategy_limits(config, signals)
    rows = _signal_rows(signals)
    rows = _enrich_matchbook_rows(config, rows, matchbook_client=matchbook_client, now=now)
    rows = _enrich_smarkets_rows(config, rows, smarkets_client=smarkets_client, now=now)
    rows = _enrich_betfair_rows(config, rows, lambda_client=lambda_client)
    executable_rows = [row for row in rows if _paper_loggable_row(row)]
    executable_keys = {_row_key(row) for row in executable_rows}
    executable_signals = [
        signal for signal in signals if signal_key(signal) in executable_keys
    ]
    liquidity_confirmed_rows = [
        row for row in executable_rows if row.get("liquidity_status") == "available"
    ]
    liquidity_by_key = {
        _row_key(row): {field: row.get(field, "") for field in LIQUIDITY_FIELDS}
        for row in executable_rows
    }
    log_result = log_signals_to_dynamodb(
        table,
        executable_signals,
        stake=config.paper_stake,
        logged_at=now,
        liquidity_by_key=liquidity_by_key,
    )
    result = {
        "mode": config.mode,
        "sports": len(sports),
        "odds_rows": len(prices),
        "snapshot": snapshot,
        "closing_update": _closing_result_dict(closing_update),
        "settlement": _settlement_result_dict(settlement),
        "smarkets_keepalive": smarkets_keepalive,
        "trading_control": trading_control,
        "candidate_signals": len(signals),
        "paper_eligible_signals": len(executable_signals),
        "liquidity_confirmed_signals": len(liquidity_confirmed_rows),
        "paper_log": _log_result_dict(log_result),
    }
    portfolio_summary = build_portfolio_summary(table, generated_at=now)
    result["portfolio_summary"] = portfolio_summary
    result["summary"] = _write_latest_summary(
        config,
        s3_client=s3_client,
        run_result=result,
        generated_at=now,
    )
    return result


def run_combined_paper_log(
    config: StrategyRunnerConfig,
    *,
    odds_client: Any | None = None,
    matchbook_client: Any | None = None,
    smarkets_client: Any | None = None,
    dynamodb_table: Any | None = None,
    s3_client: Any | None = None,
    lambda_client: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    _validate_config(config)
    table = dynamodb_table or _dynamodb_table(config)
    odds_client = odds_client or TheOddsApiClient(api_key=config.odds_api_key)
    soccer_config = _combined_branch_config(
        config,
        label="soccer",
        sports_profile=ACTIVE_H2H_PROFILE,
        strategy="exchange-clv",
        min_edge=0.005,
    )
    soccer_result = run_paper_log(
        soccer_config,
        odds_client=odds_client,
        matchbook_client=matchbook_client,
        smarkets_client=smarkets_client,
        dynamodb_table=table,
        s3_client=s3_client,
        lambda_client=lambda_client,
        now=now,
    )
    discovery_result = (
        run_paper_log(
            _combined_branch_config(
                config,
                label="matchbook-discovery",
                sports_profile=MATCHBOOK_DISCOVERY_H2H_PROFILE,
                strategy="matchbook-sharp-h2h",
                min_edge=0.015,
            ),
            odds_client=odds_client,
            matchbook_client=matchbook_client,
            smarkets_client=smarkets_client,
            dynamodb_table=table,
            s3_client=s3_client,
            lambda_client=lambda_client,
            now=now,
        )
        if config.enable_matchbook_discovery
        else _empty_branch_result()
    )
    settlement = _settle_finished_trades(config, odds_client=odds_client, table=table)
    portfolio_summary = build_portfolio_summary(table, generated_at=now)
    result = {
        "mode": config.mode,
        "branches": {
            "soccer": soccer_result,
            "matchbook_discovery": discovery_result,
        },
        "sports": soccer_result["sports"] + discovery_result["sports"],
        "odds_rows": soccer_result["odds_rows"] + discovery_result["odds_rows"],
        "candidate_signals": (
            soccer_result["candidate_signals"] + discovery_result["candidate_signals"]
        ),
        "paper_eligible_signals": (
            soccer_result["paper_eligible_signals"]
            + discovery_result["paper_eligible_signals"]
        ),
        "liquidity_confirmed_signals": (
            soccer_result["liquidity_confirmed_signals"]
            + discovery_result["liquidity_confirmed_signals"]
        ),
        "paper_log": {
            "attempted": (
                soccer_result["paper_log"]["attempted"]
                + discovery_result["paper_log"]["attempted"]
            ),
            "inserted": (
                soccer_result["paper_log"]["inserted"] + discovery_result["paper_log"]["inserted"]
            ),
            "duplicates": (
                soccer_result["paper_log"]["duplicates"]
                + discovery_result["paper_log"]["duplicates"]
            ),
        },
        "settlement": _settlement_result_dict(settlement),
        "portfolio_summary": portfolio_summary,
    }
    result["summary"] = _write_latest_combined_summary(
        config,
        s3_client=s3_client,
        run_result=result,
        generated_at=now,
    )
    return result


def _empty_branch_result() -> dict[str, Any]:
    return {
        "sports": 0,
        "odds_rows": 0,
        "candidate_signals": 0,
        "paper_eligible_signals": 0,
        "liquidity_confirmed_signals": 0,
        "paper_log": {"attempted": 0, "inserted": 0, "duplicates": 0},
    }


def _combined_branch_config(
    config: StrategyRunnerConfig,
    *,
    label: str,
    sports_profile: str,
    strategy: str,
    min_edge: float,
) -> StrategyRunnerConfig:
    return replace(
        config,
        mode="paper-log",
        sports_profile=sports_profile,
        strategy=strategy,
        markets=config.markets,
        min_edge=min_edge,
        settle_finished_trades=False,
        odds_s3_prefix=f"{config.odds_s3_prefix.strip('/')}/{label}",
        summary_s3_prefix=f"{config.summary_s3_prefix.strip('/')}/{label}",
    )


def _write_latest_combined_summary(
    config: StrategyRunnerConfig,
    *,
    s3_client: Any | None,
    run_result: dict[str, Any],
    generated_at: datetime,
) -> dict[str, str | bool]:
    if not config.odds_s3_bucket:
        return {"uploaded": False, "reason": "missing_odds_s3_bucket"}
    s3_client = s3_client or _boto3_client("s3", config.aws_region)
    prefix = config.summary_s3_prefix.strip("/")
    text_key = f"{prefix}/latest_combined_strategy_runner_summary.txt"
    json_key = f"{prefix}/latest_combined_strategy_runner_summary.json"
    s3_client.put_object(
        Bucket=config.odds_s3_bucket,
        Key=text_key,
        Body=_combined_summary_text(run_result).encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    s3_client.put_object(
        Bucket=config.odds_s3_bucket,
        Key=json_key,
        Body=json.dumps(_jsonable(run_result), indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return {
        "uploaded": True,
        "bucket": config.odds_s3_bucket,
        "text_key": text_key,
        "json_key": json_key,
        "generated_at": generated_at.isoformat(),
    }


def build_portfolio_summary(table: Any, *, generated_at: datetime) -> dict[str, Any]:
    trades = list_all_trades(table)
    open_trades = [item for item in trades if item.get("status") == "open"]
    settled = [item for item in trades if item.get("status") == "settled"]
    clv_rows = [item for item in trades if item.get("target_clv") not in {None, ""}]
    staked = sum(_float(item.get("stake")) for item in settled)
    profit = sum(_float(item.get("profit")) for item in settled)
    wins = sum(1 for item in settled if _float(item.get("profit")) > 0)
    losses = len(settled) - wins
    beat = [item for item in clv_rows if _float(item.get("target_clv")) > 0]
    miss = [item for item in clv_rows if _float(item.get("target_clv")) < 0]
    tie = len(clv_rows) - len(beat) - len(miss)
    return {
        "generated_at": generated_at.isoformat(),
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "settled_trades": len(settled),
        "settled_won": wins,
        "settled_lost": losses,
        "settled_profit": profit,
        "settled_roi": profit / staked if staked else 0.0,
        "average_booked_odds": _average(_float(item.get("target_odds")) for item in trades),
        "average_confirmed_liquidity_at_target": _average(
            _float(item.get("available_at_or_above_target")) for item in trades
        ),
        "average_clv": _average(_float(item.get("target_clv")) for item in clv_rows),
        "clv_trades": len(clv_rows),
        "beat_closing_line": len(beat),
        "missed_closing_line": len(miss),
        "tied_closing_line": tie,
    }


def _write_latest_summary(
    config: StrategyRunnerConfig,
    *,
    s3_client: Any | None,
    run_result: dict[str, Any],
    generated_at: datetime,
) -> dict[str, str | bool]:
    if not config.odds_s3_bucket:
        return {"uploaded": False, "reason": "missing_odds_s3_bucket"}
    s3_client = s3_client or _boto3_client("s3", config.aws_region)
    prefix = config.summary_s3_prefix.strip("/")
    text_key = f"{prefix}/latest_strategy_runner_summary.txt"
    json_key = f"{prefix}/latest_strategy_runner_summary.json"
    s3_client.put_object(
        Bucket=config.odds_s3_bucket,
        Key=text_key,
        Body=_summary_text(run_result).encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    s3_client.put_object(
        Bucket=config.odds_s3_bucket,
        Key=json_key,
        Body=json.dumps(_jsonable(run_result), indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return {
        "uploaded": True,
        "bucket": config.odds_s3_bucket,
        "text_key": text_key,
        "json_key": json_key,
        "generated_at": generated_at.isoformat(),
    }


def _summary_text(result: dict[str, Any]) -> str:
    portfolio = result["portfolio_summary"]
    snapshot = result["snapshot"]
    closing = result["closing_update"]
    settlement = result["settlement"]
    paper_log = result["paper_log"]
    return "\n".join(
        [
            "Strategy Runner Summary",
            f"Generated at: {portfolio['generated_at']}",
            "",
            "Latest run",
            f"- Sports scanned: {result['sports']}",
            f"- Odds rows stored: {result['odds_rows']}",
            f"- Candidate signals: {result['candidate_signals']}",
            f"- Paper-eligible signals: {result['paper_eligible_signals']}",
            f"- Liquidity-confirmed signals: {result['liquidity_confirmed_signals']}",
            f"- New paper trades: {paper_log['inserted']}",
            f"- Duplicate paper trades: {paper_log['duplicates']}",
            f"- Closing updates: {closing['updated']}/{closing['open_trades']} open trades",
            f"- Settled this run: {settlement['settled']}",
            f"- S3 snapshot: s3://{snapshot.get('bucket', '')}/{snapshot.get('key', '')}",
            "",
            "Portfolio",
            f"- Total trades: {portfolio['total_trades']}",
            f"- Open trades: {portfolio['open_trades']}",
            f"- Settled trades: {portfolio['settled_trades']}",
            f"- Settled won/lost: {portfolio['settled_won']}/{portfolio['settled_lost']}",
            f"- Settled profit: {portfolio['settled_profit']:.2f}",
            f"- Settled ROI: {portfolio['settled_roi']:.2%}",
            f"- Average booked odds: {portfolio['average_booked_odds']:.2f}",
            (
                "- Average confirmed liquidity at target: "
                f"{portfolio['average_confirmed_liquidity_at_target']:.2f}"
            ),
            f"- Average CLV: {portfolio['average_clv']:.2%}",
            (
                "- CLV beat/miss/tie: "
                f"{portfolio['beat_closing_line']}/"
                f"{portfolio['missed_closing_line']}/"
                f"{portfolio['tied_closing_line']}"
            ),
        ]
    ) + "\n"


def _combined_summary_text(result: dict[str, Any]) -> str:
    portfolio = result["portfolio_summary"]
    paper_log = result["paper_log"]
    branches = result["branches"]
    lines = [
        "Combined Strategy Runner Summary",
        f"Generated at: {portfolio['generated_at']}",
        "",
        "Latest run",
        f"- Sports scanned: {result['sports']}",
        f"- Odds rows stored: {result['odds_rows']}",
        f"- Candidate signals: {result['candidate_signals']}",
        f"- Paper-eligible signals: {result['paper_eligible_signals']}",
        f"- Liquidity-confirmed signals: {result['liquidity_confirmed_signals']}",
        f"- New paper trades: {paper_log['inserted']}",
        f"- Duplicate paper trades: {paper_log['duplicates']}",
        f"- Settled this run: {result['settlement']['settled']}",
        "",
        "Branches",
    ]
    for label, branch in branches.items():
        branch_log = branch["paper_log"]
        lines.extend(
            [
                f"- {label}:",
                f"  sports={branch['sports']}, odds_rows={branch['odds_rows']}, "
                f"candidates={branch['candidate_signals']}, "
                f"paper_eligible={branch['paper_eligible_signals']}, "
                f"liquidity_confirmed={branch['liquidity_confirmed_signals']}, "
                f"inserted={branch_log['inserted']}, duplicates={branch_log['duplicates']}",
            ]
        )
    lines.extend(
        [
            "",
            "Portfolio",
            f"- Total trades: {portfolio['total_trades']}",
            f"- Open trades: {portfolio['open_trades']}",
            f"- Settled trades: {portfolio['settled_trades']}",
            f"- Average CLV: {portfolio['average_clv']:.2%}",
        ]
    )
    return "\n".join(lines) + "\n"


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
    max_betfair_spread_pct = (
        config.max_betfair_spread_pct
        if config.max_betfair_spread_pct is not None
        else strategy.get("max_betfair_spread_pct")
    )
    min_sharp_reference_books = strategy.get("min_sharp_reference_books", 0)
    min_betfair_fair_edge = strategy.get("min_betfair_fair_edge")
    matchbook_soccer_only_markets = strategy.get("matchbook_soccer_only_markets") or set()
    line_market_min_reference_books = strategy.get("line_market_min_reference_books", 0)
    market_min_edges = strategy.get("market_min_edges") or {}
    max_target_odds = strategy.get("max_target_odds")
    signals = find_strategy_value_opportunities(
        prices,
        strategy=strategy,
        min_edge=config.min_edge,
        max_age_seconds=config.max_age_seconds,
        min_reference_books=config.min_reference_books,
        reference_weights=strategy["reference_weights"],
        now=now,
    )
    signals = _filter_betfair_dislocation_signals(
        signals,
        max_betfair_spread_pct=max_betfair_spread_pct,
        min_sharp_reference_books=min_sharp_reference_books,
        min_betfair_fair_edge=min_betfair_fair_edge,
        matchbook_soccer_only_markets=matchbook_soccer_only_markets,
        line_market_min_reference_books=line_market_min_reference_books,
        market_min_edges=market_min_edges,
        max_target_odds=max_target_odds,
    )
    signals = _filter_signals_by_max_edge(signals, max_edge=config.max_edge)
    return _unique_bet_signals(signals)


def _find_closing_signals(
    config: StrategyRunnerConfig,
    prices,
    *,
    now: datetime,
) -> list[ValueSignal]:
    allowed_markets = {market.strip() for market in config.markets.split(",") if market.strip()}
    prices = [price for price in prices if price.market_key in allowed_markets]
    prices = _filter_prices_by_event_horizon(
        prices,
        max_event_days=config.closing_max_event_days,
        now=now,
    )
    strategy = STRATEGIES[config.strategy]
    return find_strategy_value_opportunities(
        prices,
        strategy=strategy,
        min_edge=-999,
        max_age_seconds=config.max_age_seconds,
        min_reference_books=config.min_reference_books,
        reference_weights=strategy["reference_weights"],
        now=now,
    )


def _filter_execution_signals_by_strategy_limits(
    config: StrategyRunnerConfig,
    signals: list[ValueSignal],
) -> list[ValueSignal]:
    strategy = STRATEGIES[config.strategy]
    max_target_odds = strategy.get("max_target_odds")
    if max_target_odds is None:
        return signals
    return [signal for signal in signals if signal.target_odds <= max_target_odds]


def _filter_betfair_dislocation_signals(
    signals: list[ValueSignal],
    *,
    max_betfair_spread_pct: float | None,
    min_sharp_reference_books: int = 0,
    min_betfair_fair_edge: float | None = None,
    matchbook_soccer_only_markets: set[str] | None = None,
    line_market_min_reference_books: int = 0,
    market_min_edges: dict[str, float] | None = None,
    max_target_odds: float | None = None,
) -> list[ValueSignal]:
    matchbook_soccer_only_markets = matchbook_soccer_only_markets or set()
    market_min_edges = market_min_edges or {}
    if (
        max_betfair_spread_pct is None
        and min_sharp_reference_books <= 0
        and min_betfair_fair_edge is None
        and not matchbook_soccer_only_markets
        and line_market_min_reference_books <= 0
        and not market_min_edges
        and max_target_odds is None
    ):
        return signals
    filtered = []
    for signal in signals:
        if signal.edge < market_min_edges.get(signal.market_key, float("-inf")):
            continue
        if max_target_odds is not None and signal.target_odds > max_target_odds:
            continue
        if not _allowed_by_matchbook_soccer_market_rule(signal, matchbook_soccer_only_markets):
            continue
        if signal.market_key in matchbook_soccer_only_markets:
            if len(signal.reference_bookmakers) < line_market_min_reference_books:
                continue
        else:
            if _sharp_reference_count(signal) < min_sharp_reference_books:
                continue
        if signal.target_bookmaker.casefold() not in BETFAIR_TARGET_BOOKMAKERS:
            filtered.append(signal)
            continue
        if max_betfair_spread_pct is not None and (
            signal.betfair_back_lay_spread_pct is None
            or signal.betfair_back_lay_spread_pct > max_betfair_spread_pct
        ):
            continue
        if min_betfair_fair_edge is not None and (
            signal.betfair_fair_edge is None
            or signal.betfair_fair_edge < min_betfair_fair_edge
        ):
            continue
        filtered.append(signal)
    return filtered


def _allowed_by_matchbook_soccer_market_rule(signal: ValueSignal, markets: set[str]) -> bool:
    if signal.market_key not in markets:
        return True
    return signal.target_bookmaker.casefold() == "matchbook" and signal.sport_key.startswith("soccer_")


def _paper_loggable_row(row: dict[str, str]) -> bool:
    return row.get("liquidity_status") == "available"


def _sharp_reference_count(signal: ValueSignal) -> int:
    return sum(
        1
        for bookmaker in signal.reference_bookmakers
        if bookmaker.casefold() in SHARP_REFERENCE_BOOKMAKER_TITLES
    )


def _parse_row_time(value: object) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _ensure_smarkets_client(
    config: StrategyRunnerConfig,
    smarkets_client: Any | None,
) -> Any | None:
    if smarkets_client is not None:
        return smarkets_client
    if not (
        config.smarkets_session_token
        or (config.smarkets_username and config.smarkets_password)
    ):
        return None
    return SmarketsLiquidityClient(session_token=config.smarkets_session_token)


def _keep_smarkets_session_alive(
    config: StrategyRunnerConfig,
    smarkets_client: Any | None,
) -> dict[str, Any]:
    if smarkets_client is None:
        return {"attempted": False, "status": "not_configured"}
    try:
        smarkets_client.keep_alive()
    except Exception as exc:  # noqa: BLE001 - keep the strategy run alive if auth lapses.
        keepalive_error = f"{type(exc).__name__}: {exc}"
        if not (config.smarkets_username and config.smarkets_password):
            return {
                "attempted": True,
                "status": "failed",
                "error": keepalive_error,
            }
        try:
            login_payload = smarkets_client.login(
                username=config.smarkets_username,
                password=config.smarkets_password,
            )
            smarkets_client.keep_alive()
        except Exception as login_exc:  # noqa: BLE001 - report auth failure in the summary.
            return {
                "attempted": True,
                "status": "login_failed",
                "error": f"{type(login_exc).__name__}: {login_exc}",
                "initial_error": keepalive_error,
            }
        return {
            "attempted": True,
            "status": "relogged",
            "token_stop": str(login_payload.get("stop") or ""),
            "initial_error": keepalive_error,
        }
    return {"attempted": True, "status": "ok"}


def _settle_finished_trades(
    config: StrategyRunnerConfig,
    *,
    odds_client: Any,
    table: Any,
) -> DynamoSettlementResult:
    open_trades = list_open_trades(table)
    sports = sorted({str(item["sport_key"]) for item in open_trades})
    if not sports:
        return DynamoSettlementResult(open_trades=0, matched_results=0, settled=0)
    scores_payloads = [
        odds_client.fetch_scores(sport=sport, days_from=config.scores_days_from)
        for sport in sports
    ]
    winners = h2h_winners_from_scores(scores_payloads)
    result = settle_results_in_dynamodb(table, winners)
    return DynamoSettlementResult(
        open_trades=len(open_trades),
        matched_results=result.matched_results,
        settled=result.settled,
    )


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
            output.update(_liquidity_row(unavailable_matchbook_liquidity("not_applicable")))
        else:
            match = match_matchbook_liquidity(
                events,
                event_name=row["event_name"],
                market_key=row.get("market", "h2h"),
                outcome_name=row["outcome_name"],
                target_odds=float(row["target_odds"]),
                bet_side=row.get("bet_side", "back"),
            )
            output.update(_liquidity_row(match))
        enriched.append(output)
    return enriched


def _enrich_smarkets_rows(
    config: StrategyRunnerConfig,
    rows: list[dict[str, str]],
    *,
    smarkets_client: Any | None,
    now: datetime,
) -> list[dict[str, str]]:
    if not rows or not any(row.get("target_bookmaker", "").casefold() == "smarkets" for row in rows):
        return rows
    if smarkets_client is None and not config.smarkets_session_token:
        return [
            _with_smarkets_liquidity(row, unavailable_smarkets_liquidity("not_configured"))
            if row.get("target_bookmaker", "").casefold() == "smarkets"
            else row
            for row in rows
        ]

    smarkets_client = smarkets_client or SmarketsLiquidityClient(
        session_token=config.smarkets_session_token
    )
    try:
        events = smarkets_client.fetch_football_events(
            start=now,
            end=now + timedelta(days=config.max_event_days),
        )
    except Exception:  # noqa: BLE001 - a venue API error should skip Smarkets, not the run.
        return [
            _with_smarkets_liquidity(row, unavailable_smarkets_liquidity("api_error"))
            if row.get("target_bookmaker", "").casefold() == "smarkets"
            else row
            for row in rows
        ]
    enriched = []
    for row in rows:
        if row.get("target_bookmaker", "").casefold() != "smarkets":
            enriched.append(row)
            continue
        try:
            match = match_smarkets_liquidity(
                smarkets_client,
                events,
                event_name=row["event_name"],
                commence_time=_parse_row_time(row.get("commence_time")),
                market_key=row.get("market", "h2h"),
                outcome_name=row["outcome_name"],
                target_odds=float(row["target_odds"]),
                bet_side=row.get("bet_side", "back"),
            )
        except Exception:  # noqa: BLE001 - do not paper-log unverified Smarkets liquidity.
            match = unavailable_smarkets_liquidity("api_error")
        enriched.append(_with_smarkets_liquidity(row, match))
    return enriched


def _with_smarkets_liquidity(row: dict[str, str], match) -> dict[str, str]:
    output = dict(row)
    output.update(
        {
            "matchbook_event_id": str(match.smarkets_event_id or ""),
            "matchbook_market_id": str(match.smarkets_market_id or ""),
            "matchbook_runner_id": str(match.smarkets_contract_id or ""),
            "match_score": f"{match.match_score:.4f}",
            "best_back_odds": _format_optional(match.best_back_odds),
            "best_back_available": f"{match.best_back_available:.2f}",
            "available_at_or_above_target": f"{match.available_at_or_above_target:.2f}",
            "best_lay_odds": _format_optional(match.best_lay_odds),
            "best_lay_available": f"{match.best_lay_available:.2f}",
            "back_lay_spread_pct": (
                f"{match.back_lay_spread_pct:.4f}"
                if match.back_lay_spread_pct is not None
                else ""
            ),
            "liquidity_status": match.liquidity_status,
        }
    )
    return output


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
        "bet_side": signal.bet_side,
        "bet_to_place": (
            f"{signal.bet_side.title()} {signal.outcome_name} with "
            f"{signal.target_bookmaker} at {signal.target_odds:g}"
        ),
        "target_bookmaker": signal.target_bookmaker,
        "target_odds": f"{signal.target_odds:.4f}",
        "target_effective_odds": f"{signal.effective_odds:.4f}",
        "reference_fair_odds": f"{signal.reference_fair_odds:.4f}",
        "reference_probability": f"{signal.reference_probability:.4f}",
        "edge": f"{signal.edge:.4f}",
        "reference_bookmakers": ", ".join(signal.reference_bookmakers),
        "betfair_fair_odds": _format_optional(signal.betfair_fair_odds),
        "betfair_fair_edge": _format_optional(signal.betfair_fair_edge),
        "betfair_back_lay_spread_pct": _format_optional(signal.betfair_back_lay_spread_pct),
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


def _row_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        row["event_id"].casefold(),
        row.get("market", "h2h").casefold(),
        row["outcome_name"].casefold(),
        row["target_bookmaker"].casefold(),
        row.get("bet_side", "back").casefold(),
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


def _sports_for_profile(
    sports_profile: str,
    odds_client: Any,
    *,
    filter_inactive_sports: bool = True,
) -> list[str]:
    if sports_profile == ACTIVE_H2H_PROFILE:
        return active_h2h_sports(odds_client.fetch_sports())
    if sports_profile == MATCHBOOK_DISCOVERY_H2H_PROFILE:
        return _matchbook_discovery_sports(odds_client.fetch_sports())
    sports = list(SPORT_PROFILES[sports_profile])
    if not filter_inactive_sports:
        return sports
    try:
        active_sports = set(active_h2h_sports(odds_client.fetch_sports()))
    except Exception:
        return sports
    return [sport for sport in sports if sport in active_sports]


def _matchbook_discovery_sports(sports_payload: list[dict[str, object]]) -> list[str]:
    sports = []
    for sport in active_h2h_sports(sports_payload):
        if sport in MATCHBOOK_DISCOVERY_SPORT_KEYS or sport.startswith(
            MATCHBOOK_DISCOVERY_SPORT_PREFIXES
        ):
            sports.append(sport)
    return sports


def _log_result_dict(result: DynamoPaperLogResult) -> dict[str, int]:
    return {
        "attempted": result.attempted,
        "inserted": result.inserted,
        "duplicates": result.duplicates,
    }


def _closing_result_dict(result: DynamoClosingUpdateResult) -> dict[str, int]:
    return {
        "open_trades": result.open_trades,
        "matched": result.matched,
        "updated": result.updated,
    }


def _settlement_result_dict(result: DynamoSettlementResult) -> dict[str, int]:
    return {
        "open_trades": result.open_trades,
        "matched_results": result.matched_results,
        "settled": result.settled,
    }


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    return float(value)


def _average(values) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _format_optional(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {"0", "false", "no", "off", ""}
