from __future__ import annotations

import sys
from datetime import datetime, timezone

import pyarrow.parquet as pq
import pytest

from exchange_scanner.sharpness import store_odds_snapshot
from exchange_scanner.the_odds_api import OutcomePrice
from scripts import export_odds_snapshot_parquet


def price(
    *,
    outcome_name: str,
    odds: float,
    exchange_lay_odds: float | None = None,
    exchange_spread_pct: float | None = None,
) -> OutcomePrice:
    return OutcomePrice(
        bookmaker_key="pinnacle",
        bookmaker_title="Pinnacle",
        sport_key="soccer_epl",
        event_id="event-1",
        event_name="Arsenal v Chelsea",
        commence_time=datetime(2026, 8, 15, 15, tzinfo=timezone.utc),
        market_key="h2h",
        market_name="h2h",
        outcome_name=outcome_name,
        point=None,
        odds=odds,
        exchange_lay_odds=exchange_lay_odds,
        exchange_spread_pct=exchange_spread_pct,
        last_update=datetime(2026, 8, 14, 23, 5, tzinfo=timezone.utc),
    )


def test_export_latest_odds_snapshot_to_partitioned_parquet(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "markets.sqlite3"
    output_dir = tmp_path / "export"
    github_output = tmp_path / "github_output"
    snapshot_time = datetime(2026, 8, 14, 23, 5, tzinfo=timezone.utc)
    store_odds_snapshot(
        db_path,
        [
            price(
                outcome_name="Arsenal",
                odds=2.0,
                exchange_lay_odds=2.08,
                exchange_spread_pct=0.0392156863,
            ),
            price(outcome_name="Chelsea", odds=2.2),
        ],
        snapshot_time=snapshot_time,
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export_odds_snapshot_parquet.py",
            "--market-db",
            str(db_path),
            "--output-dir",
            str(output_dir),
            "--s3-prefix",
            "odds_snapshots",
            "--github-output",
            str(github_output),
        ],
    )

    export_odds_snapshot_parquet.main()

    parquet_path = (
        output_dir
        / "odds_snapshots"
        / "snapshot_date=2026-08-14"
        / "hour=23"
        / "odds_20260814T230500Z.parquet"
    )
    assert parquet_path.exists()
    output_lines = github_output.read_text(encoding="utf-8").splitlines()
    assert f"local_path={parquet_path}" in output_lines
    assert (
        "s3_key=odds_snapshots/snapshot_date=2026-08-14/hour=23/"
        "odds_20260814T230500Z.parquet"
    ) in output_lines

    rows = pq.read_table(parquet_path).to_pylist()
    assert {row["outcome_name"] for row in rows} == {"Arsenal", "Chelsea"}
    arsenal = next(row for row in rows if row["outcome_name"] == "Arsenal")
    assert arsenal["snapshot_time"] == snapshot_time
    assert arsenal["market"] == "h2h"
    assert arsenal["point"] is None
    assert arsenal["exchange_lay_odds"] == pytest.approx(2.08)
    assert arsenal["exchange_spread_pct"] == pytest.approx(0.0392156863)
    assert arsenal["implied_probability"] == pytest.approx(0.5)
    assert arsenal["days_to_start"] == pytest.approx((15 + 55 / 60) / 24)
