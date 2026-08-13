from __future__ import annotations

import pytest

from exchange_scanner.matchbook_paper import log_enriched_opportunities
from exchange_scanner.paper import list_trades, settle_results


def test_log_enriched_opportunities_uses_available_liquidity_as_stake(tmp_path) -> None:
    paper_db = tmp_path / "paper.sqlite3"
    opportunities_csv = tmp_path / "opportunities.csv"
    opportunities_csv.write_text(
        "sport_key,event_id,event_name,commence_time,market,outcome_name,"
        "target_bookmaker,target_odds,target_effective_odds,reference_fair_odds,"
        "reference_probability,edge,reference_bookmakers,"
        "available_at_or_above_target,liquidity_status\n"
        "tennis_atp,event-1,Player A v Player B,2026-08-14T12:00:00+00:00,h2h,"
        "Player B,Matchbook,3.1,3.058,2.9786,0.3357,0.0267,"
        '"Pinnacle, Betfair",63.98,available\n'
        "soccer,event-2,Team A v Team B,2026-08-14T13:00:00+00:00,h2h,"
        "Team B,Matchbook,4.4,4.332,3.9898,0.2506,0.0858,"
        '"Pinnacle, Betfair",0,not_matched\n'
    )

    inserted = log_enriched_opportunities(
        paper_db=paper_db,
        opportunities_csv=opportunities_csv,
    )

    trades = list_trades(paper_db)
    assert inserted == 1
    assert len(trades) == 1
    assert trades[0].event_id == "event-1"
    assert trades[0].stake == pytest.approx(63.98)

    settle_results(paper_db, {"event-1": "Player B"})
    settled = list_trades(paper_db)[0]
    assert settled.profit == pytest.approx(63.98 * (3.058 - 1))
