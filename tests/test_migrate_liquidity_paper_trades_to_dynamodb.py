from __future__ import annotations

from decimal import Decimal

from scripts.migrate_liquidity_paper_trades_to_dynamodb import item_from_row


def test_item_from_row_preserves_liquidity_and_settlement_fields() -> None:
    item = item_from_row(
        {
            "id": "12",
            "logged_at": "2026-08-14T12:00:00+00:00",
            "sport_key": "soccer_epl",
            "event_id": "event-1",
            "event_name": "Arsenal v Chelsea",
            "commence_time": "2026-08-15T15:00:00+00:00",
            "market": "h2h",
            "outcome_name": "Arsenal",
            "target_bookmaker": "Matchbook",
            "target_odds": "4.2",
            "target_effective_odds": "4.136",
            "reference_fair_odds": "4.0",
            "reference_probability": "0.25",
            "edge": "0.034",
            "reference_bookmakers": "Pinnacle, Smarkets",
            "stake": "1",
            "matchbook_event_id": "123",
            "matchbook_market_id": "456",
            "matchbook_runner_id": "789",
            "liquidity_status": "available",
            "available_at_or_above_target": "25.50",
            "best_back_odds": "4.2",
            "status": "settled",
            "target_clv": "0.02",
            "profit": "3.136",
        }
    )

    assert item["legacy_id"] == Decimal("12.0")
    assert item["trade_id"].startswith("paper#")
    assert item["liquidity_status"] == "available"
    assert item["available_at_or_above_target"] == Decimal("25.5")
    assert item["status"] == "settled"
    assert item["profit"] == Decimal("3.136")
    assert item["reference_bookmakers"] == ["Pinnacle", "Smarkets"]
