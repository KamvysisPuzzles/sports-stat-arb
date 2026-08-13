from __future__ import annotations

from scripts.paper_alert_summary import build_markdown


def test_summary_excludes_already_booked_opportunities() -> None:
    trades = [
        {
            "event_id": "event-1",
            "market": "h2h",
            "event_name": "Team A v Team B",
            "outcome_name": "Team B",
            "target_bookmaker": "Matchbook",
            "target_odds": "3.0",
            "stake": "10",
            "edge": "0.05",
            "status": "open",
            "logged_at": "2026-08-13T12:00:00+00:00",
            "commence_time": "2026-08-14T12:00:00+00:00",
        }
    ]
    opportunities = [
        {
            "event_id": "event-1",
            "market": "h2h",
            "event_name": "Team A v Team B",
            "bet_to_place": "Back Team B with Matchbook at 3.0",
            "available_at_or_above_target": "10",
            "liquidity_status": "available",
            "edge": "0.05",
            "commence_time": "2026-08-14T12:00:00+00:00",
        },
        {
            "event_id": "event-2",
            "market": "h2h",
            "event_name": "Team C v Team D",
            "bet_to_place": "Back Team D with Matchbook at 4.0",
            "available_at_or_above_target": "20",
            "liquidity_status": "available",
            "edge": "0.04",
            "commence_time": "2026-08-14T14:00:00+00:00",
        },
    ]

    markdown = build_markdown(trades, opportunities, new_trades_count=1)

    assert "New flagged opportunities: 1" in markdown
    assert "Executable rows: 1" in markdown
    assert "Executable liquidity found: 20.00" in markdown
    assert "Scan theoretical EV: 0.80" in markdown
    opportunities_section = markdown.split("## Opportunities", 1)[1].split("## Booked Trades", 1)[0]
    assert "Team C v Team D" in markdown
    assert "Back Team B with Matchbook at 3.0 | Team A v Team B" not in opportunities_section
