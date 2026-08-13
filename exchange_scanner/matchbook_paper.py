from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from exchange_scanner.paper import log_signals
from exchange_scanner.the_odds_api import ValueSignal


def log_enriched_opportunities(
    *,
    paper_db: Path,
    opportunities_csv: Path,
    min_liquidity: float = 0.01,
) -> int:
    rows = _read_csv(opportunities_csv)
    logged_at = datetime.now(timezone.utc)
    inserted = 0
    for row in rows:
        stake = _float(row.get("available_at_or_above_target"))
        if row.get("liquidity_status") != "available" or stake < min_liquidity:
            continue
        inserted += log_signals(
            paper_db,
            [_signal_from_row(row)],
            stake=stake,
            logged_at=logged_at,
        )
    return inserted


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _signal_from_row(row: dict[str, str]) -> ValueSignal:
    target_odds = _float(row["target_odds"])
    target_effective_odds = _optional_float(row.get("target_effective_odds"))
    reference_fair_odds = _float(row["reference_fair_odds"])
    return ValueSignal(
        sport_key=row["sport_key"],
        event_id=row["event_id"],
        event_name=row["event_name"],
        commence_time=datetime.fromisoformat(row["commence_time"]),
        market_key=row.get("market") or row.get("market_key", "h2h"),
        outcome_name=row["outcome_name"],
        target_bookmaker=row["target_bookmaker"],
        target_odds=target_odds,
        target_effective_odds=target_effective_odds,
        reference_fair_odds=reference_fair_odds,
        reference_probability=_float(row.get("reference_probability")) or 1 / reference_fair_odds,
        edge=_float(row["edge"]),
        reference_bookmakers=tuple(
            item.strip() for item in row.get("reference_bookmakers", "").split(",") if item.strip()
        ),
    )


def _float(value: str | None) -> float:
    return float(value or 0)


def _optional_float(value: str | None) -> float | None:
    return float(value) if value else None
