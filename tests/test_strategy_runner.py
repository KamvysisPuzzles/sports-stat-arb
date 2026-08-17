from __future__ import annotations

from datetime import datetime, timezone

from exchange_scanner import strategy_runner
from exchange_scanner.strategy_runner import StrategyRunnerConfig, run_paper_log


class FakeOddsClient:
    def fetch_odds(self, *, sport, regions, markets):
        now = "2026-08-14T12:00:00Z"
        return [
            {
                "id": "event-1",
                "sport_key": sport,
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "commence_time": "2026-08-15T15:00:00Z",
                "bookmakers": [
                    {
                        "key": "matchbook",
                        "title": "Matchbook",
                        "last_update": now,
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Arsenal", "price": 4.2},
                                    {"name": "Chelsea", "price": 1.25},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "pinnacle",
                        "title": "Pinnacle",
                        "last_update": now,
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Arsenal", "price": 4.0},
                                    {"name": "Chelsea", "price": 1.3333333333},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "smarkets",
                        "title": "Smarkets",
                        "last_update": now,
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Arsenal", "price": 4.0},
                                    {"name": "Chelsea", "price": 1.3333333333},
                                ],
                            }
                        ],
                    },
                ],
            }
        ]


class FakeMatchbookClient:
    def fetch_events(self, *, start, end, currency, minimum_liquidity):
        return [
            {
                "id": 123,
                "name": "Arsenal v Chelsea",
                "markets": [
                    {
                        "id": 456,
                        "product": "EXCHANGE",
                        "status": "open",
                        "name": "Match Odds",
                        "runners": [
                            {
                                "id": 789,
                                "name": "Arsenal",
                                "prices": [
                                    {
                                        "side": "back",
                                        "decimal-odds": 4.2,
                                        "available-amount": 25,
                                    },
                                    {
                                        "side": "lay",
                                        "decimal-odds": 4.3,
                                        "available-amount": 40,
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]


class FakeS3Client:
    def __init__(self) -> None:
        self.uploads = []

    def upload_file(self, filename, bucket, key):
        self.uploads.append((filename, bucket, key))


class FakeTable:
    def __init__(self) -> None:
        self.items = []

    def put_item(self, *, Item, ConditionExpression):
        self.items.append(Item)


def test_run_paper_log_archives_snapshot_and_logs_liquidity_confirmed_trade(monkeypatch) -> None:
    monkeypatch.setitem(strategy_runner.SPORT_PROFILES, "test-profile", ["soccer_epl"])
    table = FakeTable()
    s3_client = FakeS3Client()
    config = StrategyRunnerConfig(
        mode="paper-log",
        odds_api_key="test-key",
        dynamodb_table_name="paper-trades",
        odds_s3_bucket="odds-bucket",
        sports_profile="test-profile",
        max_api_requests=1,
        min_reference_books=2,
        use_betfair_lambda=False,
    )

    result = run_paper_log(
        config,
        odds_client=FakeOddsClient(),
        matchbook_client=FakeMatchbookClient(),
        dynamodb_table=table,
        s3_client=s3_client,
        now=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
    )

    assert result["sports"] == 1
    assert result["odds_rows"] == 6
    assert result["candidate_signals"] == 1
    assert result["liquidity_confirmed_signals"] == 1
    assert result["paper_log"]["inserted"] == 1
    assert result["snapshot"]["uploaded"] is True
    assert s3_client.uploads[0][1] == "odds-bucket"
    assert s3_client.uploads[0][2].startswith("odds_snapshots/snapshot_date=2026-08-14/")
    assert table.items[0]["liquidity_status"] == "available"
    assert table.items[0]["available_at_or_above_target"] == 25
