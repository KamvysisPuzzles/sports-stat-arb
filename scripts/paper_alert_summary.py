from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> None:
    args = parse_args()
    trades = read_csv(args.paper_csv)
    opportunities = read_csv(args.opportunities_csv) if args.opportunities_csv.exists() else []

    markdown = build_markdown(trades, opportunities)
    text = build_text(trades, opportunities)
    args.markdown_out.write_text(markdown)
    args.text_out.write_text(text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
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


def build_markdown(trades: list[dict[str, str]], opportunities: list[dict[str, str]]) -> str:
    open_trades = [row for row in trades if row.get("status") == "open"]
    settled = [row for row in trades if row.get("status") == "settled"]
    clv_rows = [row for row in trades if row.get("target_clv")]
    beat_close = [row for row in clv_rows if row.get("beat_closing_line") == "True"]
    profit = sum(float(row.get("profit") or 0) for row in settled)
    staked = sum(float(row.get("stake") or 0) for row in settled)
    roi = profit / staked if staked else 0.0
    beat_close_rate = len(beat_close) / len(clv_rows) if clv_rows else 0.0

    lines = [
        "# Live Paper Trading Summary",
        "",
        f"- New opportunities this run: {len(opportunities)}",
        f"- Total trades: {len(trades)}",
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
                    f"edge {float(row.get('edge') or 0):.2%} | starts {row.get('commence_time', '')}"
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


if __name__ == "__main__":
    main()
