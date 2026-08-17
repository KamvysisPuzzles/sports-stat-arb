from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> None:
    args = parse_args()
    trades = read_csv(args.paper_csv)
    opportunities = read_csv(args.opportunities_csv) if args.opportunities_csv.exists() else []
    new_trades_count = read_count(args.new_trades_count_file)

    markdown = build_markdown(
        trades,
        opportunities,
        title=args.title,
        new_trades_count=new_trades_count,
        opportunity_dedupe_key=args.opportunity_dedupe_key,
        scan_kind=args.scan_kind,
    )
    text = build_text(
        trades,
        opportunities,
        new_trades_count=new_trades_count,
        opportunity_dedupe_key=args.opportunity_dedupe_key,
        scan_kind=args.scan_kind,
    )
    args.markdown_out.write_text(markdown)
    args.text_out.write_text(text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="Live Paper Trading Summary")
    parser.add_argument("--paper-csv", type=Path, required=True)
    parser.add_argument("--opportunities-csv", type=Path, required=True)
    parser.add_argument("--new-trades-count-file", type=Path)
    parser.add_argument(
        "--opportunity-dedupe-key",
        choices=["event-market", "event-market-outcome"],
        default="event-market",
        help="How to decide whether a latest-scan opportunity is already booked.",
    )
    parser.add_argument(
        "--scan-kind",
        choices=["liquidity", "price"],
        default="liquidity",
        help="Use price for CLV-only scans without executable-liquidity data.",
    )
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--text-out", type=Path, required=True)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def read_count(path: Path | None) -> int | None:
    if not path or not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def build_markdown(
    trades: list[dict[str, str]],
    opportunities: list[dict[str, str]],
    *,
    title: str = "Live Paper Trading Summary",
    new_trades_count: int | None = None,
    opportunity_dedupe_key: str = "event-market",
    scan_kind: str = "liquidity",
) -> str:
    now = datetime.now(timezone.utc)
    portfolio_trades = [row for row in trades if row.get("liquidity_status") == "available"]
    open_trades = [row for row in portfolio_trades if row.get("status") == "open"]
    settled = [row for row in portfolio_trades if row.get("status") == "settled"]
    trades_last_24h = [
        row for row in portfolio_trades if _logged_within(row, now, timedelta(hours=24))
    ]
    clv_rows = [
        row for row in portfolio_trades if row.get("target_clv") and _trade_has_closed(row, now)
    ]
    clv_counts = _clv_counts(clv_rows)
    profit = sum(float(row.get("profit") or 0) for row in settled)
    staked = sum(float(row.get("stake") or 0) for row in settled)
    roi = profit / staked if staked else 0.0
    won_bets = sum(1 for row in settled if _float(row.get("profit")) > 0)
    lost_bets = len(settled) - won_bets
    booked_stake = sum(_float(row.get("stake")) for row in portfolio_trades)
    open_stake = sum(_float(row.get("stake")) for row in open_trades)
    booked_ev = sum(
        _float(row.get("stake")) * _float(row.get("edge")) for row in portfolio_trades
    )
    open_ev = sum(_float(row.get("stake")) * _float(row.get("edge")) for row in open_trades)
    booked_average_odds = _average(row.get("target_odds") for row in portfolio_trades)
    settled_average_odds = _average(row.get("target_odds") for row in settled)
    booked_average_liquidity = _average(
        row.get("available_at_or_above_target") for row in portfolio_trades
    )
    settled_average_liquidity = _average(
        row.get("available_at_or_above_target") for row in settled
    )
    booked_weighted_edge = booked_ev / booked_stake if booked_stake else 0.0
    open_weighted_edge = open_ev / open_stake if open_stake else 0.0
    unbooked_opportunities = _exclude_booked_opportunities(
        opportunities,
        trades,
        dedupe_key=opportunity_dedupe_key,
    )
    available_opportunities = [
        row for row in unbooked_opportunities if row.get("liquidity_status") == "available"
    ]
    scan_lines = _scan_summary_lines(
        opportunities=opportunities,
        unbooked_opportunities=unbooked_opportunities,
        available_opportunities=available_opportunities,
        new_trades_count=new_trades_count,
        scan_kind=scan_kind,
    )

    lines = [f"# {title}", ""]
    lines.extend(
        [
            "## Latest Scan",
            "",
            *scan_lines,
            "",
            "## Paper Portfolio",
            "",
            "- Scope: liquidity-confirmed trades only",
            f"- Total trades booked: {len(portfolio_trades)}",
            f"- Trades booked last 24h: {len(trades_last_24h)}",
            f"- Average booked odds: {booked_average_odds:.2f}",
            f"- Average confirmed liquidity at target: {booked_average_liquidity:.2f}",
            f"- Total paper stake deployed: {booked_stake:.2f}",
            f"- Total booked theoretical EV: {booked_ev:.2f}",
            f"- Total booked weighted edge: {booked_weighted_edge:.2%}",
            f"- Open trades: {len(open_trades)}",
            f"- Open paper stake deployed: {open_stake:.2f}",
            f"- Open booked theoretical EV: {open_ev:.2f}",
            f"- Open booked weighted edge: {open_weighted_edge:.2%}",
            "",
            "## Results And CLV",
            "",
            "- Scope: liquidity-confirmed trades only",
            f"- Settled trades: {len(settled)}",
            f"- Settled won/lost bets: {won_bets}/{lost_bets}",
            f"- Settled average booked odds: {settled_average_odds:.2f}",
            f"- Settled average confirmed liquidity at target: {settled_average_liquidity:.2f}",
            f"- Settled profit: {profit:.2f}",
            f"- Settled ROI: {roi:.2%}",
            *_format_clv_lines(clv_counts),
            "",
        ]
    )
    if unbooked_opportunities:
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
                for row in unbooked_opportunities[:10]
            ]
        )
        if len(unbooked_opportunities) > 10:
            lines.append(f"- ...and {len(unbooked_opportunities) - 10} more")
        lines.append("")
    if trades:
        lines.extend(["## Booked Trades", ""])
        lines.extend(
            [
                (
                    f"- {row.get('bet_to_place', '') or _fallback_bet(row)} | "
                    f"{row.get('event_name', '')} | stake {_float(row.get('stake')):.2f} | "
                    f"edge {_float(row.get('edge')):.2%} | "
                    f"EV {_float(row.get('stake')) * _float(row.get('edge')):.2f} | "
                    f"status {row.get('status', '')} | starts {row.get('commence_time', '')}"
                )
                for row in sorted(
                    trades,
                    key=lambda item: item.get("logged_at", ""),
                    reverse=True,
                )[:10]
            ]
        )
        if len(trades) > 10:
            lines.append(f"- ...and {len(trades) - 10} more")
        lines.append("")
    return "\n".join(lines)


def build_text(
    trades: list[dict[str, str]],
    opportunities: list[dict[str, str]],
    *,
    new_trades_count: int | None = None,
    opportunity_dedupe_key: str = "event-market",
    scan_kind: str = "liquidity",
) -> str:
    return "\n".join(
        line.lstrip("# ").lstrip("- ")
        for line in build_markdown(
            trades,
            opportunities,
            new_trades_count=new_trades_count,
            opportunity_dedupe_key=opportunity_dedupe_key,
            scan_kind=scan_kind,
        ).splitlines()
    )


def _float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _average(values: object) -> float:
    floats = [_float(value) for value in values]
    return sum(floats) / len(floats) if floats else 0.0


def _fallback_bet(row: dict[str, str]) -> str:
    outcome = row.get("outcome_name", "")
    bookmaker = row.get("target_bookmaker", "")
    odds = row.get("target_odds", "")
    return f"Back {outcome} with {bookmaker} at {odds}".strip()


def _scan_summary_lines(
    *,
    opportunities: list[dict[str, str]],
    unbooked_opportunities: list[dict[str, str]],
    available_opportunities: list[dict[str, str]],
    new_trades_count: int | None,
    scan_kind: str,
) -> list[str]:
    new_trades_line = (
        f"- Newly booked trades: {new_trades_count}"
        if new_trades_count is not None
        else "- Newly booked trades: unknown"
    )
    if scan_kind == "price":
        nominal_ev = sum(_float(row.get("edge")) for row in opportunities)
        average_edge = nominal_ev / len(opportunities) if opportunities else 0.0
        return [
            f"- Candidate price signals this scan: {len(opportunities)}",
            new_trades_line,
            f"- Candidate average edge: {average_edge:.2%}",
            f"- Candidate nominal EV at 1 GBP stake: {nominal_ev:.2f}",
            f"- Unbooked price signals shown below: {len(unbooked_opportunities)}",
        ]

    visible_liquidity = sum(
        _float(row.get("available_at_or_above_target")) for row in available_opportunities
    )
    executable_ev = sum(
        _float(row.get("available_at_or_above_target")) * _float(row.get("edge"))
        for row in available_opportunities
    )
    liquidity_weighted_edge = executable_ev / visible_liquidity if visible_liquidity else 0.0
    return [
        f"- Candidate rows this scan: {len(opportunities)}",
        f"- Unbooked opportunities shown: {len(unbooked_opportunities)}",
        f"- Executable unbooked rows: {len(available_opportunities)}",
        new_trades_line,
        f"- Executable unbooked liquidity found: {visible_liquidity:.2f}",
        f"- Liquidity-weighted unbooked scan edge: {liquidity_weighted_edge:.2%}",
        f"- Unbooked scan theoretical EV: {executable_ev:.2f}",
    ]


def _clv_counts(rows: list[dict[str, str]]) -> dict[str, float]:
    counts = {
        "beat": 0,
        "tie": 0,
        "miss": 0,
        "total": 0,
        "total_clv": 0.0,
        "beat_clv": 0.0,
        "miss_clv": 0.0,
        "tie_clv": 0.0,
    }
    for row in rows:
        target_clv = _float(row.get("target_clv"))
        counts["total"] += 1
        counts["total_clv"] += target_clv
        if target_clv > 0:
            counts["beat"] += 1
            counts["beat_clv"] += target_clv
        elif target_clv < 0:
            counts["miss"] += 1
            counts["miss_clv"] += target_clv
        else:
            counts["tie"] += 1
            counts["tie_clv"] += target_clv
    return counts


def _format_clv_lines(counts: dict[str, float]) -> list[str]:
    total = int(counts["total"])
    if total == 0:
        return ["- Beat closing line: pending (0 closed trades with CLV)"]
    beat_rate = counts["beat"] / total
    miss_rate = counts["miss"] / total
    tie_rate = counts["tie"] / total
    average_clv = counts["total_clv"] / total
    average_beat_clv = counts["beat_clv"] / counts["beat"] if counts["beat"] else 0.0
    average_miss_clv = counts["miss_clv"] / counts["miss"] if counts["miss"] else 0.0
    average_tie_clv = counts["tie_clv"] / counts["tie"] if counts["tie"] else 0.0
    return [
        f"- Beat closing line: {beat_rate:.2%} ({int(counts['beat'])}/{total})",
        f"- Missed closing line: {miss_rate:.2%} ({int(counts['miss'])}/{total})",
        f"- Tied closing line: {tie_rate:.2%} ({int(counts['tie'])}/{total})",
        f"- Average CLV per closed trade: {average_clv:.2%}",
        (
            "- CLV breakdown: "
            f"beat avg {average_beat_clv:.2%}, "
            f"miss avg {average_miss_clv:.2%}, "
            f"tie avg {average_tie_clv:.2%}"
        ),
    ]


def _format_clv_line(rate: float | None, wins: int, total: int) -> str:
    if total == 0:
        return "- Beat closing line: pending (0 closed trades with CLV)"
    return f"- Beat closing line: {rate:.2%} ({wins}/{total})"


def _exclude_booked_opportunities(
    opportunities: list[dict[str, str]],
    trades: list[dict[str, str]],
    *,
    dedupe_key: str,
) -> list[dict[str, str]]:
    booked_keys = {_opportunity_key(row, dedupe_key=dedupe_key) for row in trades}
    return [
        row
        for row in opportunities
        if _opportunity_key(row, dedupe_key=dedupe_key) not in booked_keys
    ]


def _opportunity_key(row: dict[str, str], *, dedupe_key: str) -> tuple[str, ...]:
    key = (
        _normalize(row.get("event_id")),
        _normalize(row.get("market") or row.get("market_key")),
    )
    if dedupe_key == "event-market-outcome":
        return (*key, _normalize(row.get("outcome_name")))
    return key


def _normalize(value: str | None) -> str:
    return (value or "").casefold()


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
