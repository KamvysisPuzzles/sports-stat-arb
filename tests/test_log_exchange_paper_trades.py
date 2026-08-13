from __future__ import annotations

import pytest

from exchange_scanner.paper import list_trades
from scripts.log_exchange_paper_trades import main as log_exchange_main


def test_cli_logs_exchange_rows_with_matchbook_liquidity(tmp_path, monkeypatch) -> None:
    paper_db = tmp_path / "paper.sqlite3"
    opportunities_csv = tmp_path / "opportunities.csv"
    inserted_count = tmp_path / "inserted.txt"
    opportunities_csv.write_text(
        "sport_key,event_id,event_name,commence_time,market,outcome_name,"
        "target_bookmaker,target_odds,target_effective_odds,reference_fair_odds,"
        "reference_probability,edge,reference_bookmakers,"
        "matchbook_event_id,matchbook_market_id,matchbook_runner_id,liquidity_status,"
        "available_at_or_above_target,best_back_odds,best_back_available,best_lay_odds,"
        "best_lay_available,back_lay_spread_pct\n"
        "tennis_atp,event-1,Player A v Player B,2026-08-14T12:00:00+00:00,h2h,"
        "Player B,Matchbook,3.1,3.058,2.9786,0.3357,0.0267,"
        '"Pinnacle, Betfair",123,456,789,available,63.98,3.1,63.98,3.2,20,0.0317\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "log_exchange_paper_trades.py",
            "--paper-db",
            str(paper_db),
            "--opportunities-csv",
            str(opportunities_csv),
            "--paper-stake",
            "1",
            "--inserted-count-out",
            str(inserted_count),
        ],
    )

    log_exchange_main()

    trades = list_trades(paper_db)
    assert inserted_count.read_text(encoding="utf-8") == "1\n"
    assert len(trades) == 1
    assert trades[0].stake == 1
    assert trades[0].target_bookmaker == "Matchbook"
    assert trades[0].liquidity_status == "available"
    assert trades[0].available_at_or_above_target == pytest.approx(63.98)
    assert trades[0].best_back_odds == pytest.approx(3.1)
