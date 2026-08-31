from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from exchange_scanner.trading_control import is_control_item, trading_control_state


def dashboard_payload(
    table: Any,
    *,
    filters: dict[str, Any] | None = None,
    now: datetime | None = None,
    page: str = "paper",
    control_table: Any | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    filters = filters or {}
    page = _dashboard_page(page)
    control = trading_control_state(control_table or table)
    normalise = _normalise_live_order if page == "live" else _normalise_item
    trades = [normalise(item) for item in _scan_all(table) if not is_control_item(item)]
    filtered = _apply_filters(trades, filters, now=now)
    return {
        "page": page,
        "generated_at": now.isoformat(),
        "trading_control": _jsonable(control),
        "filters": {key: value for key, value in filters.items() if _filter_values(value)},
        "filter_options": _filter_options(trades),
        "summary": _summary(filtered, now=now),
        "all_summary": _summary(trades, now=now),
        "kelly": _kelly_curve(filtered, filters),
        "venue_results": _venue_results(filtered, now=now),
        "sport_results": _group_results(filtered, group_key="sport_family", label_key="sport", now=now),
        "league_results": _group_results(filtered, group_key="sport_key", label_key="league", now=now),
        "trades": sorted(
            filtered,
            key=lambda item: item.get("logged_at", ""),
            reverse=True,
        ),
    }


def render_dashboard_html(payload: dict[str, Any]) -> str:
    page = _dashboard_page(str(payload.get("page") or "paper"))
    summary = payload["summary"]
    all_summary = payload["all_summary"]
    rows = payload["trades"][:200]
    generated = _escape(payload["generated_at"])
    active_filters = payload.get("filters", {})
    filter_options = payload.get("filter_options", {})
    trading_control = payload.get("trading_control", {})
    token = str(payload.get("token", ""))
    filter_label = (
        ", ".join(f"{key}={', '.join(_filter_values(value))}" for key, value in active_filters.items())
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
    .page-tabs {{
      display: flex;
      gap: 8px;
      margin-top: 12px;
    }}
    .page-tabs a {{
      color: var(--text);
      text-decoration: none;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 7px 10px;
      font-size: 13px;
      background: var(--panel-2);
    }}
    .page-tabs a.active {{
      background: var(--text);
      color: var(--bg);
    }}
    .control {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin: 0 0 16px;
      padding: 12px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .advanced-filters {{
      margin: 0 0 16px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .advanced-filters summary {{
      cursor: pointer;
      padding: 10px 12px;
      color: var(--text);
      font-size: 13px;
      user-select: none;
    }}
    .advanced-filters summary::marker {{ color: var(--muted); }}
    .filter-panel {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 10px;
      padding: 0 12px 12px;
    }}
    .filter-group {{
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      align-content: start;
    }}
    .filter-group-title {{
      flex-basis: 100%;
      color: var(--muted);
      font-size: 12px;
    }}
    .check {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 6px 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
      color: var(--text);
      font-size: 12px;
    }}
    .check input {{ margin: 0; }}
    .filter-actions {{
      display: flex;
      gap: 8px;
      align-items: end;
    }}
    .range-filter {{
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 8px;
      align-items: center;
      width: 100%;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
    }}
    .range-filter input[type="range"] {{
      width: 100%;
      min-width: 120px;
    }}
    .range-value {{
      color: var(--text);
      font-variant-numeric: tabular-nums;
      font-size: 12px;
      min-width: 64px;
      text-align: right;
    }}
    .kelly-section {{
      margin: 0 0 16px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }}
    .kelly-head {{
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 10px;
    }}
    .kelly-form {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}
    .number-filter {{
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 8px;
      align-items: center;
      width: 100%;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
    }}
    .number-filter input[type="number"] {{
      min-width: 0;
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--bg);
      color: var(--text);
      padding: 6px 8px;
      font: inherit;
      font-variant-numeric: tabular-nums;
    }}
    .number-unit {{
      color: var(--muted);
      font-size: 12px;
    }}
    .kelly-chart {{
      width: 100%;
      height: auto;
      display: block;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #111820;
    }}
    .kelly-stats {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px 16px;
      color: var(--muted);
      font-size: 12px;
      margin-top: 8px;
    }}
    button {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--text);
      color: var(--bg);
      padding: 7px 10px;
      font: inherit;
      cursor: pointer;
    }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }}
    .venue-section {{ margin: 0 0 16px; }}
    .venue-table {{ min-width: 760px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 1160px; }}
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
    <h1>{_escape(_page_title(page))}</h1>
    <div class="meta">Generated {generated}<br>Active filters: {_escape(filter_label)}</div>
    {_page_tabs_html(token, page)}
  </header>
  <main>
    {_trading_control_html(trading_control)}
    {_metrics_html(summary, all_summary) if page == "paper" else _live_metrics_html(summary, all_summary)}
    {_quick_filters_html(token, page)}
    {_multi_filter_html(token, active_filters, filter_options, page=page)}
    {_kelly_html(payload.get("kelly", {}), token, active_filters) if page == "paper" else ""}
    {_venue_results_html(payload.get("venue_results", [])) if page == "paper" else _live_venue_results_html(payload.get("venue_results", []))}
    {_group_results_html("Results by Sport", payload.get("sport_results", []), "Sport")}
    {_group_results_html("Results by League", payload.get("league_results", []), "League")}
    <div class="table-wrap">
      <table>
        {_live_orders_table_html(rows) if page == "live" else _paper_trades_table_html(rows)}
      </table>
    </div>
  </main>
</body>
</html>
"""


def _dashboard_page(page: str) -> str:
    return "live" if str(page or "").casefold() == "live" else "paper"


def _page_title(page: str) -> str:
    return "Live Orders" if page == "live" else "Paper Trades"


def _page_tabs_html(token: str, page: str) -> str:
    return (
        '<nav class="page-tabs" aria-label="Dashboard pages">'
        f'<a class="{"active" if page == "paper" else ""}" href="{_href_attr(_filter_href(token, page="paper"))}">Paper</a>'
        f'<a class="{"active" if page == "live" else ""}" href="{_href_attr(_filter_href(token, page="live"))}">Live</a>'
        "</nav>"
    )


def _quick_filters_html(token: str, page: str) -> str:
    open_status = "submitted" if page == "live" else "open"
    settled_status = "matched" if page == "live" else "settled"
    open_label = "Submitted" if page == "live" else "Open"
    settled_label = "Matched" if page == "live" else "Settled"
    return f"""<nav class="filters">
      <a href="{_href_attr(_filter_href(token, page=page))}">All</a>
      <a href="{_href_attr(_filter_href(token, page=page, status=open_status))}">{_escape(open_label)}</a>
      <a href="{_href_attr(_filter_href(token, page=page, status=settled_status))}">{_escape(settled_label)}</a>
      <a href="{_href_attr(_filter_href(token, page=page, bookmaker='Matchbook'))}">Matchbook</a>
      <a href="{_href_attr(_filter_href(token, page=page, bookmaker='Smarkets'))}">Smarkets</a>
      <a href="{_href_attr(_filter_href(token, page=page, bookmaker='Betfair'))}">Betfair</a>
      <a href="{_href_attr(_filter_href(token, page=page, format='json'))}">JSON</a>
    </nav>"""


def _trading_control_html(control: dict[str, Any]) -> str:
    paused = bool(control.get("paused"))
    status = "Paused" if paused else "Live"
    tone = "bad" if paused else "good"
    updated_at = control.get("updated_at") or "not changed"
    return f"""<section class="control">
      <div>
        <div class="label">Trading</div>
        <div class="value {tone}">{_escape(status)}</div>
        <div class="meta">Last changed: {_escape(updated_at)}</div>
      </div>
    </section>"""


def _metrics_html(summary: dict[str, Any], all_summary: dict[str, Any]) -> str:
    return f"""<section class="grid">
      {_metric("Trades", summary["total_trades"], f"all {all_summary['total_trades']}")}
      {_metric("Open", summary["open_trades"])}
      {_metric("Settled", summary["settled_trades"])}
      {_metric("Trades Last 24h", summary["trades_last_24h"])}
      {_metric("Won/Lost", f"{summary['settled_won']}/{summary['settled_lost']}")}
      {_metric("PnL", f"{summary['settled_profit']:.2f}", _class_for_number(summary["settled_profit"]))}
      {_metric("ROI", f"{summary['settled_roi']:.2%}", _class_for_number(summary["settled_roi"]))}
      {_metric("Open Risk", f"{summary['open_liability']:.2f}")}
      {_metric("Avg Risk Odds", f"{summary['average_risk_odds']:.2f}")}
      {_metric("Median Liquidity", f"{summary['median_available_risk_at_target']:.2f}", "risk")}
      {_metric("Closed CLV", f"{summary['average_closed_clv']:.2%}", f"n={summary['closed_clv_trades']}", tone=_class_for_number(summary["average_closed_clv"]))}
      {_metric("Median Closed CLV", f"{summary['median_closed_clv']:.2%}", f"n={summary['closed_clv_trades']}", tone=_class_for_number(summary["median_closed_clv"]))}
      {_metric("Closed B/M/T", f"{summary['closed_clv_beats']}/{summary['closed_clv_misses']}/{summary['closed_clv_ties']}")}
      {_metric("Closed Fair Edge", f"{summary['average_closed_fair_edge']:.2%}", f"n={summary['closed_fair_edge_trades']}", tone=_class_for_number(summary["average_closed_fair_edge"]))}
      {_metric("Median Closed Fair", f"{summary['median_closed_fair_edge']:.2%}", f"n={summary['closed_fair_edge_trades']}", tone=_class_for_number(summary["median_closed_fair_edge"]))}
      {_metric("Closed Fair +", f"{summary['positive_closed_fair_edge_rate']:.2%}", f"{summary['positive_closed_fair_edge']}/{summary['closed_fair_edge_trades']}", tone=_class_for_number(summary["average_closed_fair_edge"]))}
      {_metric("MTM CLV", f"{summary['average_mark_to_market_clv']:.2%}", f"n={summary['mark_to_market_clv_trades']}", tone=_class_for_number(summary["average_mark_to_market_clv"]))}
      {_metric("Median MTM CLV", f"{summary['median_mark_to_market_clv']:.2%}", f"n={summary['mark_to_market_clv_trades']}", tone=_class_for_number(summary["median_mark_to_market_clv"]))}
      {_metric("MTM B/M/T", f"{summary['mark_to_market_clv_beats']}/{summary['mark_to_market_clv_misses']}/{summary['mark_to_market_clv_ties']}")}
      {_metric("MTM Fair Edge", f"{summary['average_mark_to_market_fair_edge']:.2%}", f"n={summary['mark_to_market_fair_edge_trades']}", tone=_class_for_number(summary["average_mark_to_market_fair_edge"]))}
      {_metric("Median MTM Fair", f"{summary['median_mark_to_market_fair_edge']:.2%}", f"n={summary['mark_to_market_fair_edge_trades']}", tone=_class_for_number(summary["median_mark_to_market_fair_edge"]))}
    </section>"""


def _live_metrics_html(summary: dict[str, Any], all_summary: dict[str, Any]) -> str:
    return f"""<section class="grid">
      {_metric("Orders", summary["total_trades"], f"all {all_summary['total_trades']}")}
      {_metric("Dry Run", summary["dry_run_orders"])}
      {_metric("Submitted", summary["submitted_orders"])}
      {_metric("Open", summary["live_open_orders"])}
      {_metric("Matched", summary["matched_orders"])}
      {_metric("Failed", summary["failed_orders"], tone="bad" if summary["failed_orders"] else "")}
      {_metric("Orders Last 24h", summary["trades_last_24h"])}
      {_metric("Total Risk", f"{summary['total_liability']:.2f}")}
      {_metric("Open Risk", f"{summary['live_open_liability']:.2f}")}
      {_metric("Matched Size", f"{summary['matched_size']:.2f}")}
      {_metric("Avg Limit Odds", f"{summary['average_booked_odds']:.2f}")}
      {_metric("Median Liquidity", f"{summary['median_available_risk_at_target']:.2f}", "risk")}
      {_metric("Entry EV", f"{summary['entry_expected_value']:.2f}", tone=_class_for_number(summary["entry_expected_value"]))}
      {_metric("Avg Edge", f"{summary['average_edge']:.2%}", tone=_class_for_number(summary["average_edge"]))}
    </section>"""


def _metric(label: str, value: object, extra: str = "", *, tone: str = "") -> str:
    class_name = tone or (extra if extra in {"good", "bad", "warn"} else "")
    subtitle = "" if extra in {"", "good", "bad", "warn"} else f'<div class="label">{_escape(extra)}</div>'
    return (
        '<div class="metric">'
        f'<div class="label">{_escape(label)}</div>'
        f'<div class="value {class_name}">{_escape(value)}</div>'
        f"{subtitle}</div>"
    )


def _paper_trades_table_html(rows: list[dict[str, Any]]) -> str:
    return f"""<thead>
          <tr>
            <th>Logged</th>
            <th>Status</th>
            <th>Book</th>
            <th>Event</th>
            <th>Bet</th>
            <th>Raw Odds</th>
            <th>Risk Odds</th>
            <th>Risk</th>
            <th>Liquidity</th>
            <th>Edge</th>
            <th>CLV</th>
            <th>Closing Fair Edge</th>
            <th>Ref Disagree</th>
            <th>Ref Spread</th>
            <th>Venue Fair Edge</th>
            <th>Venue Spread</th>
            <th>Profit</th>
            <th>Starts</th>
          </tr>
        </thead>
        <tbody>
          {_trade_rows_html(rows)}
        </tbody>"""


def _trade_rows_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="18">No trades match the current filters.</td></tr>'
    return "\n".join(
        "<tr>"
        f"<td>{_short_time(row.get('logged_at'))}</td>"
        f"<td>{_escape(row.get('status', ''))}</td>"
        f"<td>{_escape(row.get('target_bookmaker', ''))}</td>"
        f"<td>{_escape(row.get('event_name', ''))}</td>"
        f"<td>{_escape(row.get('risk_selection', ''))}</td>"
        f"<td>{_format_number(row.get('target_odds'))}</td>"
        f"<td>{_format_number(row.get('risk_odds'))}</td>"
        f"<td>{_format_number(row.get('liability'))}</td>"
        f"<td>{_format_number(row.get('available_risk_at_target'))}</td>"
        f"<td>{_format_pct(row.get('edge'))}</td>"
        f"<td>{_format_pct(row.get('target_clv'))}</td>"
        f"<td>{_format_pct(row.get('closing_edge'))}</td>"
        f"<td>{_format_pct(row.get('reference_disagreement_pct'))}</td>"
        f"<td>{_format_pct(row.get('reference_max_spread_pct'))}</td>"
        f"<td>{_format_pct(row.get('betfair_fair_edge'))}</td>"
        f"<td>{_format_pct(row.get('betfair_back_lay_spread_pct'))}</td>"
        f"<td>{_format_number(row.get('profit'))}</td>"
        f"<td>{_short_time(row.get('commence_time'))}</td>"
        "</tr>"
        for row in rows
    )


def _live_orders_table_html(rows: list[dict[str, Any]]) -> str:
    return f"""<thead>
          <tr>
            <th>Logged</th>
            <th>Status</th>
            <th>Mode</th>
            <th>Book</th>
            <th>Event</th>
            <th>Bet</th>
            <th>Limit Odds</th>
            <th>Stake</th>
            <th>Liability</th>
            <th>Sizing</th>
            <th>Edge</th>
            <th>Ref Disagree</th>
            <th>Available</th>
            <th>Venue Order</th>
            <th>Matched</th>
            <th>Avg Matched</th>
            <th>Error</th>
            <th>Starts</th>
          </tr>
        </thead>
        <tbody>
          {_live_order_rows_html(rows)}
        </tbody>"""


def _live_order_rows_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="18">No live orders match the current filters.</td></tr>'
    return "\n".join(
        "<tr>"
        f"<td>{_short_time(row.get('logged_at'))}</td>"
        f"<td>{_escape(row.get('status', ''))}</td>"
        f"<td>{_escape(row.get('execution_mode', ''))}</td>"
        f"<td>{_escape(row.get('target_bookmaker', ''))}</td>"
        f"<td>{_escape(row.get('event_name', ''))}</td>"
        f"<td>{_escape(row.get('risk_selection', ''))}</td>"
        f"<td>{_format_number(row.get('target_odds'))}</td>"
        f"<td>{_format_number(row.get('stake'))}</td>"
        f"<td>{_format_number(row.get('liability'))}</td>"
        f"<td>{_escape(row.get('sizing_method', ''))}</td>"
        f"<td>{_format_pct(row.get('edge'))}</td>"
        f"<td>{_format_pct(row.get('reference_disagreement_pct'))}</td>"
        f"<td>{_format_number(row.get('available_risk_at_target'))}</td>"
        f"<td>{_escape(row.get('venue_order_id', ''))}</td>"
        f"<td>{_format_number(row.get('matched_size'))}</td>"
        f"<td>{_format_number(row.get('avg_matched_odds'))}</td>"
        f"<td>{_escape(row.get('error', ''))}</td>"
        f"<td>{_short_time(row.get('commence_time'))}</td>"
        "</tr>"
        for row in rows
    )


def _bet_side(row: dict[str, Any]) -> str:
    side = str(row.get("bet_side") or "back").casefold()
    return "lay" if side == "lay" else "back"


def _venue_results_html(venues: list[dict[str, Any]]) -> str:
    rows = _group_rows_html(venues, label_key="venue")
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
              <th>Avg Closed CLV</th>
              <th>Median Closed CLV</th>
              <th>Avg Closed Fair</th>
              <th>Median Closed Fair</th>
            </tr>
          </thead>
          <tbody>
            {rows}
          </tbody>
        </table>
      </div>
    </section>"""


def _live_venue_results_html(venues: list[dict[str, Any]]) -> str:
    rows = _live_group_rows_html(venues, label_key="venue")
    return f"""<section class="venue-section">
      <h2>Orders by Venue</h2>
      <div class="table-wrap venue-wrap">
        <table class="venue-table">
          <thead>
            <tr>
              <th>Venue</th>
              <th>Orders</th>
              <th>Dry Run</th>
              <th>Submitted</th>
              <th>Open</th>
              <th>Matched</th>
              <th>Failed</th>
              <th>Total Risk</th>
              <th>Matched Size</th>
              <th>Avg Edge</th>
              <th>Median Liquidity</th>
            </tr>
          </thead>
          <tbody>
            {rows}
          </tbody>
        </table>
      </div>
    </section>"""


def _live_group_rows_html(rows: list[dict[str, Any]], *, label_key: str) -> str:
    if not rows:
        return '<tr><td colspan="11">No live orders for the current filters.</td></tr>'
    return "\n".join(
        "<tr>"
        f"<td>{_escape(row[label_key])}</td>"
        f"<td>{_escape(row['total_trades'])}</td>"
        f"<td>{_escape(row['dry_run_orders'])}</td>"
        f"<td>{_escape(row['submitted_orders'])}</td>"
        f"<td>{_escape(row['live_open_orders'])}</td>"
        f"<td>{_escape(row['matched_orders'])}</td>"
        f"<td class='bad'>{_escape(row['failed_orders'])}</td>"
        f"<td>{row['total_liability']:.2f}</td>"
        f"<td>{row['matched_size']:.2f}</td>"
        f"<td class='{_class_for_number(row['average_edge'])}'>{row['average_edge']:.2%}</td>"
        f"<td>{row['median_available_risk_at_target']:.2f}</td>"
        "</tr>"
        for row in rows
    )


def _group_results_html(title: str, rows: list[dict[str, Any]], label: str) -> str:
    return f"""<section class="venue-section">
      <h2>{_escape(title)}</h2>
      <div class="table-wrap venue-wrap">
        <table class="venue-table">
          <thead>
            <tr>
              <th>{_escape(label)}</th>
              <th>Trades</th>
              <th>Open</th>
              <th>Settled</th>
              <th>Won/Lost</th>
              <th>PnL</th>
              <th>ROI</th>
              <th>Median Liquidity</th>
              <th>Avg Closed CLV</th>
              <th>Median Closed CLV</th>
              <th>Avg Closed Fair</th>
              <th>Median Closed Fair</th>
            </tr>
          </thead>
          <tbody>
            {_group_rows_html(rows, label_key=label.casefold())}
          </tbody>
        </table>
      </div>
    </section>"""


def _group_rows_html(rows: list[dict[str, Any]], *, label_key: str) -> str:
    if not rows:
        return '<tr><td colspan="12">No results for the current filters.</td></tr>'
    return "\n".join(
        "<tr>"
        f"<td>{_escape(row[label_key])}</td>"
        f"<td>{_escape(row['total_trades'])}</td>"
        f"<td>{_escape(row['open_trades'])}</td>"
        f"<td>{_escape(row['settled_trades'])}</td>"
        f"<td>{_escape(row['settled_won'])}/{_escape(row['settled_lost'])}</td>"
        f"<td class='{_class_for_number(row['settled_profit'])}'>{row['settled_profit']:.2f}</td>"
        f"<td class='{_class_for_number(row['settled_roi'])}'>{row['settled_roi']:.2%}</td>"
        f"<td>{row['median_available_risk_at_target']:.2f}</td>"
        f"<td class='{_class_for_number(row['average_clv'])}'>{row['average_clv']:.2%}</td>"
        f"<td class='{_class_for_number(row['median_closed_clv'])}'>{row['median_closed_clv']:.2%}</td>"
        f"<td class='{_class_for_number(row['average_closed_fair_edge'])}'>{row['average_closed_fair_edge']:.2%}</td>"
        f"<td class='{_class_for_number(row['median_closed_fair_edge'])}'>{row['median_closed_fair_edge']:.2%}</td>"
        "</tr>"
        for row in rows
    )


def _multi_filter_html(
    token: str,
    active_filters: dict[str, Any],
    filter_options: dict[str, list[dict[str, str]]],
    *,
    page: str,
) -> str:
    sport_values = set(_filter_values(active_filters.get("sport")))
    league_values = set(_filter_values(active_filters.get("league")))
    status = _first_filter_value(active_filters.get("status"))
    bookmaker = _first_filter_value(active_filters.get("bookmaker"))
    format_value = _first_filter_value(active_filters.get("format"))
    clv_value = _first_filter_value(active_filters.get("clv"))
    max_reference_disagreement = _first_filter_value(
        active_filters.get("max_reference_disagreement_pct")
    )
    max_reference_spread = _first_filter_value(active_filters.get("max_reference_spread_pct"))
    min_liquidity = _first_filter_value(active_filters.get("min_liquidity"))
    kelly_bankroll = _first_filter_value(active_filters.get("kelly_bankroll"))
    kelly_edge = _kelly_percent_filter_value(active_filters, "kelly_edge")
    kelly_fraction = _kelly_percent_filter_value(active_filters, "kelly_fraction")
    kelly_sizing = _kelly_percent_filter_value(active_filters, "kelly_sizing")
    return f"""<details class="advanced-filters">
      <summary>Advanced Filters</summary>
      <form class="filter-panel" method="get">
        <input type="hidden" name="token" value="{_escape(token)}">
        <input type="hidden" name="page" value="{_escape(page)}">
        {_hidden_input("status", status)}
        {_hidden_input("bookmaker", bookmaker)}
        {_hidden_input("format", format_value)}
        {_hidden_input("kelly_bankroll", kelly_bankroll)}
        {_hidden_input("kelly_edge_pct", kelly_edge)}
        {_hidden_input("kelly_fraction_pct", kelly_fraction)}
        {_hidden_input("kelly_sizing_pct", kelly_sizing)}
        <div class="filter-group">
          <div class="filter-group-title">CLV</div>
          {_radio("clv", "", "All", clv_value)}
          {_radio("clv", "closed", "Closed market", clv_value)}
          {_radio("clv", "mtm", "Mark to market", clv_value)}
          {_radio("clv", "missing", "Missing", clv_value)}
        </div>
        <div class="filter-group">
          <div class="filter-group-title">Reference Quality</div>
          {_range_filter(
              name="max_reference_disagreement_pct",
              label="Max disagreement",
              value=max_reference_disagreement,
              default=0.03,
              max_value=1.0,
              step=0.005,
              scale=100,
              suffix="%",
          )}
          {_range_filter(
              name="max_reference_spread_pct",
              label="Max spread",
              value=max_reference_spread,
              default=0.15,
              max_value=0.5,
              step=0.005,
              scale=100,
              suffix="%",
          )}
        </div>
        <div class="filter-group">
          <div class="filter-group-title">Execution</div>
          {_range_filter(
              name="min_liquidity",
              label="Min liquidity",
              value=min_liquidity,
              default=25,
              max_value=500,
              step=5,
              scale=1,
              prefix="GBP ",
              suffix="",
          )}
        </div>
        <div class="filter-group">
          <div class="filter-group-title">Sports</div>
          {_checkboxes("sport", filter_options.get("sports", []), sport_values)}
        </div>
        <div class="filter-group">
          <div class="filter-group-title">Leagues</div>
          {_checkboxes("league", filter_options.get("leagues", []), league_values)}
        </div>
        <div class="filter-actions">
          <button type="submit">Apply</button>
          <a href="{_href_attr(_filter_href(token, page=page))}">Clear</a>
        </div>
      </form>
    </details>"""


def _kelly_html(kelly: dict[str, Any], token: str, active_filters: dict[str, Any]) -> str:
    params = kelly.get("params", {})
    hidden = "\n".join(
        _hidden_inputs(name, active_filters.get(name))
        for name in (
            "status",
            "bookmaker",
            "format",
            "clv",
            "max_reference_disagreement_pct",
            "max_reference_spread_pct",
            "min_liquidity",
            "sport",
            "league",
        )
    )
    return f"""<section class="kelly-section">
      <div class="kelly-head">
        <h2>Kelly Equity Curve</h2>
        <div class="meta">Settled trades only, capped by sizing and recorded liquidity.</div>
      </div>
      <form class="kelly-form" method="get">
        <input type="hidden" name="token" value="{_escape(token)}">
        {hidden}
        {_number_filter(
            name="kelly_bankroll",
            label="Bankroll",
            value=params.get("bankroll", 1000),
            default=1000,
            min_value=1,
            max_value=1000000,
            step=100,
            unit="GBP",
        )}
        {_number_filter(
            name="kelly_edge_pct",
            label="Edge",
            value=params.get("edge", 0.01),
            default=1,
            min_value=0,
            max_value=10,
            step=0.1,
            scale=100,
            unit="%",
        )}
        {_number_filter(
            name="kelly_fraction_pct",
            label="Kelly fraction",
            value=params.get("fraction", 0.25),
            default=25,
            min_value=0,
            max_value=100,
            step=1,
            scale=100,
            unit="%",
        )}
        {_number_filter(
            name="kelly_sizing_pct",
            label="Sizing",
            value=params.get("sizing", 0.05),
            default=5,
            min_value=0,
            max_value=25,
            step=0.5,
            scale=100,
            unit="%",
        )}
        <div class="filter-actions">
          <button type="submit">Update</button>
        </div>
      </form>
      {_kelly_svg(kelly)}
      <div class="kelly-stats">
        <span>Trades: {_escape(kelly.get("trades", 0))}</span>
        <span>Final: {_format_money(kelly.get("final_bankroll"))}</span>
        <span>Return: {_format_pct(kelly.get("return_pct"))}</span>
        <span>Max drawdown: {_format_pct(kelly.get("max_drawdown_pct"))}</span>
        <span>Edge: {_format_pct(params.get("edge"))}</span>
        <span>Avg risk: {_format_pct(kelly.get("average_risk_pct"))}</span>
      </div>
    </section>"""


def _kelly_curve(trades: list[dict[str, Any]], filters: dict[str, Any]) -> dict[str, Any]:
    bankroll = _bounded_filter_float(
        filters.get("kelly_bankroll"), default=1000.0, min_value=1.0, max_value=1000000.0
    )
    edge = _bounded_percent_filter_float(
        filters.get("kelly_edge_pct"),
        legacy_value=filters.get("kelly_edge"),
        default=0.01,
        min_value=0.0,
        max_value=0.10,
    )
    fraction = _bounded_percent_filter_float(
        filters.get("kelly_fraction_pct"),
        legacy_value=filters.get("kelly_fraction"),
        default=0.25,
        min_value=0.0,
        max_value=1.0,
    )
    sizing = _bounded_percent_filter_float(
        filters.get("kelly_sizing_pct"),
        legacy_value=filters.get("kelly_sizing") or filters.get("kelly_max_risk_pct"),
        default=0.05,
        min_value=0.0,
        max_value=0.25,
    )
    equity = bankroll
    peak = bankroll
    max_drawdown = 0.0
    total_risk_pct = 0.0
    points = [{"index": 0, "equity": equity}]
    settled = sorted(
        (item for item in trades if str(item.get("status", "")).casefold() == "settled"),
        key=lambda item: str(item.get("logged_at") or ""),
    )
    for index, item in enumerate(settled, start=1):
        risk_odds = _float(item.get("risk_odds"))
        if risk_odds <= 1 or edge <= 0 or fraction <= 0 or sizing <= 0:
            risk_pct = 0.0
        else:
            full_kelly = edge / (risk_odds - 1)
            risk_pct = min(sizing, max(0.0, full_kelly * fraction))
        risk_amount = equity * risk_pct
        available_risk = _float(item.get("available_risk_at_target"))
        if available_risk > 0:
            risk_amount = min(risk_amount, available_risk)
        starting_equity = equity
        liability = _trade_liability(item)
        profit_per_risk = _float(item.get("profit")) / liability if liability > 0 else 0.0
        equity += risk_amount * profit_per_risk
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
        total_risk_pct += risk_amount / starting_equity if starting_equity > 0 else 0.0
        points.append({"index": index, "equity": equity})
    return {
        "params": {
            "bankroll": bankroll,
            "edge": edge,
            "fraction": fraction,
            "sizing": sizing,
        },
        "trades": len(settled),
        "points": points,
        "final_bankroll": equity,
        "return_pct": (equity / bankroll - 1) if bankroll > 0 else 0.0,
        "max_drawdown_pct": max_drawdown,
        "average_risk_pct": total_risk_pct / len(settled) if settled else 0.0,
    }


def _kelly_svg(kelly: dict[str, Any]) -> str:
    points = list(kelly.get("points") or [])
    if len(points) < 2:
        return (
            '<svg class="kelly-chart" viewBox="0 0 720 220" role="img" '
            'aria-label="Kelly equity curve">'
            '<text x="360" y="112" text-anchor="middle" fill="#9aa8b5" font-size="13">'
            "No settled trades for the current filters."
            "</text></svg>"
        )
    width = 720
    height = 220
    pad_left = 48
    pad_right = 16
    pad_top = 16
    pad_bottom = 34
    equities = [_float(point.get("equity")) for point in points]
    min_equity = min(equities)
    max_equity = max(equities)
    if min_equity == max_equity:
        min_equity *= 0.98
        max_equity *= 1.02
    usable_width = width - pad_left - pad_right
    usable_height = height - pad_top - pad_bottom

    def xy(index: int, equity: float) -> tuple[float, float]:
        x = pad_left + (index / max(1, len(points) - 1)) * usable_width
        y = pad_top + (max_equity - equity) / (max_equity - min_equity) * usable_height
        return x, y

    path = []
    for point_index, point in enumerate(points):
        x, y = xy(point_index, _float(point.get("equity")))
        path.append(("M" if point_index == 0 else "L") + f"{x:.1f},{y:.1f}")
    zero_y = xy(0, _float(kelly.get("params", {}).get("bankroll", 0)))[1]
    final_class = _class_for_number(_float(kelly.get("return_pct")))
    stroke = "#3ecf8e" if final_class == "good" else "#ff6b6b" if final_class == "bad" else "#ffd166"
    return f"""<svg class="kelly-chart" viewBox="0 0 {width} {height}" role="img" aria-label="Kelly equity curve">
      <line x1="{pad_left}" y1="{zero_y:.1f}" x2="{width - pad_right}" y2="{zero_y:.1f}" stroke="#2d3742" stroke-dasharray="4 4"/>
      <line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{height - pad_bottom}" stroke="#2d3742"/>
      <line x1="{pad_left}" y1="{height - pad_bottom}" x2="{width - pad_right}" y2="{height - pad_bottom}" stroke="#2d3742"/>
      <path d="{' '.join(path)}" fill="none" stroke="{stroke}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>
      <text x="{pad_left}" y="{height - 10}" fill="#9aa8b5" font-size="11">0</text>
      <text x="{width - pad_right}" y="{height - 10}" text-anchor="end" fill="#9aa8b5" font-size="11">{len(points) - 1} trades</text>
      <text x="{pad_left - 8}" y="{pad_top + 4}" text-anchor="end" fill="#9aa8b5" font-size="11">{_format_money(max_equity)}</text>
      <text x="{pad_left - 8}" y="{height - pad_bottom}" text-anchor="end" fill="#9aa8b5" font-size="11">{_format_money(min_equity)}</text>
    </svg>"""


def _checkboxes(
    name: str,
    options: list[dict[str, str]],
    selected_values: set[str],
) -> str:
    if not options:
        return '<span class="label">None</span>'
    return "\n".join(
        '<label class="check">'
        f'<input type="checkbox" name="{_escape(name)}" value="{_escape(option["value"])}"'
        f"{' checked' if option['value'] in selected_values else ''}>"
        f"{_escape(option['label'])}"
        "</label>"
        for option in options
    )


def _radio(name: str, value: str, label: str, selected_value: str) -> str:
    return (
        '<label class="check">'
        f'<input type="radio" name="{_escape(name)}" value="{_escape(value)}"'
        f"{' checked' if value == selected_value else ''}>"
        f"{_escape(label)}"
        "</label>"
    )


def _range_filter(
    *,
    name: str,
    label: str,
    value: str,
    default: float,
    max_value: float,
    step: float,
    scale: float = 100,
    prefix: str = "",
    suffix: str = "%",
) -> str:
    enabled = value != ""
    slider_value = _bounded_float(value, default=default, min_value=0.0, max_value=max_value)
    input_id = f"filter-{name}"
    output_id = f"{input_id}-value"
    disabled = "" if enabled else " disabled"
    checked = " checked" if enabled else ""
    checkbox_formatter = _range_value_formatter("r.value", scale=scale, prefix=prefix, suffix=suffix)
    input_formatter = _range_value_formatter("this.value", scale=scale, prefix=prefix, suffix=suffix)
    return (
        '<label class="range-filter">'
        f'<input type="checkbox"{checked} '
        f'onchange="const r=document.getElementById(\'{input_id}\');'
        f"r.disabled=!this.checked;document.getElementById('{output_id}').textContent=this.checked?{checkbox_formatter}:'off';\">"
        f'<span>{_escape(label)}</span>'
        f'<input id="{_escape(input_id)}" type="range" name="{_escape(name)}" '
        f'min="0" max="{max_value:g}" step="{step:g}" value="{slider_value:g}"{disabled} '
        f"oninput=\"document.getElementById('{output_id}').textContent={input_formatter};\">"
        f'<span id="{_escape(output_id)}" class="range-value">'
        f"{_escape(_format_range_value(slider_value, scale=scale, prefix=prefix, suffix=suffix) if enabled else 'off')}</span>"
        "</label>"
    )


def _number_filter(
    *,
    name: str,
    label: str,
    value: object,
    default: float,
    min_value: float,
    max_value: float,
    step: float,
    scale: float = 1,
    unit: str = "",
) -> str:
    numeric_value = _float(value) * scale if value not in {None, ""} else default
    numeric_value = min(max(numeric_value, min_value), max_value)
    return (
        '<label class="number-filter">'
        f"<span>{_escape(label)}</span>"
        f'<input type="number" name="{_escape(name)}" min="{min_value:g}" '
        f'max="{max_value:g}" step="{step:g}" value="{numeric_value:g}">'
        f'<span class="number-unit">{_escape(unit)}</span>'
        "</label>"
    )


def _range_value_formatter(value_expression: str, *, scale: float, prefix: str, suffix: str) -> str:
    decimals = 1 if scale == 100 else 0
    return (
        f"'{_escape(prefix)}'+(Number({value_expression})*{scale:g}).toFixed({decimals})+'{_escape(suffix)}'"
    )


def _format_range_value(value: float, *, scale: float, prefix: str, suffix: str) -> str:
    decimals = 1 if scale == 100 else 0
    return f"{prefix}{value * scale:.{decimals}f}{suffix}"


def _hidden_input(name: str, value: str) -> str:
    if not value:
        return ""
    return f'<input type="hidden" name="{_escape(name)}" value="{_escape(value)}">'


def _hidden_inputs(name: str, value: Any) -> str:
    return "\n".join(_hidden_input(name, item) for item in _filter_values(value))


def _filter_href(token: str, **params: str) -> str:
    page = params.pop("page", "paper")
    pairs = [("token", token), *params.items()]
    if page == "live":
        pairs.insert(1, ("page", page))
    query = "&".join(
        f"{_url_escape(key)}={_url_escape(value)}"
        for key, value in pairs
        if value
    )
    return f"?{query}" if query else "?"


def _href_attr(value: str) -> str:
    return html.escape(value, quote=True)


def _summary(trades: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    settled = [item for item in trades if item.get("status") == "settled"]
    open_trades = [item for item in trades if item.get("status") == "open"]
    clv_rows = [item for item in trades if _has_clv(item)]
    closed_clv_rows = [item for item in clv_rows if _market_has_closed(item, now=now)]
    mark_to_market_clv_rows = clv_rows
    fair_edge_rows = [item for item in trades if _has_closing_edge(item)]
    closed_fair_edge_rows = [
        item for item in fair_edge_rows if _market_has_closed(item, now=now)
    ]
    mark_to_market_fair_edge_rows = fair_edge_rows
    staked = sum(_float(item.get("stake")) for item in settled)
    settled_liability = sum(_trade_liability(item) for item in settled)
    open_liability = sum(_trade_liability(item) for item in open_trades)
    total_liability = sum(_trade_liability(item) for item in trades)
    entry_expected_value = sum(_trade_expected_value(item) for item in trades)
    profit = sum(_float(item.get("profit")) for item in settled)
    wins = sum(1 for item in settled if _float(item.get("profit")) > 0)
    losses = len(settled) - wins
    closed_counts = _clv_counts(closed_clv_rows)
    mark_to_market_counts = _clv_counts(mark_to_market_clv_rows)
    trades_last_24h = sum(1 for item in trades if _is_logged_in_last_24h(item, now=now))
    dry_run_orders = sum(1 for item in trades if str(item.get("status", "")).casefold() == "dry_run")
    submitted_orders = sum(
        1 for item in trades if str(item.get("status", "")).casefold() == "submitted"
    )
    matched_orders = sum(
        1 for item in trades if str(item.get("status", "")).casefold() == "matched"
    )
    failed_orders = sum(1 for item in trades if str(item.get("status", "")).casefold() == "failed")
    live_open_orders = sum(
        1
        for item in trades
        if str(item.get("status", "")).casefold()
        in {"dry_run", "submitted", "open", "partially_matched"}
    )
    live_open_liability = sum(
        _trade_liability(item)
        for item in trades
        if str(item.get("status", "")).casefold()
        in {"dry_run", "submitted", "open", "partially_matched"}
    )
    matched_size = sum(_float(item.get("matched_size")) for item in trades)
    average_closed_clv = _average(_float(item.get("target_clv")) for item in closed_clv_rows)
    median_closed_clv = _median(_float(item.get("target_clv")) for item in closed_clv_rows)
    average_mark_to_market_clv = _average(
        _float(item.get("target_clv")) for item in mark_to_market_clv_rows
    )
    median_mark_to_market_clv = _median(
        _float(item.get("target_clv")) for item in mark_to_market_clv_rows
    )
    average_closed_fair_edge = _average(
        _float(item.get("closing_edge")) for item in closed_fair_edge_rows
    )
    median_closed_fair_edge = _median(
        _float(item.get("closing_edge")) for item in closed_fair_edge_rows
    )
    average_mark_to_market_fair_edge = _average(
        _float(item.get("closing_edge")) for item in mark_to_market_fair_edge_rows
    )
    median_mark_to_market_fair_edge = _median(
        _float(item.get("closing_edge")) for item in mark_to_market_fair_edge_rows
    )
    positive_closed_fair_edge = sum(
        1 for item in closed_fair_edge_rows if _float(item.get("closing_edge")) > 0
    )
    positive_mark_to_market_fair_edge = sum(
        1 for item in mark_to_market_fair_edge_rows if _float(item.get("closing_edge")) > 0
    )
    return {
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "settled_trades": len(settled),
        "trades_last_24h": trades_last_24h,
        "dry_run_orders": dry_run_orders,
        "submitted_orders": submitted_orders,
        "matched_orders": matched_orders,
        "failed_orders": failed_orders,
        "live_open_orders": live_open_orders,
        "live_open_liability": live_open_liability,
        "matched_size": matched_size,
        "settled_won": wins,
        "settled_lost": losses,
        "settled_profit": profit,
        "settled_roi": profit / staked if staked else 0.0,
        "settled_liability": settled_liability,
        "open_liability": open_liability,
        "total_liability": total_liability,
        "settled_risk_roi": profit / settled_liability if settled_liability else 0.0,
        "entry_expected_value": entry_expected_value,
        "average_booked_odds": _average(_float(item.get("target_odds")) for item in trades),
        "average_edge": _average(_float(item.get("edge")) for item in trades),
        "average_risk_odds": _average(_float(item.get("risk_odds")) for item in trades),
        "median_confirmed_liquidity_at_target": _median(
            _float(item.get("available_at_or_above_target"))
            for item in trades
            if _has_applicable_liquidity(item)
            and item.get("available_at_or_above_target") not in {None, ""}
        ),
        "median_available_risk_at_target": _median(
            _float(item.get("available_risk_at_target"))
            for item in trades
            if _has_applicable_liquidity(item)
            and item.get("available_risk_at_target") not in {None, ""}
        ),
        "average_clv": average_closed_clv,
        "clv_trades": len(closed_clv_rows),
        "beat_closing_line": closed_counts["beats"],
        "missed_closing_line": closed_counts["misses"],
        "tied_closing_line": closed_counts["ties"],
        "average_closed_clv": average_closed_clv,
        "median_closed_clv": median_closed_clv,
        "closed_clv_trades": len(closed_clv_rows),
        "closed_clv_beats": closed_counts["beats"],
        "closed_clv_misses": closed_counts["misses"],
        "closed_clv_ties": closed_counts["ties"],
        "average_mark_to_market_clv": average_mark_to_market_clv,
        "median_mark_to_market_clv": median_mark_to_market_clv,
        "mark_to_market_clv_trades": len(mark_to_market_clv_rows),
        "mark_to_market_clv_beats": mark_to_market_counts["beats"],
        "mark_to_market_clv_misses": mark_to_market_counts["misses"],
        "mark_to_market_clv_ties": mark_to_market_counts["ties"],
        "average_closed_fair_edge": average_closed_fair_edge,
        "median_closed_fair_edge": median_closed_fair_edge,
        "closed_fair_edge_trades": len(closed_fair_edge_rows),
        "positive_closed_fair_edge": positive_closed_fair_edge,
        "positive_closed_fair_edge_rate": (
            positive_closed_fair_edge / len(closed_fair_edge_rows)
            if closed_fair_edge_rows
            else 0.0
        ),
        "average_mark_to_market_fair_edge": average_mark_to_market_fair_edge,
        "median_mark_to_market_fair_edge": median_mark_to_market_fair_edge,
        "mark_to_market_fair_edge_trades": len(mark_to_market_fair_edge_rows),
        "positive_mark_to_market_fair_edge": positive_mark_to_market_fair_edge,
        "positive_mark_to_market_fair_edge_rate": (
            positive_mark_to_market_fair_edge / len(mark_to_market_fair_edge_rows)
            if mark_to_market_fair_edge_rows
            else 0.0
        ),
    }


def _has_clv(item: dict[str, Any]) -> bool:
    return item.get("target_clv") not in {None, ""}


def _has_closing_edge(item: dict[str, Any]) -> bool:
    return item.get("closing_edge") not in {None, ""}


def _market_has_closed(item: dict[str, Any], *, now: datetime) -> bool:
    if str(item.get("status", "")).casefold() == "settled":
        return True
    commence_time = _parse_datetime(item.get("commence_time"))
    return commence_time is not None and commence_time <= _as_utc(now)


def _clv_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    beats = sum(1 for item in rows if _float(item.get("target_clv")) > 0)
    misses = sum(1 for item in rows if _float(item.get("target_clv")) < 0)
    return {
        "beats": beats,
        "misses": misses,
        "ties": len(rows) - beats - misses,
    }


def _venue_results(trades: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    return _group_results(trades, group_key="target_bookmaker", label_key="venue", now=now)


def _group_results(
    trades: list[dict[str, Any]],
    *,
    group_key: str,
    label_key: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        label = _group_label(trade, group_key=group_key)
        grouped.setdefault(label, []).append(trade)

    rows = []
    for label, grouped_trades in grouped.items():
        summary = _summary(grouped_trades, now=now)
        rows.append(
            {
                label_key: label,
                **summary,
            }
        )
    return sorted(
        rows,
        key=lambda item: (item["settled_profit"], item["total_trades"], item[label_key]),
        reverse=True,
    )


def _group_label(trade: dict[str, Any], *, group_key: str) -> str:
    if group_key == "sport_family":
        return _pretty_label(str(trade.get("sport_family") or "unknown"))
    if group_key == "sport_key":
        return _pretty_label(str(trade.get("sport_key") or "unknown"))
    return str(trade.get(group_key) or "Unknown")


def _filter_options(trades: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    sports = sorted({str(item.get("sport_family") or "") for item in trades if item.get("sport_family")})
    leagues = sorted({str(item.get("sport_key") or "") for item in trades if item.get("sport_key")})
    return {
        "sports": [{"value": value, "label": _pretty_label(value)} for value in sports],
        "leagues": [{"value": value, "label": _pretty_label(value)} for value in leagues],
    }


def _apply_filters(
    trades: list[dict[str, Any]],
    filters: dict[str, Any],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    status = _first_filter_value(filters.get("status")).casefold()
    bookmaker = _first_filter_value(filters.get("bookmaker")).casefold()
    clv = _first_filter_value(filters.get("clv")).casefold()
    max_reference_disagreement = _optional_filter_float(
        filters.get("max_reference_disagreement_pct")
    )
    max_reference_spread = _optional_filter_float(filters.get("max_reference_spread_pct"))
    min_liquidity = _optional_filter_float(filters.get("min_liquidity"))
    sports = {value.casefold() for value in _filter_values(filters.get("sport"))}
    leagues = {value.casefold() for value in _filter_values(filters.get("league"))}
    output = trades
    if status:
        output = [item for item in output if str(item.get("status", "")).casefold() == status]
    if bookmaker:
        output = [
            item
            for item in output
            if bookmaker in str(item.get("target_bookmaker", "")).casefold()
        ]
    if sports:
        output = [item for item in output if str(item.get("sport_family", "")).casefold() in sports]
    if leagues:
        output = [item for item in output if str(item.get("sport_key", "")).casefold() in leagues]
    if clv:
        output = [item for item in output if _matches_clv_filter(item, clv=clv, now=now)]
    if max_reference_disagreement is not None:
        output = [
            item
            for item in output
            if _has_number_at_or_below(item, "reference_disagreement_pct", max_reference_disagreement)
        ]
    if max_reference_spread is not None:
        output = [
            item
            for item in output
            if _has_number_at_or_below(item, "reference_max_spread_pct", max_reference_spread)
        ]
    if min_liquidity is not None:
        output = [
            item
            for item in output
            if _has_applicable_liquidity(item)
            and _has_number_at_or_above(item, "available_risk_at_target", min_liquidity)
        ]
    return output


def _matches_clv_filter(item: dict[str, Any], *, clv: str, now: datetime) -> bool:
    has_clv = _has_clv(item)
    if clv == "closed":
        return has_clv and _market_has_closed(item, now=now)
    if clv == "mtm":
        return has_clv
    if clv == "missing":
        return not has_clv
    return True


def _has_number_at_or_below(item: dict[str, Any], field: str, maximum: float) -> bool:
    value = item.get(field)
    if value in {None, ""}:
        return False
    return _float(value) <= maximum


def _has_number_at_or_above(item: dict[str, Any], field: str, minimum: float) -> bool:
    value = item.get(field)
    if value in {None, ""}:
        return False
    return _float(value) >= minimum


def _normalise_item(item: dict[str, Any]) -> dict[str, Any]:
    normalised = {key: _jsonable(value) for key, value in item.items()}
    sport_key = str(normalised.get("sport_key") or "")
    normalised["sport_family"] = _sport_family(sport_key)
    normalised["bet_side"] = _bet_side(normalised)
    normalised["edge_basis"] = _edge_basis(normalised)
    normalised["liability"] = _trade_liability(normalised)
    normalised["risk_odds"] = _risk_odds(normalised)
    normalised["risk_selection"] = _risk_selection(normalised)
    normalised["available_risk_at_target"] = _available_risk_at_target(normalised)
    normalised["entry_expected_value"] = _trade_expected_value(normalised)
    return normalised


def _normalise_live_order(item: dict[str, Any]) -> dict[str, Any]:
    normalised = {key: _jsonable(value) for key, value in item.items()}
    if "trade_id" not in normalised and normalised.get("order_id"):
        normalised["trade_id"] = normalised["order_id"]
    if "target_odds" not in normalised and normalised.get("limit_odds") not in {None, ""}:
        normalised["target_odds"] = normalised["limit_odds"]
    if "available_at_or_above_target" not in normalised:
        normalised["available_at_or_above_target"] = normalised.get("available_at_target")
    sport_key = str(normalised.get("sport_key") or "")
    normalised["sport_family"] = _sport_family(sport_key)
    normalised["bet_side"] = _bet_side(normalised)
    normalised["edge_basis"] = _edge_basis(normalised)
    normalised["liability"] = _float(normalised.get("liability")) or _trade_liability(normalised)
    normalised["risk_odds"] = _risk_odds(normalised)
    normalised["risk_selection"] = _risk_selection(normalised)
    normalised["available_risk_at_target"] = _available_risk_at_target(normalised)
    normalised["entry_expected_value"] = _trade_expected_value(normalised)
    return normalised


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


def _format_money(value: object) -> str:
    if value in {None, ""}:
        return ""
    return f"GBP {_float(value):,.2f}"


def _trade_liability(item: dict[str, Any]) -> float:
    stake = _float(item.get("stake"))
    odds = _float(item.get("target_odds"))
    if _bet_side(item) == "lay":
        return max(0.0, stake * max(0.0, odds - 1))
    return stake


def _trade_expected_value(item: dict[str, Any]) -> float:
    return _trade_liability(item) * _float(item.get("edge"))


def _risk_odds(item: dict[str, Any]) -> float:
    odds = _float(item.get("target_odds"))
    if odds <= 1:
        return 0.0
    commission_rate = _commission_rate(item)
    if _bet_side(item) == "lay":
        liability_per_unit = odds - 1
        return 1 + ((1 - commission_rate) / liability_per_unit)
    target_effective_odds = _float(item.get("target_effective_odds"))
    if target_effective_odds > 1:
        return target_effective_odds
    return 1 + ((odds - 1) * (1 - commission_rate))


def _risk_selection(item: dict[str, Any]) -> str:
    outcome = str(item.get("outcome_name") or "")
    if _bet_side(item) == "lay":
        return f"Not {outcome}"
    return outcome


def _available_risk_at_target(item: dict[str, Any]) -> float:
    available_size = _float(item.get("available_at_or_above_target"))
    odds = _float(item.get("target_odds"))
    if _bet_side(item) == "lay":
        return max(0.0, available_size * max(0.0, odds - 1))
    return available_size


def _has_applicable_liquidity(item: dict[str, Any]) -> bool:
    status = str(item.get("liquidity_status") or "").casefold()
    return status not in {"not_applicable", "not_checked"}


def _edge_basis(item: dict[str, Any]) -> str:
    return "liability" if _bet_side(item) == "lay" else "stake"


def _commission_rate(item: dict[str, Any]) -> float:
    bookmaker = str(item.get("target_bookmaker") or "").casefold()
    if bookmaker in {"matchbook", "smarkets", "betfair", "betfair_ex_uk", "betfair_ex_eu"}:
        return 0.02
    return 0.0


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


def _is_logged_in_last_24h(trade: dict[str, Any], *, now: datetime) -> bool:
    logged_at = _parse_datetime(trade.get("logged_at"))
    if logged_at is None:
        return False
    now_utc = _as_utc(now)
    return now_utc - timedelta(hours=24) <= logged_at <= now_utc


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    text = str(value or "")
    if not text:
        return None
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _filter_values(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in {None, ""}]
    return [str(value)]


def _first_filter_value(value: Any) -> str:
    values = _filter_values(value)
    return values[0] if values else ""


def _kelly_percent_filter_value(filters: dict[str, Any], base_name: str) -> str:
    pct_value = _first_filter_value(filters.get(f"{base_name}_pct"))
    if pct_value:
        return pct_value
    legacy_value = _first_filter_value(filters.get(base_name))
    if not legacy_value:
        return ""
    return f"{_float(legacy_value) * 100:g}"


def _optional_filter_float(value: Any) -> float | None:
    text = _first_filter_value(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _bounded_float(value: str, *, default: float, min_value: float, max_value: float) -> float:
    try:
        parsed = float(value)
    except ValueError:
        parsed = default
    return min(max(parsed, min_value), max_value)


def _bounded_filter_float(
    value: Any, *, default: float, min_value: float, max_value: float
) -> float:
    text = _first_filter_value(value)
    if not text:
        return default
    return _bounded_float(text, default=default, min_value=min_value, max_value=max_value)


def _bounded_percent_filter_float(
    value: Any,
    *,
    legacy_value: Any = None,
    default: float,
    min_value: float,
    max_value: float,
) -> float:
    text = _first_filter_value(value)
    if text:
        return _bounded_float(
            text,
            default=default * 100,
            min_value=min_value * 100,
            max_value=max_value * 100,
        ) / 100
    return _bounded_filter_float(
        legacy_value, default=default, min_value=min_value, max_value=max_value
    )


def _sport_family(sport_key: str) -> str:
    if not sport_key:
        return "unknown"
    return sport_key.split("_", 1)[0]


def _pretty_label(value: str) -> str:
    return " ".join(
        word.upper() if len(word) <= 3 else word.title()
        for word in value.replace("-", "_").split("_")
    )


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
