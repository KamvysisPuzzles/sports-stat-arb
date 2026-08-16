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

    assert "Candidate rows this scan: 2" in markdown
    assert "Unbooked opportunities shown: 1" in markdown
    assert "Executable unbooked rows: 1" in markdown
    assert "Executable unbooked liquidity found: 20.00" in markdown
    assert "Unbooked scan theoretical EV: 0.80" in markdown
    opportunities_section = markdown.split("## Opportunities", 1)[1].split("## Booked Trades", 1)[0]
    assert "Team C v Team D" in markdown
    assert "Back Team B with Matchbook at 3.0 | Team A v Team B" not in opportunities_section


def test_price_scan_summary_uses_price_signal_language() -> None:
    opportunities = [
        {
            "event_id": "event-1",
            "market": "h2h",
            "outcome_name": "Team B",
            "event_name": "Team A v Team B",
            "bet_to_place": "Back Team B with Betfair at 3.0",
            "edge": "0.05",
            "commence_time": "2026-08-14T12:00:00+00:00",
        },
        {
            "event_id": "event-2",
            "market": "h2h",
            "outcome_name": "Team D",
            "event_name": "Team C v Team D",
            "bet_to_place": "Back Team D with Smarkets at 4.0",
            "edge": "0.04",
            "commence_time": "2026-08-14T14:00:00+00:00",
        },
    ]

    markdown = build_markdown(
        [],
        opportunities,
        new_trades_count=2,
        opportunity_dedupe_key="event-market-outcome",
        scan_kind="price",
    )

    assert "Candidate price signals this scan: 2" in markdown
    assert "Newly booked trades: 2" in markdown
    assert "Candidate average edge: 4.50%" in markdown
    assert "Candidate nominal EV at 1 GBP stake: 0.09" in markdown
    assert "Unbooked price signals shown below: 2" in markdown


def test_summary_splits_clv_beats_misses_and_ties() -> None:
    trades = [
        {
            "event_id": "event-1",
            "market": "h2h",
            "event_name": "Team A v Team B",
            "outcome_name": "Team A",
            "stake": "1",
            "edge": "0.03",
            "target_odds": "3.0",
            "status": "settled",
            "logged_at": "2026-08-13T12:00:00+00:00",
            "commence_time": "2026-08-14T12:00:00+00:00",
            "target_clv": "0.0500",
            "profit": "2.0",
            "liquidity_status": "available",
        },
        {
            "event_id": "event-2",
            "market": "h2h",
            "event_name": "Team C v Team D",
            "outcome_name": "Team C",
            "stake": "1",
            "edge": "0.03",
            "target_odds": "4.0",
            "status": "settled",
            "logged_at": "2026-08-13T12:00:00+00:00",
            "commence_time": "2026-08-14T12:00:00+00:00",
            "target_clv": "-0.0200",
            "profit": "-1.0",
            "liquidity_status": "available",
        },
        {
            "event_id": "event-3",
            "market": "h2h",
            "event_name": "Team E v Team F",
            "outcome_name": "Team E",
            "stake": "1",
            "edge": "0.03",
            "target_odds": "5.0",
            "status": "settled",
            "logged_at": "2026-08-13T12:00:00+00:00",
            "commence_time": "2026-08-14T12:00:00+00:00",
            "target_clv": "0.0000",
            "profit": "-1.0",
            "liquidity_status": "available",
        },
        {
            "event_id": "event-4",
            "market": "h2h",
            "event_name": "Team G v Team H",
            "outcome_name": "Team G",
            "stake": "1",
            "edge": "0.03",
            "target_odds": "10.0",
            "status": "settled",
            "logged_at": "2026-08-13T12:00:00+00:00",
            "commence_time": "2026-08-14T12:00:00+00:00",
            "target_clv": "0.5000",
            "profit": "5.0",
            "liquidity_status": "not_applicable",
        },
    ]

    markdown = build_markdown(trades, [])

    assert "Scope: liquidity-confirmed trades only" in markdown
    assert "Raw trades logged" not in markdown
    assert "Total trades booked: 3" in markdown
    assert "Average booked odds: 4.00" in markdown
    assert "Settled trades: 3" in markdown
    assert "Settled won/lost bets: 1/2" in markdown
    assert "Settled average booked odds: 4.00" in markdown
    assert "Beat closing line: 33.33% (1/3)" in markdown
    assert "Missed closing line: 33.33% (1/3)" in markdown
    assert "Tied closing line: 33.33% (1/3)" in markdown
    assert "Average CLV per closed trade: 1.00%" in markdown
    assert "CLV breakdown: beat avg 5.00%, miss avg -2.00%, tie avg 0.00%" in markdown
