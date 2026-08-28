from __future__ import annotations

import argparse
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from exchange_scanner.dynamodb_paper import (
    LIQUIDITY_FIELDS,
    log_signals_to_dynamodb,
    signal_key,
)
from exchange_scanner.the_odds_api import ValueSignal

FRACTIONAL_SECONDS_RE = re.compile(r"(\.\d{1,5})([+-]\d\d:\d\d)$")


def main() -> None:
    args = parse_args()
    candidates = _candidate_signals(args)
    unique_candidates = {
        signal_key(signal)
        for _, signal in candidates
    }
    if args.dry_run:
        print(
            "Betfair target backfill dry run: "
            f"candidate_rows={args._candidate_rows}, "
            f"unique_candidates={len(unique_candidates)}, "
            f"first_bookable_candidates={len(candidates)}"
        )
        return

    table = _dynamodb_table(args.table_name, args.region)
    by_snapshot: dict[datetime, list[ValueSignal]] = defaultdict(list)
    for snapshot_time, signal in candidates:
        by_snapshot[snapshot_time].append(signal)

    attempted = 0
    inserted = 0
    duplicates = 0
    for snapshot_time in sorted(by_snapshot):
        signals = by_snapshot[snapshot_time]
        liquidity_by_key = {
            signal_key(signal): _unavailable_liquidity()
            for signal in signals
        }
        result = log_signals_to_dynamodb(
            table,
            signals,
            stake=args.paper_stake,
            logged_at=snapshot_time,
            liquidity_by_key=liquidity_by_key,
        )
        attempted += result.attempted
        inserted += result.inserted
        duplicates += result.duplicates

    print(
        "Betfair target backfill complete: "
        f"candidate_rows={args._candidate_rows}, "
        f"unique_candidates={len(unique_candidates)}, "
        f"attempted={attempted}, "
        f"inserted={inserted}, "
        f"duplicates={duplicates}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay S3 parquet odds snapshots and backfill Betfair target paper trades."
    )
    parser.add_argument(
        "--parquet-root",
        type=Path,
        default=Path("data/s3_odds_snapshots/odds_snapshots"),
    )
    parser.add_argument("--table-name", default="sports-stat-arb-paper-trades")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--paper-stake", type=float, default=1.0)
    parser.add_argument("--max-age-seconds", type=int, default=180)
    parser.add_argument("--max-event-days", type=float, default=4.0)
    parser.add_argument("--max-target-odds", type=float, default=6.0)
    parser.add_argument("--max-target-spread-pct", type=float, default=0.06)
    parser.add_argument("--min-edge", type=float, default=0.02)
    parser.add_argument("--max-edge", type=float, default=0.10)
    parser.add_argument("--commission-rate", type=float, default=0.02)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args._candidate_rows = 0
    return args


def _candidate_signals(args: argparse.Namespace) -> list[tuple[datetime, ValueSignal]]:
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("Install duckdb first: python -m pip install duckdb") from exc

    parquet_glob = str(args.parquet_root / "**" / "*.parquet")
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    rows = con.execute(
        f"""
        WITH raw AS (
            SELECT
                CAST(snapshot_time AS VARCHAR) AS snapshot_time_text,
                snapshot_time,
                sport_key,
                event_id,
                event_name,
                commence_time,
                CAST(commence_time AS VARCHAR) AS commence_time_text,
                market,
                outcome_name,
                bookmaker_key,
                bookmaker_title,
                lower(coalesce(bookmaker_identity, bookmaker_key, bookmaker_title)) AS bookmaker_identity,
                odds,
                exchange_lay_odds,
                exchange_spread_pct,
                last_update
            FROM read_parquet('{parquet_glob}', hive_partitioning=1, union_by_name=true)
            WHERE market = 'h2h'
              AND odds IS NOT NULL
              AND last_update >= snapshot_time - INTERVAL {int(args.max_age_seconds)} SECOND
              AND commence_time > snapshot_time
              AND commence_time <= snapshot_time + ({float(args.max_event_days)} * INTERVAL 1 DAY)
        ),
        outcome_counts AS (
            SELECT
                snapshot_time,
                event_id,
                market,
                bookmaker_identity,
                count(DISTINCT outcome_name) AS outcome_count
            FROM raw
            GROUP BY 1, 2, 3, 4
        ),
        expected AS (
            SELECT
                snapshot_time,
                event_id,
                market,
                max(outcome_count) AS expected_outcomes
            FROM outcome_counts
            GROUP BY 1, 2, 3
        ),
        refs AS (
            SELECT
                raw.*,
                CASE
                    WHEN exchange_lay_odds IS NOT NULL AND odds > 1 AND exchange_lay_odds > 1
                    THEN ((1 / odds) + (1 / exchange_lay_odds)) / 2
                    WHEN odds > 1
                    THEN 1 / odds
                    ELSE NULL
                END AS raw_probability
            FROM raw
            WHERE bookmaker_key IN ('pinnacle', 'smarkets')
        ),
        complete_refs AS (
            SELECT refs.*
            FROM refs
            JOIN expected USING (snapshot_time, event_id, market)
            JOIN outcome_counts USING (snapshot_time, event_id, market, bookmaker_identity)
            WHERE outcome_counts.outcome_count = expected.expected_outcomes
        ),
        ref_norm AS (
            SELECT
                *,
                raw_probability
                    / sum(raw_probability) OVER (
                        PARTITION BY snapshot_time, event_id, market, bookmaker_identity
                    ) AS normalised_probability
            FROM complete_refs
            WHERE raw_probability IS NOT NULL
        ),
        fair_base AS (
            SELECT
                snapshot_time,
                event_id,
                market,
                outcome_name,
                avg(normalised_probability) AS median_probability,
                count(DISTINCT bookmaker_key) AS reference_count,
                min(1 / normalised_probability) AS min_ref_fair_odds,
                max(1 / normalised_probability) AS max_ref_fair_odds,
                max(CASE WHEN bookmaker_key = 'pinnacle' THEN CAST(last_update AS VARCHAR) END) AS pinnacle_last_update,
                max(CASE WHEN bookmaker_key = 'smarkets' THEN CAST(last_update AS VARCHAR) END) AS smarkets_last_update,
                max(CASE WHEN bookmaker_key = 'smarkets' THEN exchange_spread_pct END) AS smarkets_spread_pct
            FROM ref_norm
            GROUP BY 1, 2, 3, 4
            HAVING count(DISTINCT bookmaker_key) = 2
        ),
        fair AS (
            SELECT
                *,
                median_probability
                    / sum(median_probability) OVER (PARTITION BY snapshot_time, event_id, market)
                    AS fair_probability
            FROM fair_base
        ),
        targets AS (
            SELECT *
            FROM raw
            WHERE bookmaker_key = 'betfair_ex_uk'
              AND exchange_lay_odds IS NOT NULL
              AND exchange_spread_pct IS NOT NULL
              AND exchange_spread_pct <= {float(args.max_target_spread_pct)}
        ),
        back_candidates AS (
            SELECT
                'back' AS bet_side,
                targets.snapshot_time,
                targets.snapshot_time_text,
                targets.sport_key,
                targets.event_id,
                targets.event_name,
                targets.commence_time_text,
                targets.market,
                targets.outcome_name,
                targets.bookmaker_title AS target_bookmaker,
                targets.odds AS target_odds,
                1 + ((targets.odds - 1) * (1 - {float(args.commission_rate)})) AS target_effective_odds,
                fair.fair_probability,
                1 / fair.fair_probability AS reference_fair_odds,
                ((1 + ((targets.odds - 1) * (1 - {float(args.commission_rate)}))) * fair.fair_probability) - 1 AS edge,
                targets.exchange_spread_pct AS betfair_back_lay_spread_pct,
                fair.min_ref_fair_odds,
                fair.max_ref_fair_odds,
                fair.pinnacle_last_update,
                fair.smarkets_last_update,
                fair.smarkets_spread_pct
            FROM targets
            JOIN fair USING (snapshot_time, event_id, market, outcome_name)
            WHERE targets.odds <= {float(args.max_target_odds)}
        ),
        lay_candidates AS (
            SELECT
                'lay' AS bet_side,
                targets.snapshot_time,
                targets.snapshot_time_text,
                targets.sport_key,
                targets.event_id,
                targets.event_name,
                targets.commence_time_text,
                targets.market,
                targets.outcome_name,
                targets.bookmaker_title AS target_bookmaker,
                targets.exchange_lay_odds AS target_odds,
                targets.exchange_lay_odds AS target_effective_odds,
                fair.fair_probability,
                1 / fair.fair_probability AS reference_fair_odds,
                (((1 - fair.fair_probability) * (1 - {float(args.commission_rate)}))
                    - (fair.fair_probability * (targets.exchange_lay_odds - 1)))
                    / (targets.exchange_lay_odds - 1) AS edge,
                targets.exchange_spread_pct AS betfair_back_lay_spread_pct,
                fair.min_ref_fair_odds,
                fair.max_ref_fair_odds,
                fair.pinnacle_last_update,
                fair.smarkets_last_update,
                fair.smarkets_spread_pct
            FROM targets
            JOIN fair USING (snapshot_time, event_id, market, outcome_name)
            WHERE targets.exchange_lay_odds <= {float(args.max_target_odds)}
        ),
        candidates AS (
            SELECT * FROM back_candidates
            UNION ALL
            SELECT * FROM lay_candidates
        ),
        bookable AS (
            SELECT
                *,
                (max_ref_fair_odds - min_ref_fair_odds)
                    / ((max_ref_fair_odds + min_ref_fair_odds) / 2) AS reference_disagreement_pct
            FROM candidates
            WHERE edge >= {float(args.min_edge)}
              AND edge <= {float(args.max_edge)}
        ),
        first_bookable AS (
            SELECT
                *,
                row_number() OVER (
                    PARTITION BY event_id, market, outcome_name, target_bookmaker, bet_side
                    ORDER BY snapshot_time ASC, edge DESC
                ) AS rn
            FROM bookable
        )
        SELECT
            count(*) OVER () AS first_bookable_count,
            (SELECT count(*) FROM bookable) AS candidate_rows,
            snapshot_time_text,
            sport_key,
            event_id,
            event_name,
            commence_time_text,
            market,
            outcome_name,
            target_bookmaker,
            target_odds,
            target_effective_odds,
            fair_probability,
            reference_fair_odds,
            edge,
            bet_side,
            betfair_back_lay_spread_pct,
            reference_disagreement_pct,
            min_ref_fair_odds,
            max_ref_fair_odds,
            pinnacle_last_update,
            smarkets_last_update,
            smarkets_spread_pct
        FROM first_bookable
        WHERE rn = 1
        ORDER BY snapshot_time ASC, edge DESC
        """
    ).fetchall()
    if rows:
        args._candidate_rows = int(rows[0][1])
    signals = []
    for row in rows:
        signals.append((_parse_time(row[2]), _signal_from_row(row)))
    return signals


def _signal_from_row(row: tuple[Any, ...]) -> ValueSignal:
    return ValueSignal(
        sport_key=str(row[3]),
        event_id=str(row[4]),
        event_name=str(row[5]),
        commence_time=_parse_time(row[6]),
        market_key=str(row[7]),
        outcome_name=str(row[8]),
        target_bookmaker=str(row[9]),
        target_odds=float(row[10]),
        target_effective_odds=float(row[11]),
        reference_fair_odds=float(row[13]),
        reference_probability=float(row[12]),
        edge=float(row[14]),
        reference_bookmakers=("Pinnacle", "Smarkets"),
        bet_side=str(row[15]),
        betfair_back_lay_spread_pct=_optional_float(row[16]),
        reference_fair_odds_by_bookmaker=(
            ("Pinnacle", float(row[18])),
            ("Smarkets", float(row[19])),
        ),
        reference_spread_pct_by_bookmaker=(
            (("Smarkets", float(row[22])),) if row[22] is not None else ()
        ),
        reference_last_update_by_bookmaker=(
            ("Pinnacle", str(row[20] or "")),
            ("Smarkets", str(row[21] or "")),
        ),
        reference_disagreement_pct=_optional_float(row[17]),
        reference_max_spread_pct=_optional_float(row[22]),
        reference_avg_spread_pct=_optional_float(row[22]),
    )


def _unavailable_liquidity() -> dict[str, str]:
    values = {
        "liquidity_status": "unavailable",
        "available_at_or_above_target": "0.00",
        "best_back_available": "0.00",
        "best_lay_available": "0.00",
    }
    return {field: values.get(field, "") for field in LIQUIDITY_FIELDS}


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    return float(value)


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).replace("Z", "+00:00").replace(" ", "T", 1)
        if text.endswith("+00"):
            text = f"{text}:00"
        text = FRACTIONAL_SECONDS_RE.sub(lambda match: match.group(1).ljust(7, "0") + match.group(2), text)
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _dynamodb_table(table_name: str, region: str):
    import boto3

    return boto3.resource("dynamodb", region_name=region).Table(table_name)


if __name__ == "__main__":
    main()
