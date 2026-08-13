from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> None:
    args = parse_args()
    trades = read_csv(args.paper_csv)
    opportunities = read_csv(args.opportunities_csv) if args.opportunities_csv.exists() else []

    markdown = build_markdown(trades, opportunities, title=args.title)
    text = build_text(trades, opportunities)
    args.markdown_out.write_text(markdown)
    args.text_out.write_text(text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="Live Paper Trading Summary")
    parser.add_argument("--paper-csv", type=Path, required=True)
    parser.add_argument("--opportunities-csv", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--text-out", type=Path, required=True)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def build_markdown(
    trades: list[dict[str, str]],
    opportunities: list[dict[str, str]],
    *,
    title: str = "Live Paper Trading Summary",
) -> str:
    now = datetime.now(timezone.utc)
    open_trades = [row for row in trades if row.get("status") == "open"]
    settled = [row for row in trades if row.get("status") == "settled"]
    trades_last_24h = [row for row in trades if _logged_within(row, now, timedelta(hours=24))]
    clv_rows = [row for row in trades if row.get("target_clv") and _trade_has_closed(row, now)]
    beat_close = [row for row in clv_rows if row.get("beat_closing_line") == "True"]
    profit = sum(float(row.get("profit") or 0) for row in settled)
    staked = sum(float(row.get("stake") or 0) for row in settled)
    roi = profit / staked if staked else 0.0
    beat_close_rate = len(beat_close) / len(clv_rows) if clv_rows else 0.0
    available_opportunities = [
        row for row in opportunities if row.get("liquidity_status") == "available"
    ]
    visible_liquidity = sum(
        _float(row.get("available_at_or_above_target")) for row in available_opportunities
    )
    executable_ev = sum(
        _float(row.get("available_at_or_above_target")) * _float(row.get("edge"))
        for row in available_opportunities
    )
    liquidity_weighted_edge = executable_ev / visible_liquidity if visible_liquidity else 0.0

    lines = [
        f"# {title}",
        "",
        f"- New opportunities this run: {len(opportunities)}",
        f"- Matchbook executable rows: {len(available_opportunities)}",
        f"- Visible Matchbook liquidity: {visible_liquidity:.2f}",
        f"- Liquidity-weighted edge: {liquidity_weighted_edge:.2%}",
        f"- Theoretical executable EV: {executable_ev:.2f}",
        f"- Total trades: {len(trades)}",
        f"- Trades logged last 24h: {len(trades_last_24h)}",
        f"- Open trades: {len(open_trades)}",
        f"- Settled trades: {len(settled)}",
        f"- Settled profit: {profit:.2f}",
        f"- Settled ROI: {roi:.2%}",
        f"- Beat closing line: {beat_close_rate:.2%} ({len(beat_close)}/{len(clv_rows)})",
        "",
    ]
    if opportunities:
        lines.extend(["## Opportunities", ""])
        lines.extend(
            [
                (
                    f"- {row.get('bet_to_place', '')} | {row.get('event_name', '')} | "
                    f"edge {_float(row.get('edge')):.2%} | "
                    f"liquidity {_float(row.get('available_at_or_above_target')):.2f} | "
                    f"status {row.get('liquidity_status', '')} | "
                    f"starts {row.get('commence_time', '')}"
                )
                for row in opportunities[:10]
            ]
        )
        if len(opportunities) > 10:
            lines.append(f"- ...and {len(opportunities) - 10} more")
        lines.append("")
    return "\n".join(lines)


def build_text(trades: list[dict[str, str]], opportunities: list[dict[str, str]]) -> str:
    return "\n".join(line.lstrip("# ").lstrip("- ") for line in build_markdown(trades, opportunities).splitlines())


def _float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _trade_has_closed(row: dict[str, str], now: datetime) -> bool:
    if row.get("status") == "settled":
        return True
    try:
        commence_time = datetime.fromisoformat(row.get("commence_time", ""))
    except ValueError:
        return False
    if commence_time.tzinfo is None:
        commence_time = commence_time.replace(tzinfo=timezone.utc)
    return commence_time <= now


def _logged_within(row: dict[str, str], now: datetime, window: timedelta) -> bool:
    try:
        logged_at = datetime.fromisoformat(row.get("logged_at", ""))
    except ValueError:
        return False
    if logged_at.tzinfo is None:
        logged_at = logged_at.replace(tzinfo=timezone.utc)
    return now - window <= logged_at <= now


if __name__ == "__main__":
    main()
