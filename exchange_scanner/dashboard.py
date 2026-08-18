from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def dashboard_payload(
    table: Any,
    *,
    filters: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    filters = filters or {}
    trades = [_normalise_item(item) for item in _scan_all(table)]
    filtered = _apply_filters(trades, filters)
    return {
        "generated_at": now.isoformat(),
        "filters": {key: value for key, value in filters.items() if value},
        "summary": _summary(filtered),
        "all_summary": _summary(trades),
        "venue_results": _venue_results(filtered),
        "trades": sorted(
            filtered,
            key=lambda item: item.get("logged_at", ""),
            reverse=True,
        ),
    }


def render_dashboard_html(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    all_summary = payload["all_summary"]
    rows = payload["trades"][:200]
    generated = _escape(payload["generated_at"])
    active_filters = payload.get("filters", {})
    token = str(payload.get("token", ""))
    filter_label = (
        ", ".join(f"{key}={value}" for key, value in active_filters.items())
        if active_filters
        else "none"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sports Stat Arb Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101418;
      --panel: #171d23;
      --panel-2: #1e252d;
      --text: #f0f4f8;
      --muted: #9aa8b5;
      --line: #2d3742;
      --good: #3ecf8e;
      --bad: #ff6b6b;
      --warn: #ffd166;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      padding: 20px 16px 10px;
      border-bottom: 1px solid var(--line);
      background: #121820;
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    h2 {{ margin: 0 0 8px; font-size: 16px; }}
    .meta {{ color: var(--muted); font-size: 13px; line-height: 1.4; }}
    main {{ padding: 16px; max-width: 1200px; margin: 0 auto; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
    }}
    .label {{ color: var(--muted); font-size: 12px; margin-bottom: 6px; }}
    .value {{ font-size: 22px; font-weight: 700; }}
    .good {{ color: var(--good); }}
    .bad {{ color: var(--bad); }}
    .warn {{ color: var(--warn); }}
    .filters {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 0 0 16px;
    }}
    .filters a {{
      color: var(--text);
      text-decoration: none;
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 10px;
      font-size: 13px;
    }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }}
    .venue-section {{ margin: 0 0 16px; }}
    .venue-table {{ min-width: 760px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 980px; }}
    th, td {{
      text-align: left;
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      white-space: nowrap;
    }}
    th {{
      color: var(--muted);
      background: #151b22;
      position: sticky;
      top: 0;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    @media (max-width: 720px) {{
      header {{ position: static; }}
      main {{ padding: 12px; }}
      .value {{ font-size: 20px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Sports Stat Arb Dashboard</h1>
    <div class="meta">Generated {generated}<br>Active filters: {_escape(filter_label)}</div>
  </header>
  <main>
    {_metrics_html(summary, all_summary)}
    <nav class="filters">
      <a href="{_href_attr(_filter_href(token))}">All</a>
      <a href="{_href_attr(_filter_href(token, status='open'))}">Open</a>
      <a href="{_href_attr(_filter_href(token, status='settled'))}">Settled</a>
      <a href="{_href_attr(_filter_href(token, bookmaker='Matchbook'))}">Matchbook</a>
      <a href="{_href_attr(_filter_href(token, bookmaker='Betfair'))}">Betfair</a>
      <a href="{_href_attr(_filter_href(token, format='json'))}">JSON</a>
    </nav>
    {_venue_results_html(payload.get("venue_results", []))}
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Logged</th>
            <th>Status</th>
            <th>Book</th>
            <th>Event</th>
            <th>Selection</th>
            <th>Odds</th>
            <th>Liquidity</th>
            <th>Edge</th>
            <th>CLV</th>
            <th>Profit</th>
            <th>Starts</th>
          </tr>
        </thead>
        <tbody>
          {_trade_rows_html(rows)}
        </tbody>
      </table>
    </div>
  </main>
</body>
</html>
"""


def _metrics_html(summary: dict[str, Any], all_summary: dict[str, Any]) -> str:
    return f"""<section class="grid">
      {_metric("Trades", summary["total_trades"], f"all {all_summary['total_trades']}")}
      {_metric("Open", summary["open_trades"])}
      {_metric("Settled", summary["settled_trades"])}
      {_metric("Won/Lost", f"{summary['settled_won']}/{summary['settled_lost']}")}
      {_metric("PnL", f"{summary['settled_profit']:.2f}", _class_for_number(summary["settled_profit"]))}
      {_metric("ROI", f"{summary['settled_roi']:.2%}", _class_for_number(summary["settled_roi"]))}
      {_metric("Avg Odds", f"{summary['average_booked_odds']:.2f}")}
      {_metric("Median Liquidity", f"{summary['median_confirmed_liquidity_at_target']:.2f}")}
      {_metric("Avg CLV", f"{summary['average_clv']:.2%}", _class_for_number(summary["average_clv"]))}
      {_metric("CLV B/M/T", f"{summary['beat_closing_line']}/{summary['missed_closing_line']}/{summary['tied_closing_line']}")}
    </section>"""


def _metric(label: str, value: object, extra: str = "") -> str:
    class_name = extra if extra in {"good", "bad", "warn"} else ""
    subtitle = "" if extra in {"", "good", "bad", "warn"} else f'<div class="label">{_escape(extra)}</div>'
    return (
        '<div class="metric">'
        f'<div class="label">{_escape(label)}</div>'
        f'<div class="value {class_name}">{_escape(value)}</div>'
        f"{subtitle}</div>"
    )


def _trade_rows_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="11">No trades match the current filters.</td></tr>'
    return "\n".join(
        "<tr>"
        f"<td>{_short_time(row.get('logged_at'))}</td>"
        f"<td>{_escape(row.get('status', ''))}</td>"
        f"<td>{_escape(row.get('target_bookmaker', ''))}</td>"
        f"<td>{_escape(row.get('event_name', ''))}</td>"
        f"<td>{_escape(row.get('outcome_name', ''))}</td>"
        f"<td>{_format_number(row.get('target_odds'))}</td>"
        f"<td>{_format_number(row.get('available_at_or_above_target'))}</td>"
        f"<td>{_format_pct(row.get('edge'))}</td>"
        f"<td>{_format_pct(row.get('target_clv'))}</td>"
        f"<td>{_format_number(row.get('profit'))}</td>"
        f"<td>{_short_time(row.get('commence_time'))}</td>"
        "</tr>"
        for row in rows
    )


def _venue_results_html(venues: list[dict[str, Any]]) -> str:
    rows = _venue_rows_html(venues)
    return f"""<section class="venue-section">
      <h2>Results by Venue</h2>
      <div class="table-wrap venue-wrap">
        <table class="venue-table">
          <thead>
            <tr>
              <th>Venue</th>
              <th>Trades</th>
              <th>Open</th>
              <th>Settled</th>
              <th>Won/Lost</th>
              <th>PnL</th>
              <th>ROI</th>
              <th>Median Liquidity</th>
              <th>Avg CLV</th>
            </tr>
          </thead>
          <tbody>
            {rows}
          </tbody>
        </table>
      </div>
    </section>"""


def _venue_rows_html(venues: list[dict[str, Any]]) -> str:
    if not venues:
        return '<tr><td colspan="9">No venue results for the current filters.</td></tr>'
    return "\n".join(
        "<tr>"
        f"<td>{_escape(row['venue'])}</td>"
        f"<td>{_escape(row['total_trades'])}</td>"
        f"<td>{_escape(row['open_trades'])}</td>"
        f"<td>{_escape(row['settled_trades'])}</td>"
        f"<td>{_escape(row['settled_won'])}/{_escape(row['settled_lost'])}</td>"
        f"<td class='{_class_for_number(row['settled_profit'])}'>{row['settled_profit']:.2f}</td>"
        f"<td class='{_class_for_number(row['settled_roi'])}'>{row['settled_roi']:.2%}</td>"
        f"<td>{row['median_confirmed_liquidity_at_target']:.2f}</td>"
        f"<td class='{_class_for_number(row['average_clv'])}'>{row['average_clv']:.2%}</td>"
        "</tr>"
        for row in venues
    )


def _filter_href(token: str, **params: str) -> str:
    pairs = [("token", token), *params.items()]
    query = "&".join(
        f"{_url_escape(key)}={_url_escape(value)}"
        for key, value in pairs
        if value
    )
    return f"?{query}" if query else "?"


def _href_attr(value: str) -> str:
    return html.escape(value, quote=True)


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [item for item in trades if item.get("status") == "settled"]
    open_trades = [item for item in trades if item.get("status") == "open"]
    clv_rows = [item for item in trades if item.get("target_clv") not in {None, ""}]
    staked = sum(_float(item.get("stake")) for item in settled)
    profit = sum(_float(item.get("profit")) for item in settled)
    wins = sum(1 for item in settled if _float(item.get("profit")) > 0)
    losses = len(settled) - wins
    beat = [item for item in clv_rows if _float(item.get("target_clv")) > 0]
    miss = [item for item in clv_rows if _float(item.get("target_clv")) < 0]
    tie = len(clv_rows) - len(beat) - len(miss)
    return {
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "settled_trades": len(settled),
        "settled_won": wins,
        "settled_lost": losses,
        "settled_profit": profit,
        "settled_roi": profit / staked if staked else 0.0,
        "average_booked_odds": _average(_float(item.get("target_odds")) for item in trades),
        "median_confirmed_liquidity_at_target": _median(
            _float(item.get("available_at_or_above_target"))
            for item in trades
            if item.get("available_at_or_above_target") not in {None, ""}
        ),
        "average_clv": _average(_float(item.get("target_clv")) for item in clv_rows),
        "clv_trades": len(clv_rows),
        "beat_closing_line": len(beat),
        "missed_closing_line": len(miss),
        "tied_closing_line": tie,
    }


def _venue_results(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_venue: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        venue = str(trade.get("target_bookmaker") or "Unknown")
        by_venue.setdefault(venue, []).append(trade)

    rows = []
    for venue, venue_trades in by_venue.items():
        summary = _summary(venue_trades)
        rows.append(
            {
                "venue": venue,
                **summary,
            }
        )
    return sorted(
        rows,
        key=lambda item: (item["settled_profit"], item["total_trades"], item["venue"]),
        reverse=True,
    )


def _apply_filters(
    trades: list[dict[str, Any]],
    filters: dict[str, str],
) -> list[dict[str, Any]]:
    status = filters.get("status", "").casefold()
    bookmaker = filters.get("bookmaker", "").casefold()
    sport = filters.get("sport", "").casefold()
    output = trades
    if status:
        output = [item for item in output if str(item.get("status", "")).casefold() == status]
    if bookmaker:
        output = [
            item
            for item in output
            if bookmaker in str(item.get("target_bookmaker", "")).casefold()
        ]
    if sport:
        output = [item for item in output if sport in str(item.get("sport_key", "")).casefold()]
    return output


def _normalise_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: _jsonable(value) for key, value in item.items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _format_pct(value: object) -> str:
    if value in {None, ""}:
        return ""
    return f"{_float(value):.2%}"


def _format_number(value: object) -> str:
    if value in {None, ""}:
        return ""
    return f"{_float(value):.2f}"


def _short_time(value: object) -> str:
    text = str(value or "")
    return text.replace("+00:00", "Z").replace("T", " ")[:19]


def _class_for_number(value: float) -> str:
    if value > 0:
        return "good"
    if value < 0:
        return "bad"
    return "warn"


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _average(values) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _median(values) -> float:
    items = sorted(values)
    if not items:
        return 0.0
    midpoint = len(items) // 2
    if len(items) % 2:
        return items[midpoint]
    return (items[midpoint - 1] + items[midpoint]) / 2


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _url_escape(value: object) -> str:
    from urllib.parse import quote_plus

    return quote_plus(str(value))


def dashboard_json(payload: dict[str, Any]) -> str:
    return json.dumps(_jsonable(payload), indent=2)


def _scan_all(table: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {}
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return items
