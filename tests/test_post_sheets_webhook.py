from __future__ import annotations

from pathlib import Path

from scripts import post_sheets_webhook


def test_build_payload_reads_csvs_and_summaries(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "paper_trades_h2h.csv").write_text(
        "event_id,market_key,stake\nabc,h2h,12.50\n",
        encoding="utf-8",
    )
    (data_dir / "matchbook_liquidity_snapshots_h2h.csv").write_text(
        "snapshot_time,event_id,available_at_or_above_target\n2026-08-13T10:00:00+00:00,abc,12.50\n",
        encoding="utf-8",
    )
    (data_dir / "paper_summary_h2h.md").write_text(
        "# Matchbook h2h Paper Trading Summary\n\n"
        "- New opportunities this run: 1\n"
        "- Visible Matchbook liquidity: 12.50\n",
        encoding="utf-8",
    )
    (data_dir / "paper_trades_spreads.csv").write_text("", encoding="utf-8")
    (data_dir / "paper_summary_spreads.md").write_text("", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    payload = post_sheets_webhook.build_payload(
        secret="secret",
        generated_at="2026-08-13T12:00:00+00:00",
        run_id="123",
        run_attempt="1",
        repository="KamvysisPuzzles/sports-stat-arb",
    )

    assert payload["secret"] == "secret"
    assert payload["run_id"] == "123"

    tables = {table["name"]: table for table in payload["tables"]}
    assert tables["h2h_trades"]["headers"] == ["event_id", "market_key", "stake"]
    assert tables["h2h_trades"]["rows"] == [
        {"event_id": "abc", "market_key": "h2h", "stake": "12.50"}
    ]
    assert tables["h2h_summary"]["headers"] == post_sheets_webhook.SUMMARY_HEADERS
    assert tables["h2h_summary"]["rows"] == [
        {
            "generated_at": "2026-08-13T12:00:00+00:00",
            "metric": "New opportunities this run",
            "value": "1",
        },
        {
            "generated_at": "2026-08-13T12:00:00+00:00",
            "metric": "Visible Matchbook liquidity",
            "value": "12.50",
        },
    ]
    assert tables["spreads_liquidity"]["headers"] == []
    assert tables["spreads_liquidity"]["rows"] == []
