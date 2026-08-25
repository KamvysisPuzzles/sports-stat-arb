from __future__ import annotations

import json
from datetime import datetime, timezone

from exchange_scanner import strategy_runner
from exchange_scanner.strategy_runner import StrategyRunnerConfig, run_paper_log


class FakeOddsClient:
    def __init__(self) -> None:
        self.odds_calls = 0
        self.score_calls = []
        self.sports_payload = [{"key": "soccer_epl", "active": True}]

    def fetch_sports(self):
        return self.sports_payload

    def fetch_odds(self, *, sport, regions, markets):
        self.odds_calls += 1
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
                                    {"name": "Chelsea", "price": 1.4},
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

    def fetch_scores(self, *, sport, days_from=3):
        self.score_calls.append((sport, days_from))
        return [
            {
                "id": "event-1",
                "completed": True,
                "scores": [
                    {"name": "Arsenal", "score": "2"},
                    {"name": "Chelsea", "score": "1"},
                ],
            }
        ]


class FakeLongshotOddsClient(FakeOddsClient):
    def fetch_odds(self, *, sport, regions, markets):
        self.odds_calls += 1
        payload = super().fetch_odds(sport=sport, regions=regions, markets=markets)
        payload[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 7.2
        payload[0]["bookmakers"][0]["markets"][0]["outcomes"][1]["price"] = 1.01
        return payload


class FakeMultiSportOddsClient(FakeOddsClient):
    def __init__(self) -> None:
        super().__init__()
        self.requested_sports = []
        self.sports_payload = [
            {"key": "soccer_epl", "active": True},
            {"key": "soccer_fa_cup", "active": True},
            {"key": "basketball_nba", "active": True},
        ]

    def fetch_odds(self, *, sport, regions, markets):
        self.requested_sports.append(sport)
        payload = super().fetch_odds(sport=sport, regions=regions, markets=markets)
        payload[0]["id"] = f"{sport}-event-1"
        return payload


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
        self.objects = {}

    def upload_file(self, filename, bucket, key):
        self.uploads.append((filename, bucket, key))

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = {
            "body": Body.decode("utf-8") if isinstance(Body, bytes) else Body,
            "content_type": ContentType,
        }


class ConditionalCheckFailedException(Exception):
    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    def __init__(self) -> None:
        self.items = {}

    def put_item(self, *, Item, ConditionExpression=None):
        if ConditionExpression is not None:
            assert ConditionExpression == "attribute_not_exists(trade_id)"
        if ConditionExpression is not None and Item["trade_id"] in self.items:
            raise ConditionalCheckFailedException()
        self.items[Item["trade_id"]] = Item

    def get_item(self, *, Key):
        item = self.items.get(Key["trade_id"])
        return {"Item": item} if item else {}

    def scan(self, **kwargs):
        if not kwargs:
            return {"Items": list(self.items.values())}
        status = kwargs["ExpressionAttributeValues"][":open_status"]
        return {
            "Items": [
                item
                for item in self.items.values()
                if item.get("status") == status
            ]
        }

    def update_item(self, *, Key, UpdateExpression, ExpressionAttributeValues, **kwargs):
        item = self.items[Key["trade_id"]]
        if "closing_checked_at" in UpdateExpression:
            item["closing_checked_at"] = ExpressionAttributeValues[":checked_at"]
            item["closing_target_odds"] = ExpressionAttributeValues[":closing_target_odds"]
            item["target_clv"] = ExpressionAttributeValues[":target_clv"]
            item["closing_reference_fair_odds"] = ExpressionAttributeValues[
                ":closing_reference_fair_odds"
            ]
            item["closing_edge"] = ExpressionAttributeValues[":closing_edge"]
        else:
            item["status"] = ExpressionAttributeValues[":settled"]
            item["result"] = ExpressionAttributeValues[":result"]
            item["profit"] = ExpressionAttributeValues[":profit"]
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def delete_item(self, *, Key):
        self.items.pop(Key["trade_id"])
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def test_run_paper_log_archives_snapshot_and_logs_liquidity_confirmed_trade(monkeypatch) -> None:
    monkeypatch.setitem(strategy_runner.SPORT_PROFILES, "test-profile", ["soccer_epl"])
    table = FakeTable()
    s3_client = FakeS3Client()
    odds_client = FakeOddsClient()
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
        odds_client=odds_client,
        matchbook_client=FakeMatchbookClient(),
        dynamodb_table=table,
        s3_client=s3_client,
        now=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
    )

    assert result["sports"] == 1
    assert result["odds_rows"] == 6
    assert result["closing_update"]["open_trades"] == 0
    assert result["settlement"]["open_trades"] == 0
    assert odds_client.odds_calls == 1
    assert odds_client.score_calls == []
    assert result["candidate_signals"] == 2
    assert result["liquidity_confirmed_signals"] == 1
    assert result["paper_log"]["inserted"] == 1
    assert result["summary"]["uploaded"] is True
    assert result["snapshot"]["uploaded"] is True
    assert s3_client.uploads[0][1] == "odds-bucket"
    assert s3_client.uploads[0][2].startswith("odds_snapshots/snapshot_date=2026-08-14/")
    summary_text = s3_client.objects[
        ("odds-bucket", "summaries/latest_strategy_runner_summary.txt")
    ]["body"]
    assert "Strategy Runner Summary" in summary_text
    assert "New paper trades: 1" in summary_text
    summary_json = json.loads(
        s3_client.objects[("odds-bucket", "summaries/latest_strategy_runner_summary.json")][
            "body"
        ]
    )
    assert summary_json["portfolio_summary"]["total_trades"] == 1
    item = next(iter(table.items.values()))
    assert item["liquidity_status"] == "available"
    assert item["available_at_or_above_target"] == 25


def test_run_paper_log_filters_static_profile_to_active_sports(monkeypatch) -> None:
    monkeypatch.setitem(
        strategy_runner.SPORT_PROFILES,
        "test-profile",
        ["soccer_epl", "soccer_inactive"],
    )
    odds_client = FakeOddsClient()
    odds_client.sports_payload = [
        {"key": "soccer_epl", "active": True},
        {"key": "soccer_inactive", "active": False},
    ]
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
        odds_client=odds_client,
        matchbook_client=FakeMatchbookClient(),
        dynamodb_table=FakeTable(),
        s3_client=FakeS3Client(),
        now=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
    )

    assert result["sports"] == 1
    assert odds_client.odds_calls == 1


def test_run_paper_log_can_disable_active_sports_filter(monkeypatch) -> None:
    monkeypatch.setitem(
        strategy_runner.SPORT_PROFILES,
        "test-profile",
        ["soccer_epl", "soccer_inactive"],
    )
    odds_client = FakeOddsClient()
    odds_client.sports_payload = [{"key": "soccer_epl", "active": True}]
    config = StrategyRunnerConfig(
        mode="paper-log",
        odds_api_key="test-key",
        dynamodb_table_name="paper-trades",
        odds_s3_bucket="odds-bucket",
        sports_profile="test-profile",
        max_api_requests=2,
        filter_inactive_sports=False,
        min_reference_books=2,
        use_betfair_lambda=False,
    )

    result = run_paper_log(
        config,
        odds_client=odds_client,
        matchbook_client=FakeMatchbookClient(),
        dynamodb_table=FakeTable(),
        s3_client=FakeS3Client(),
        now=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
    )

    assert result["sports"] == 2
    assert odds_client.odds_calls == 2


def test_matchbook_sharp_h2h_config_defaults_to_higher_edge() -> None:
    config = strategy_runner.config_from_event({"strategy": "matchbook-sharp-h2h"})

    assert config.strategy == "matchbook-sharp-h2h"
    assert config.min_edge == 0.015


def test_strategy_runner_default_markets_include_soccer_line_markets() -> None:
    config = strategy_runner.config_from_event({})

    assert config.markets == "h2h,totals,spreads"


def test_run_strategy_mode_combined_defaults_to_active_soccer_only(monkeypatch) -> None:
    monkeypatch.setitem(strategy_runner.SPORT_PROFILES, "test-profile", ["soccer_epl"])
    table = FakeTable()
    odds_client = FakeMultiSportOddsClient()
    s3_client = FakeS3Client()

    result = strategy_runner.run_strategy_mode(
        {
            "mode": "paper-log-combined",
            "odds_api_key": "test-key",
            "dynamodb_table_name": "paper-trades",
            "odds_s3_bucket": "odds-bucket",
            "sports_profile": "test-profile",
            "strategy": "exchange-clv",
            "max_api_requests": 10,
            "min_reference_books": 2,
            "use_betfair_lambda": False,
        },
        odds_client=odds_client,
        matchbook_client=FakeMatchbookClient(),
        dynamodb_table=table,
        s3_client=s3_client,
        now=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
    )

    assert result["mode"] == "paper-log-combined"
    assert result["branches"]["soccer"]["sports"] == 2
    assert result["branches"]["matchbook_discovery"]["sports"] == 0
    assert odds_client.requested_sports == ["soccer_epl", "soccer_fa_cup"]
    assert result["paper_log"]["inserted"] == 2
    assert len(table.items) == 2
    assert {
        item["sport_key"]
        for item in table.items.values()
    } == {"soccer_epl", "soccer_fa_cup"}
    assert s3_client.uploads[0][2].rsplit("hour=", maxsplit=1)[0] == (
        "odds_snapshots/soccer/snapshot_date=2026-08-14/"
    )
    assert (
        "odds-bucket",
        "summaries/latest_combined_strategy_runner_summary.json",
    ) in s3_client.objects


def test_run_strategy_mode_combined_can_enable_matchbook_discovery(monkeypatch) -> None:
    monkeypatch.setitem(strategy_runner.SPORT_PROFILES, "test-profile", ["soccer_epl"])
    table = FakeTable()
    odds_client = FakeMultiSportOddsClient()

    result = strategy_runner.run_strategy_mode(
        {
            "mode": "paper-log-combined",
            "odds_api_key": "test-key",
            "dynamodb_table_name": "paper-trades",
            "odds_s3_bucket": "odds-bucket",
            "sports_profile": "test-profile",
            "strategy": "exchange-clv",
            "max_api_requests": 10,
            "min_reference_books": 2,
            "use_betfair_lambda": False,
            "enable_matchbook_discovery": True,
        },
        odds_client=odds_client,
        matchbook_client=FakeMatchbookClient(),
        dynamodb_table=table,
        s3_client=FakeS3Client(),
        now=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
    )

    assert result["branches"]["soccer"]["sports"] == 2
    assert result["branches"]["matchbook_discovery"]["sports"] == 1
    assert odds_client.requested_sports == ["soccer_epl", "soccer_fa_cup", "basketball_nba"]
    assert {item["sport_key"] for item in table.items.values()} == {
        "soccer_epl",
        "soccer_fa_cup",
        "basketball_nba",
    }


def test_run_strategy_mode_can_clear_paper_trades() -> None:
    table = FakeTable()
    table.items = {
        "paper#1": {"trade_id": "paper#1", "status": "open"},
        "paper#2": {"trade_id": "paper#2", "status": "settled"},
    }

    result = strategy_runner.run_strategy_mode(
        {
            "mode": "clear-paper-trades",
            "dynamodb_table_name": "paper-trades",
        },
        dynamodb_table=table,
    )

    assert result == {
        "mode": "clear-paper-trades",
        "table": "paper-trades",
        "deleted": 2,
    }
    assert table.items == {}


def test_run_paper_log_updates_and_settles_existing_open_trade(monkeypatch) -> None:
    monkeypatch.setitem(strategy_runner.SPORT_PROFILES, "test-profile", ["soccer_epl"])
    table = FakeTable()
    odds_client = FakeOddsClient()
    seeded_config = StrategyRunnerConfig(
        mode="paper-log",
        odds_api_key="test-key",
        dynamodb_table_name="paper-trades",
        odds_s3_bucket="odds-bucket",
        sports_profile="test-profile",
        max_api_requests=1,
        min_reference_books=2,
        use_betfair_lambda=False,
    )
    first_result = run_paper_log(
        seeded_config,
        odds_client=odds_client,
        matchbook_client=FakeMatchbookClient(),
        dynamodb_table=table,
        s3_client=FakeS3Client(),
        now=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
    )
    assert first_result["paper_log"]["inserted"] == 1

    second_odds_client = FakeOddsClient()
    second_result = run_paper_log(
        seeded_config,
        odds_client=second_odds_client,
        matchbook_client=FakeMatchbookClient(),
        dynamodb_table=table,
        s3_client=FakeS3Client(),
        now=datetime(2026, 8, 14, 12, 5, tzinfo=timezone.utc),
    )

    assert second_result["closing_update"]["open_trades"] == 1
    assert second_result["closing_update"]["updated"] == 1
    assert second_result["settlement"]["open_trades"] == 1
    assert second_result["settlement"]["settled"] == 1
    assert second_result["paper_log"]["duplicates"] == 1
    assert second_result["portfolio_summary"]["settled_trades"] == 1
    assert second_result["portfolio_summary"]["settled_profit"] == 3.136
    assert second_odds_client.odds_calls == 1
    assert second_odds_client.score_calls == [("soccer_epl", 3)]
    item = next(iter(table.items.values()))
    assert item["status"] == "settled"
    assert item["result"] == "Arsenal"


def test_run_paper_log_does_not_log_exchange_clv_longshots(monkeypatch) -> None:
    monkeypatch.setitem(strategy_runner.SPORT_PROFILES, "test-profile", ["soccer_epl"])
    table = FakeTable()
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
        odds_client=FakeLongshotOddsClient(),
        matchbook_client=FakeMatchbookClient(),
        dynamodb_table=table,
        s3_client=FakeS3Client(),
        now=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
    )

    assert result["candidate_signals"] == 0
    assert result["paper_log"]["inserted"] == 0
    assert table.items == {}


def test_run_paper_log_skips_new_trades_when_trading_is_paused(monkeypatch) -> None:
    monkeypatch.setitem(strategy_runner.SPORT_PROFILES, "test-profile", ["soccer_epl"])
    table = FakeTable()
    table.put_item(
        Item={
            "trade_id": "control#trading",
            "status": "control",
            "control_type": "trading",
            "paused": True,
            "updated_at": "2026-08-14T11:00:00+00:00",
            "updated_by": "test",
        }
    )
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

    assert result["trading_control"]["paused"] is True
    assert result["candidate_signals"] == 0
    assert result["paper_log"]["inserted"] == 0
    assert result["portfolio_summary"]["total_trades"] == 0
    assert list(table.items) == ["control#trading"]
