from __future__ import annotations

import argparse
import csv
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TABLES = (
    ("h2h_trades", "data/paper_trades_h2h.csv", "replace", "csv"),
    ("h2h_liquidity", "data/matchbook_liquidity_snapshots_h2h.csv", "replace", "csv"),
    ("h2h_summary", "data/paper_summary_h2h.md", "replace", "summary"),
    ("spreads_trades", "data/paper_trades_spreads.csv", "replace", "csv"),
    (
        "spreads_liquidity",
        "data/matchbook_liquidity_snapshots_spreads.csv",
        "replace",
        "csv",
    ),
    ("spreads_summary", "data/paper_summary_spreads.md", "replace", "summary"),
)

SUMMARY_HEADERS = ["generated_at", "metric", "value"]


def main() -> None:
    args = parse_args()
    webhook_url = args.webhook_url or os.environ.get("SHEETS_WEBHOOK_URL", "")
    if not webhook_url:
        raise SystemExit("SHEETS_WEBHOOK_URL is required")

    payload = build_payload(
        secret=args.secret or os.environ.get("SHEETS_WEBHOOK_SECRET", ""),
        generated_at=args.generated_at or datetime.now(timezone.utc).isoformat(),
        run_id=args.run_id or os.environ.get("GITHUB_RUN_ID", ""),
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        repository=os.environ.get("GITHUB_REPOSITORY", ""),
    )
    post_payload(webhook_url, payload, timeout_seconds=args.timeout_seconds)
    print(f"Posted {len(payload['tables'])} tables to Google Sheets webhook.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Post paper-trading CSVs and summaries to a Google Apps Script webhook."
    )
    parser.add_argument("--webhook-url", default="")
    parser.add_argument("--secret", default="")
    parser.add_argument("--generated-at", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    return parser.parse_args()


def build_payload(
    *,
    secret: str,
    generated_at: str,
    run_id: str = "",
    run_attempt: str = "",
    repository: str = "",
) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    for name, path_text, mode, table_type in DEFAULT_TABLES:
        path = Path(path_text)
        if table_type == "summary":
            headers = SUMMARY_HEADERS
            rows = read_summary(path, generated_at=generated_at)
        else:
            headers, rows = read_csv(path)
        tables.append(
            {
                "name": name,
                "mode": mode,
                "headers": headers,
                "rows": rows,
            }
        )

    return {
        "secret": secret,
        "generated_at": generated_at,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "repository": repository,
        "tables": tables,
    }


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists() or path.stat().st_size == 0:
        return [], []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or [])
        return headers, [dict(row) for row in reader]


def read_summary(path: Path, *, generated_at: str) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        metric, value = line[2:].split(":", 1)
        rows.append(
            {
                "generated_at": generated_at,
                "metric": metric.strip(),
                "value": value.strip(),
            }
        )
    return rows


def post_payload(webhook_url: str, payload: dict[str, Any], *, timeout_seconds: float) -> None:
    encoded = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8", errors="replace")
        if response.status >= 400:
            raise RuntimeError(f"Sheets webhook failed with {response.status}: {body}")
        print(f"Sheets webhook response: {response.status} {body[:200]}")


if __name__ == "__main__":
    main()
