from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from dotenv import load_dotenv

from exchange_scanner.backtest import BacktestBet, backtest_summary, run_backtest
from exchange_scanner.bookmaker_links import EventPageResolution, resolve_event_page
from exchange_scanner.paper import (
    PaperTrade,
    list_trades,
    log_signals,
    paper_summary,
    settle_results,
    update_closing_values,
)
from exchange_scanner.sharpness import (
    recompute_sharpness_weights,
    sharpness_weight_mapping,
    store_odds_snapshot,
    write_sharpness_weights_csv,
)
from exchange_scanner.the_odds_api import (
    MATCHBOOK_COMMISSION_RATE,
    TheOddsApiClient,
    find_value_opportunities,
    h2h_winners_from_scores,
    normalise_odds_api_events,
)

SHARP_REFERENCE_BOOKMAKERS = {
    "pinnacle",
    "betfair",
    "smarkets",
    "matchbook",
}

MATCHBOOK_TARGET_BOOKMAKERS = {
    "matchbook",
}

BETFAIR_TARGET_BOOKMAKERS = {
    "betfair",
    "betfair_ex_uk",
    "betfair_ex_eu",
}

EXCHANGE_CLV_TARGET_BOOKMAKERS = {
    "matchbook",
    "smarkets",
    *BETFAIR_TARGET_BOOKMAKERS,
}

SHARPNESS_WEIGHTS = {
    "*": 0.20,
    "pinnacle": 1.00,
    "betfair": 1.00,
    "smarkets": 0.90,
    "matchbook": 0.90,
    "bet365": 0.50,
    "betfair sportsbook": 0.45,
    "william hill": 0.40,
    "sky bet": 0.40,
    "paddy power": 0.40,
    "bet victor": 0.40,
    "betway": 0.35,
    "boylesports": 0.35,
    "coral": 0.35,
    "ladbrokes": 0.35,
    "unibet (uk)": 0.35,
    "grosvenor": 0.30,
    "livescore bet": 0.30,
    "virgin bet": 0.30,
    "888sport": 0.30,
}

MATCHBOOK_COMMISSION_RATES = {
    "matchbook": MATCHBOOK_COMMISSION_RATE,
}

EXCHANGE_CLV_COMMISSION_RATES = {
    "matchbook": MATCHBOOK_COMMISSION_RATE,
    "smarkets": 0.02,
    "betfair": 0.02,
    "betfair_ex_uk": 0.02,
    "betfair_ex_eu": 0.02,
}

STRATEGIES = {
    "sharp-weighted-clv": {
        "target_bookmakers": MATCHBOOK_TARGET_BOOKMAKERS,
        "reference_bookmakers": None,
        "allow_target_bookmakers_as_references": True,
        "reference_weights": SHARPNESS_WEIGHTS,
        "target_commission_rates": MATCHBOOK_COMMISSION_RATES,
    },
    "exchange-clv": {
        "target_bookmakers": EXCHANGE_CLV_TARGET_BOOKMAKERS,
        "reference_bookmakers": SHARP_REFERENCE_BOOKMAKERS,
        "allow_target_bookmakers_as_references": True,
        "reference_weights": None,
        "target_commission_rates": EXCHANGE_CLV_COMMISSION_RATES,
    },
}

SPORT_PROFILES = {
    "matchbook-h2h-expanded": [
        "americanfootball_cfl",
        "americanfootball_ncaaf",
        "americanfootball_nfl",
        "americanfootball_nfl_preseason",
        "aussierules_afl",
        "aussierules_aflw",
        "baseball_kbo",
        "baseball_milb",
        "baseball_mlb",
        "baseball_npb",
        "basketball_nba",
        "basketball_wnba",
        "boxing_boxing",
        "cricket_caribbean_premier_league",
        "cricket_international_t20",
        "cricket_odi",
        "cricket_test_match",
        "cricket_the_hundred",
        "cricket_the_hundred_womens",
        "icehockey_nhl",
        "lacrosse_pll",
        "mma_mixed_martial_arts",
        "rugbyleague_nrl",
        "rugbyleague_nrlw",
        "soccer_argentina_primera_division",
        "soccer_austria_bundesliga",
        "soccer_belgium_first_div",
        "soccer_brazil_campeonato",
        "soccer_brazil_serie_b",
        "soccer_chile_campeonato",
        "soccer_china_superleague",
        "soccer_concacaf_leagues_cup",
        "soccer_conmebol_copa_libertadores",
        "soccer_conmebol_copa_sudamericana",
        "soccer_denmark_superliga",
        "soccer_efl_champ",
        "soccer_england_efl_cup",
        "soccer_england_league1",
        "soccer_england_league2",
        "soccer_epl",
        "soccer_finland_veikkausliiga",
        "soccer_france_ligue_one",
        "soccer_france_ligue_two",
        "soccer_germany_bundesliga",
        "soccer_germany_bundesliga2",
        "soccer_germany_dfb_pokal",
        "soccer_germany_liga3",
        "soccer_greece_super_league",
        "soccer_italy_coppa_italia",
        "soccer_italy_serie_a",
        "soccer_italy_serie_b",
        "soccer_japan_j_league",
        "soccer_korea_kleague1",
        "soccer_league_of_ireland",
        "soccer_mexico_ligamx",
        "soccer_netherlands_eredivisie",
        "soccer_norway_eliteserien",
        "soccer_poland_ekstraklasa",
        "soccer_portugal_primeira_liga",
        "soccer_russia_premier_league",
        "soccer_saudi_arabia_pro_league",
        "soccer_spain_la_liga",
        "soccer_spain_segunda_division",
        "soccer_spl",
        "soccer_sweden_allsvenskan",
        "soccer_sweden_superettan",
        "soccer_switzerland_superleague",
        "soccer_turkey_super_league",
        "soccer_uefa_champs_league_qualification",
        "soccer_uefa_nations_league",
        "soccer_usa_mls",
        "tennis_atp_canadian_open",
        "tennis_atp_cincinnati_open",
        "tennis_wta_canadian_open",
        "tennis_wta_cincinnati_open",
    ],
}


def main() -> None:
    load_dotenv()
    args = parse_args()

    if args.backtest:
        bets = backtest(args)
        if args.output == "json":
            print(
                json.dumps(
                    {"summary": backtest_summary(bets), "bets": [_backtest_row(bet) for bet in bets]},
                    indent=2,
                )
            )
        else:
            write_backtest_csv(bets)
        return

    if args.paper_export:
        write_paper_csv(list_trades(args.paper_db, status=args.paper_status or None))
        return

    if args.paper_settle_results:
        settled = settle_paper_results(args)
        print(f"Settled {settled} paper trades.", file=sys.stderr)
        write_paper_csv(list_trades(args.paper_db))
        return

    if args.recompute_sharpness_weights:
        weights = recompute_sharpness_weights(
            args.market_db,
            benchmark_bookmakers=SHARP_REFERENCE_BOOKMAKERS,
            min_samples=args.sharpness_min_samples,
        )
        write_sharpness_weights_csv(weights, args.sharpness_weights_csv)
        print(f"Recomputed {len(weights)} sharpness weights.", file=sys.stderr)
        return

    signals = scan_the_odds_api(args)
    if args.paper_update_closing:
        updated = update_closing_values(args.paper_db, signals)
        print(f"Updated closing values for {updated} paper trades.", file=sys.stderr)
        write_paper_csv(list_trades(args.paper_db))
        return

    if args.paper_log:
        inserted = log_signals(args.paper_db, signals, stake=args.paper_stake)
        if args.paper_inserted_count_out:
            args.paper_inserted_count_out.write_text(f"{inserted}\n", encoding="utf-8")
        print(f"Logged {inserted} new paper trades.", file=sys.stderr)

    if args.output == "json":
        print(json.dumps([signal.as_dict() for signal in signals], indent=2))
    else:
        write_value_csv(signals, args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find value bets against sharper reference prices."
    )
    parser.add_argument(
        "--strategy",
        choices=sorted(STRATEGIES),
        default="sharp-weighted-clv",
        help="Value strategy to run. Default targets Matchbook against weighted reference prices.",
    )
    parser.add_argument("--fixtures", type=Path, help="Read quotes from a local JSON fixture.")
    parser.add_argument(
        "--paper-db",
        type=Path,
        default=Path("paper_trades.sqlite3"),
        help="SQLite database for paper-trade logging.",
    )
    parser.add_argument(
        "--paper-log",
        action="store_true",
        help="Log current scan candidates as paper trades, ignoring events already logged.",
    )
    parser.add_argument(
        "--paper-stake",
        type=float,
        default=1.0,
        help="Flat paper stake stored for each logged trade.",
    )
    parser.add_argument(
        "--paper-inserted-count-out",
        type=Path,
        help="Optional file path to write the number of newly inserted paper trades.",
    )
    parser.add_argument(
        "--paper-export",
        action="store_true",
        help="Export logged paper trades as CSV.",
    )
    parser.add_argument(
        "--paper-status",
        default="",
        help="Optional paper export status filter, for example open.",
    )
    parser.add_argument(
        "--paper-update-closing",
        action="store_true",
        help="Scan current odds and update closing values for matching open paper trades.",
    )
    parser.add_argument(
        "--paper-settle-results",
        action="store_true",
        help="Fetch recent scores and settle matching open paper trades.",
    )
    parser.add_argument(
        "--scores-days-from",
        type=int,
        default=3,
        help="Days of recent completed games to request from The Odds API scores endpoint.",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run the value strategy against historical odds and settled results.",
    )
    parser.add_argument(
        "--historical-odds",
        type=Path,
        help="Historical The Odds API-style JSON/JSONL file or directory.",
    )
    parser.add_argument(
        "--results",
        type=Path,
        help="Settled results CSV/JSON with event_id, optional market, and winner columns.",
    )
    parser.add_argument(
        "--backtest-stake",
        type=float,
        default=1.0,
        help="Flat stake per backtest bet.",
    )
    parser.add_argument(
        "--backtest-daily-time",
        default="22:00",
        help="UTC HH:MM decision time for once-per-day backtests. Empty means use every snapshot.",
    )
    parser.add_argument(
        "--allow-rebet-same-event",
        action="store_true",
        help="Allow backtests to place another bet on an event already selected earlier.",
    )
    parser.add_argument("--sport", default="soccer_efl_champ")
    parser.add_argument(
        "--sports",
        default="",
        help="Comma-separated The Odds API sport keys. Overrides --sport.",
    )
    parser.add_argument(
        "--sports-profile",
        choices=sorted(SPORT_PROFILES),
        default="matchbook-h2h-expanded",
        help="Named sports profile to scan. Combines with --sports.",
    )
    parser.add_argument(
        "--markets",
        default="h2h",
        help="The Odds API market keys, comma-separated. Example: h2h,spreads,totals",
    )
    parser.add_argument("--regions", default="uk,eu", help="The Odds API regions, comma-separated.")
    parser.add_argument(
        "--min-reference-books",
        type=int,
        default=3,
        help="Minimum reference bookmakers required per outcome for value mode.",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=120,
        help="Ignore The Odds API bookmaker prices older than this.",
    )
    parser.add_argument(
        "--include-started",
        action="store_true",
        help="Include events that have already commenced in The Odds API scans.",
    )
    parser.add_argument(
        "--max-event-days",
        type=float,
        default=2.0,
        help="For The Odds API scans, ignore events commencing more than this many days from now.",
    )
    parser.add_argument(
        "--unique-events",
        action="store_true",
        help="For value mode, return only the highest-edge signal per event.",
    )
    parser.add_argument(
        "--unique-bets",
        action="store_true",
        help="For value mode, return only the best price per event/market/outcome.",
    )
    parser.add_argument(
        "--max-api-requests",
        type=int,
        default=int(os.getenv("ODDS_API_MAX_REQUESTS", "25")),
        help="Abort The Odds API scans that would exceed this many odds requests.",
    )
    parser.add_argument(
        "--dry-run-estimate",
        action="store_true",
        help="Print the planned The Odds API request count and exit without fetching odds.",
    )
    parser.add_argument(
        "--odds-cache-dir",
        type=Path,
        default=None,
        help="Optional cache directory for The Odds API responses.",
    )
    parser.add_argument(
        "--odds-cache-ttl-seconds",
        type=int,
        default=0,
        help="Reuse cached Odds API responses younger than this many seconds.",
    )
    parser.add_argument(
        "--market-db",
        type=Path,
        default=Path("data/market_snapshots.sqlite3"),
        help="SQLite database for raw odds snapshots and learned sharpness weights.",
    )
    parser.add_argument(
        "--store-odds-snapshot",
        action="store_true",
        help="Store normalized odds rows from this scan in --market-db.",
    )
    parser.add_argument(
        "--recompute-sharpness-weights",
        action="store_true",
        help="Recompute bookmaker sharpness weights from stored odds snapshots.",
    )
    parser.add_argument(
        "--sharpness-weights-csv",
        type=Path,
        default=Path("data/bookmaker_sharpness_weights.csv"),
        help="CSV path for exported learned sharpness weights.",
    )
    parser.add_argument(
        "--sharpness-min-samples",
        type=int,
        default=25,
        help="Minimum bookmaker/outcome samples required to publish a learned weight.",
    )
    parser.add_argument(
        "--use-learned-sharpness-weights",
        action="store_true",
        help="Use learned weights from --market-db for weighted CLV scans when available.",
    )
    parser.add_argument("--min-edge", type=float, default=float(os.getenv("MIN_EDGE", "0.02")))
    parser.add_argument(
        "--max-edge",
        type=float,
        default=float(os.getenv("MAX_EDGE", "0.10")),
        help="Maximum value edge to book. Use a negative value to disable the cap.",
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=float(os.getenv("BANKROLL", "0")),
        help="Optional bankroll for value-mode stake recommendations.",
    )
    parser.add_argument(
        "--kelly-fraction",
        type=float,
        default=float(os.getenv("KELLY_FRACTION", "0.25")),
        help="Fraction of full Kelly to use for value-mode stake recommendations.",
    )
    parser.add_argument(
        "--stake-cap-pct",
        type=float,
        default=float(os.getenv("STAKE_CAP_PCT", "0.005")),
        help="Maximum value-mode stake as a fraction of bankroll.",
    )
    parser.add_argument(
        "--max-stake",
        type=float,
        default=float(os.getenv("MAX_STAKE", "50")),
        help="Maximum value-mode stake recommendation.",
    )
    parser.add_argument(
        "--resolve-event-pages",
        action="store_true",
        help="For value mode, try to resolve exact bookmaker event page URLs.",
    )
    parser.add_argument("--max-results", type=int, default=50)
    parser.add_argument("--output", choices=["csv", "json"], default="csv")
    return parser.parse_args()


def backtest(args: argparse.Namespace) -> list[BacktestBet]:
    if not args.historical_odds:
        raise SystemExit("--backtest requires --historical-odds")
    if not args.results:
        raise SystemExit("--backtest requires --results")

    allowed_markets = {market.strip() for market in args.markets.split(",") if market.strip()}
    strategy = _strategy_config(args)
    return run_backtest(
        historical_odds_path=args.historical_odds,
        results_path=args.results,
        target_bookmakers=strategy["target_bookmakers"],
        reference_bookmakers=strategy["reference_bookmakers"],
        markets=allowed_markets,
        min_edge=args.min_edge,
        max_age_seconds=args.max_age_seconds,
        min_reference_books=args.min_reference_books,
        include_started=args.include_started,
        max_event_days=args.max_event_days,
        unique_events=args.unique_events,
        stake=args.backtest_stake,
        daily_decision_time=args.backtest_daily_time or None,
        allow_rebet_same_event=args.allow_rebet_same_event,
        allow_target_bookmakers_as_references=strategy[
            "allow_target_bookmakers_as_references"
        ],
        reference_weights=strategy["reference_weights"],
        target_commission_rates=strategy["target_commission_rates"],
        max_betfair_spread_pct=strategy.get("max_betfair_spread_pct"),
    )


def settle_paper_results(args: argparse.Namespace) -> int:
    api_key = os.getenv("THE_ODDS_API_KEY")
    if not api_key:
        raise SystemExit("Missing required environment variable: THE_ODDS_API_KEY")
    open_trades = list_trades(args.paper_db, status="open")
    sports = sorted({trade.sport_key for trade in open_trades})
    if not sports:
        return 0
    client = TheOddsApiClient(api_key=api_key)
    scores_payloads = [
        client.fetch_scores(sport=sport, days_from=args.scores_days_from) for sport in sports
    ]
    winners = h2h_winners_from_scores(scores_payloads)
    return settle_results(args.paper_db, winners)


def scan_the_odds_api(args: argparse.Namespace):
    if args.fixtures:
        events = json.loads(args.fixtures.read_text())
    else:
        sport_keys = _sport_keys(args)
        planned_requests = len(sport_keys)
        if args.dry_run_estimate:
            print(
                json.dumps(
                    {
                        "provider": "the-odds-api",
                        "planned_odds_requests": planned_requests,
                        "sports": sport_keys,
                        "markets": args.markets,
                        "regions": args.regions,
                    },
                    indent=2,
                )
            )
            raise SystemExit(0)

        api_key = os.getenv("THE_ODDS_API_KEY")
        if not api_key:
            raise SystemExit("Missing required environment variable: THE_ODDS_API_KEY")

        if planned_requests > args.max_api_requests:
            raise SystemExit(
                f"Refusing to make {planned_requests} The Odds API requests; "
                f"--max-api-requests is {args.max_api_requests}. "
                "Use --dry-run-estimate to inspect the plan or raise the cap intentionally."
            )

        client = TheOddsApiClient(
            api_key=api_key,
            cache_dir=args.odds_cache_dir,
            cache_ttl_seconds=args.odds_cache_ttl_seconds,
        )
        events = []
        for sport in sport_keys:
            events.extend(
                client.fetch_odds(
                    sport=sport,
                    regions=args.regions,
                    markets=args.markets,
                )
            )
    prices = normalise_odds_api_events(events)
    if getattr(args, "store_odds_snapshot", False):
        inserted = store_odds_snapshot(args.market_db, prices)
        print(f"Stored {inserted} odds snapshot rows.", file=sys.stderr)
    allowed_markets = {market.strip() for market in args.markets.split(",") if market.strip()}
    prices = [price for price in prices if price.market_key in allowed_markets]
    prices = _filter_prices_by_event_horizon(
        prices,
        max_event_days=getattr(args, "max_event_days", 2.0),
    )

    strategy = _strategy_config(args)
    reference_weights = _reference_weights_for_scan(args, strategy, allowed_markets)
    signals = find_value_opportunities(
        prices,
        target_bookmakers=strategy["target_bookmakers"],
        reference_bookmakers=strategy["reference_bookmakers"],
        min_edge=args.min_edge,
        max_age_seconds=args.max_age_seconds,
        min_reference_books=args.min_reference_books,
        include_started=args.include_started,
        allow_target_bookmakers_as_references=strategy["allow_target_bookmakers_as_references"],
        reference_weights=reference_weights,
        target_commission_rates=strategy["target_commission_rates"],
    )
    signals = _filter_signals_by_strategy_rules(signals, strategy)
    if not getattr(args, "paper_update_closing", False):
        signals = _filter_signals_by_max_edge(signals, max_edge=getattr(args, "max_edge", 0.10))
    if getattr(args, "unique_events", False):
        return _unique_event_signals(signals)
    if getattr(args, "unique_bets", False):
        return _unique_bet_signals(signals)
    return signals


def _strategy_config(args: argparse.Namespace):
    return STRATEGIES[getattr(args, "strategy", "sharp-weighted-clv")]


def _reference_weights_for_scan(args: argparse.Namespace, strategy, allowed_markets: set[str]):
    if not getattr(args, "use_learned_sharpness_weights", False):
        return strategy["reference_weights"]
    learned_weights = sharpness_weight_mapping(
        args.market_db,
        sport_keys=set(_sport_keys(args)),
        market_keys=allowed_markets,
    )
    if not learned_weights:
        return strategy["reference_weights"]
    merged_weights = dict(strategy["reference_weights"] or {"*": 0.20})
    merged_weights.update(learned_weights)
    return merged_weights


def _filter_signals_by_strategy_rules(signals, strategy):
    max_betfair_spread_pct = strategy.get("max_betfair_spread_pct")
    if max_betfair_spread_pct is None:
        return signals
    output = []
    for signal in signals:
        if not _is_betfair_signal(signal):
            output.append(signal)
            continue
        if max_betfair_spread_pct is not None and (
            signal.betfair_back_lay_spread_pct is None
            or signal.betfair_back_lay_spread_pct > max_betfair_spread_pct
        ):
            continue
        output.append(signal)
    return output


def _is_betfair_signal(signal) -> bool:
    return signal.target_bookmaker.casefold() in BETFAIR_TARGET_BOOKMAKERS


def _filter_signals_by_max_edge(signals, *, max_edge: float):
    if max_edge < 0:
        return signals
    return [signal for signal in signals if signal.edge <= max_edge]


def _unique_event_signals(signals):
    best_by_event = {}
    for signal in signals:
        key = (signal.sport_key, signal.event_name, signal.commence_time)
        if key not in best_by_event:
            best_by_event[key] = signal
    return list(best_by_event.values())


def _unique_bet_signals(signals):
    best_by_bet = {}
    for signal in signals:
        key = (signal.event_id, signal.market_key, signal.outcome_name.casefold())
        existing = best_by_bet.get(key)
        if existing is None or _better_signal_price(signal, existing):
            best_by_bet[key] = signal
    return sorted(best_by_bet.values(), key=lambda signal: signal.edge, reverse=True)


def _better_signal_price(candidate, existing) -> bool:
    if candidate.effective_odds != existing.effective_odds:
        return candidate.effective_odds > existing.effective_odds
    return candidate.edge > existing.edge


def _filter_prices_by_event_horizon(prices, *, max_event_days: float, now: datetime | None = None):
    if max_event_days < 0:
        return prices
    now = now or datetime.now(timezone.utc)
    latest_commence_time = now + timedelta(days=max_event_days)
    return [price for price in prices if price.commence_time <= latest_commence_time]


def _sport_keys(args: argparse.Namespace) -> list[str]:
    sports = []
    if getattr(args, "sports_profile", ""):
        sports.extend(SPORT_PROFILES[args.sports_profile])
    if args.sports:
        sports.extend(sport.strip() for sport in args.sports.split(",") if sport.strip())
    if not sports:
        sports.append(args.sport)
    return list(dict.fromkeys(sports))


def write_value_csv(signals, args) -> None:
    fieldnames = [
        "sport_key",
        "event_id",
        "event_name",
        "commence_time",
        "market",
        "outcome_name",
        "bet_to_place",
        "target_bookmaker",
        "target_odds",
        "target_effective_odds",
        "target_odds_fractional",
        "target_odds_american",
        "target_implied_probability",
        "reference_fair_odds",
        "reference_fair_odds_fractional",
        "reference_fair_odds_american",
        "reference_fair_implied_probability",
        "reference_probability",
        "edge",
        "reference_bookmakers",
        "target_bookmaker_url",
        "event_search_url",
        "event_page_url",
        "event_page_status",
        "event_page_reason",
        "copy_search",
        "copy_bet_instruction",
        "min_acceptable_odds",
        "recommended_stake",
        "staking_method",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for signal in signals:
        resolution = _resolve_value_event_page(signal, args)
        row = signal.as_dict()
        row["edge"] = f"{row['edge']:.4f}"
        row["target_effective_odds"] = f"{row['target_effective_odds']:.4f}"
        row["reference_fair_odds"] = f"{row['reference_fair_odds']:.4f}"
        row["reference_probability"] = f"{row['reference_probability']:.4f}"
        row["min_acceptable_odds"] = f"{row['min_acceptable_odds']:.4f}"
        row.update(_odds_format_columns("target_odds", signal.target_odds))
        row.update(_odds_format_columns("reference_fair_odds", signal.reference_fair_odds))
        row["bet_to_place"] = _bet_to_place(signal)
        row["event_page_url"] = resolution.url or row["target_bookmaker_url"]
        row["event_page_status"] = resolution.status
        row["event_page_reason"] = resolution.reason
        stake = _recommended_value_stake(signal, args)
        row["recommended_stake"] = f"{stake:.2f}" if stake is not None else ""
        row["staking_method"] = _staking_method(args) if stake is not None else ""
        writer.writerow(row)


def write_backtest_csv(bets: list[BacktestBet]) -> None:
    fieldnames = [
        "snapshot_time",
        "sport_key",
        "event_id",
        "event_name",
        "commence_time",
        "market",
        "outcome_name",
        "bet_to_place",
        "target_bookmaker",
        "target_odds",
        "target_odds_fractional",
        "target_odds_american",
        "reference_fair_odds",
        "edge",
        "reference_bookmakers",
        "betfair_fair_odds",
        "betfair_fair_edge",
        "betfair_back_lay_spread_pct",
        "stake",
        "result",
        "won",
        "profit",
        "closing_snapshot_time",
        "closing_target_odds",
        "target_clv",
        "beat_closing_line",
        "closing_reference_fair_odds",
        "closing_fair_edge",
        "positive_closing_fair_edge",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for bet in bets:
        writer.writerow(_backtest_row(bet))

    summary = backtest_summary(bets)
    print(
        "Backtest summary: "
        f"bets={summary['bets']} "
        f"wins={summary['wins']} "
        f"profit={summary['profit']:.2f} "
        f"roi={summary['roi']:.2%} "
        f"beat_closing_line={summary['beat_closing_line_rate']:.2%} "
        f"avg_clv={summary['average_target_clv']:.2%} "
        f"positive_closing_fair_edge={summary['positive_closing_fair_edge_rate']:.2%}",
        file=sys.stderr,
    )


def _backtest_row(bet: BacktestBet) -> dict[str, str | float | bool]:
    signal = bet.signal
    row = signal.as_dict()
    row.update(_odds_format_columns("target_odds", signal.target_odds))
    row["snapshot_time"] = bet.snapshot_time.isoformat()
    row["commence_time"] = signal.commence_time.isoformat()
    row["bet_to_place"] = _bet_to_place(signal)
    row["reference_fair_odds"] = f"{signal.reference_fair_odds:.4f}"
    row["edge"] = f"{signal.edge:.4f}"
    row["stake"] = f"{bet.stake:.2f}"
    row["result"] = bet.result
    row["won"] = bet.won
    row["profit"] = f"{bet.profit:.2f}"
    row["closing_snapshot_time"] = (
        bet.closing_snapshot_time.isoformat() if bet.closing_snapshot_time else ""
    )
    row["closing_target_odds"] = (
        f"{bet.closing_target_odds:.4f}" if bet.closing_target_odds else ""
    )
    row["target_clv"] = f"{bet.target_clv:.4f}" if bet.target_clv is not None else ""
    row["beat_closing_line"] = bet.target_clv > 0 if bet.target_clv is not None else ""
    row["closing_reference_fair_odds"] = (
        f"{bet.closing_reference_fair_odds:.4f}" if bet.closing_reference_fair_odds else ""
    )
    row["closing_fair_edge"] = (
        f"{bet.closing_fair_edge:.4f}" if bet.closing_fair_edge is not None else ""
    )
    row["positive_closing_fair_edge"] = (
        bet.closing_fair_edge > 0 if bet.closing_fair_edge is not None else ""
    )
    return row


def write_paper_csv(trades: list[PaperTrade]) -> None:
    fieldnames = [
        "id",
        "logged_at",
        "sport_key",
        "event_id",
        "event_name",
        "commence_time",
        "market",
        "outcome_name",
        "bet_to_place",
        "target_bookmaker",
        "target_odds",
        "target_odds_fractional",
        "target_odds_american",
        "reference_fair_odds",
        "edge",
        "reference_bookmakers",
        "stake",
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
        "status",
        "closing_checked_at",
        "closing_target_odds",
        "target_clv",
        "beat_closing_line",
        "closing_reference_fair_odds",
        "closing_edge",
        "positive_closing_edge",
        "result",
        "profit",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for trade in trades:
        writer.writerow(_paper_row(trade))

    summary = paper_summary(trades)
    print(
        "Paper summary: "
        f"trades={summary['trades']} "
        f"open={summary['open']} "
        f"settled={summary['settled']} "
        f"settled_profit={summary['settled_profit']:.2f} "
        f"settled_roi={summary['settled_roi']:.2%} "
        f"closing_checked={summary['closing_checked']} "
        f"beat_closing_line={summary['beat_closing_line_rate']:.2%} "
        f"avg_clv={summary['average_target_clv']:.2%} "
        f"positive_closing_edge={summary['positive_closing_edge_rate']:.2%}",
        file=sys.stderr,
    )


def _paper_row(trade: PaperTrade) -> dict[str, str | float | int | bool]:
    return {
        "id": trade.id,
        "logged_at": trade.logged_at.isoformat(),
        "sport_key": trade.sport_key,
        "event_id": trade.event_id,
        "event_name": trade.event_name,
        "commence_time": trade.commence_time.isoformat(),
        "market": trade.market_key,
        "outcome_name": trade.outcome_name,
        "bet_to_place": (
            f"Back {trade.outcome_name} with {trade.target_bookmaker} "
            f"at {trade.target_odds:g} ({_fractional_odds(trade.target_odds)})+"
        ),
        "target_bookmaker": trade.target_bookmaker,
        "target_odds": f"{trade.target_odds:.4f}",
        "target_odds_fractional": _fractional_odds(trade.target_odds),
        "target_odds_american": _american_odds(trade.target_odds),
        "reference_fair_odds": f"{trade.reference_fair_odds:.4f}",
        "edge": f"{trade.edge:.4f}",
        "reference_bookmakers": trade.reference_bookmakers,
        "betfair_fair_odds": _format_optional_number(trade.betfair_fair_odds, decimals=4),
        "betfair_fair_edge": _format_optional_number(trade.betfair_fair_edge, decimals=4),
        "betfair_back_lay_spread_pct": _format_optional_number(
            trade.betfair_back_lay_spread_pct,
            decimals=4,
        ),
        "stake": f"{trade.stake:.2f}",
        "matchbook_event_id": trade.matchbook_event_id or "",
        "matchbook_market_id": trade.matchbook_market_id or "",
        "matchbook_runner_id": trade.matchbook_runner_id or "",
        "liquidity_status": trade.liquidity_status or "",
        "available_at_or_above_target": _format_optional_number(
            trade.available_at_or_above_target
        ),
        "best_back_odds": _format_optional_number(trade.best_back_odds, decimals=4),
        "best_back_available": _format_optional_number(trade.best_back_available),
        "best_lay_odds": _format_optional_number(trade.best_lay_odds, decimals=4),
        "best_lay_available": _format_optional_number(trade.best_lay_available),
        "back_lay_spread_pct": _format_optional_number(
            trade.back_lay_spread_pct,
            decimals=4,
        ),
        "status": trade.status,
        "closing_checked_at": (
            trade.closing_checked_at.isoformat() if trade.closing_checked_at else ""
        ),
        "closing_target_odds": (
            f"{trade.closing_target_odds:.4f}" if trade.closing_target_odds else ""
        ),
        "target_clv": f"{trade.target_clv:.4f}" if trade.target_clv is not None else "",
        "beat_closing_line": trade.target_clv > 0 if trade.target_clv is not None else "",
        "closing_reference_fair_odds": (
            f"{trade.closing_reference_fair_odds:.4f}"
            if trade.closing_reference_fair_odds
            else ""
        ),
        "closing_edge": f"{trade.closing_edge:.4f}" if trade.closing_edge is not None else "",
        "positive_closing_edge": trade.closing_edge > 0 if trade.closing_edge is not None else "",
        "result": trade.result or "",
        "profit": f"{trade.profit:.2f}" if trade.profit is not None else "",
    }


def _format_optional_number(value: float | None, *, decimals: int = 2) -> str:
    return f"{value:.{decimals}f}" if value is not None else ""


def _recommended_value_stake(signal, args) -> float | None:
    if args.bankroll <= 0 or signal.target_odds <= 1:
        return None
    full_kelly_fraction = signal.edge / (signal.effective_odds - 1)
    if full_kelly_fraction <= 0:
        return None
    fractional_kelly = args.bankroll * full_kelly_fraction * args.kelly_fraction
    bankroll_cap = args.bankroll * args.stake_cap_pct
    return max(0.0, min(fractional_kelly, bankroll_cap, args.max_stake))


def _bet_to_place(signal) -> str:
    return (
        f"Back {signal.outcome_name} with {signal.target_bookmaker} "
        f"at {signal.target_odds:g} ({_fractional_odds(signal.target_odds)})+"
    )


def _odds_format_columns(prefix: str, decimal_odds: float) -> dict[str, str]:
    return {
        f"{prefix}_fractional": _fractional_odds(decimal_odds),
        f"{prefix}_american": _american_odds(decimal_odds),
        f"{prefix.removesuffix('_odds')}_implied_probability": f"{1 / decimal_odds:.2%}",
    }


def _fractional_odds(decimal_odds: float) -> str:
    if decimal_odds <= 1:
        return ""
    fraction = Fraction(decimal_odds - 1).limit_denominator(100)
    return f"{fraction.numerator}/{fraction.denominator}"


def _american_odds(decimal_odds: float) -> str:
    if decimal_odds <= 1:
        return ""
    if decimal_odds >= 2:
        return f"+{round((decimal_odds - 1) * 100):g}"
    return f"-{round(100 / (decimal_odds - 1)):g}"


def _resolve_value_event_page(signal, args):
    if not getattr(args, "resolve_event_pages", False):
        return EventPageResolution("")
    return resolve_event_page(
        bookmaker=signal.target_bookmaker,
        event_name=signal.event_name,
        selection=signal.outcome_name,
    )


def _staking_method(args) -> str:
    return (
        f"{args.kelly_fraction:g} Kelly, capped at "
        f"{args.stake_cap_pct:.2%} bankroll and {args.max_stake:g} max"
    )


if __name__ == "__main__":
    main()
