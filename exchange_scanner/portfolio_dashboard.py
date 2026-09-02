from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

EXPECTED_VENUES = ("Betfair", "Matchbook", "Smarkets")
OPEN_ORDER_STATUSES = {"submitted", "open", "partially_matched", "dry_run"}
FAILED_ORDER_STATUSES = {"failed", "rejected", "error", "unknown"}
SETTLED_STATUSES = {"settled"}
ACCOUNT_STALE_AFTER = timedelta(minutes=5)


def portfolio_payload(
    orders_table: Any,
    *,
    account_table: Any | None = None,
    filters: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = _as_utc(now or datetime.now(timezone.utc))
    filters = filters or {}
    all_orders = [_normalise_order(item) for item in _scan_all(orders_table)]
    live_orders = [item for item in all_orders if item["execution_mode"] == "live"]
    orders = _filter_orders(live_orders, filters=filters, now=now)
    accounts = _account_rows(account_table, now=now)
    positions = [item for item in orders if _is_open_position(item)]
    closed_trades = [item for item in orders if _is_closed_trade(item)]
    open_orders = [item for item in orders if _is_open_order(item)]
    summary = _portfolio_summary(
        orders,
        accounts=accounts,
        positions=positions,
        closed_trades=closed_trades,
        open_orders=open_orders,
    )
    return {
        "generated_at": now.isoformat(),
        "filters": _jsonable(filters),
        "summary": summary,
        "accounts": accounts,
        "positions": sorted(positions, key=lambda item: item.get("commence_time", "")),
        "open_orders": sorted(open_orders, key=lambda item: item.get("logged_at", ""), reverse=True),
        "closed_trades": sorted(
            closed_trades,
            key=lambda item: item.get("settled_at") or item.get("logged_at", ""),
            reverse=True,
        ),
        "orders": sorted(orders, key=lambda item: item.get("logged_at", ""), reverse=True),
        "venue_summary": _venue_summary(orders, accounts=accounts),
        "pnl_series": _pnl_series(closed_trades),
        "exceptions": _reconciliation_exceptions(
            orders,
            accounts=accounts,
            now=now,
        ),
        "excluded_dry_run_orders": len(all_orders) - len(live_orders),
    }


def portfolio_json(payload: dict[str, Any]) -> str:
    return json.dumps(_jsonable(payload), indent=2, sort_keys=True)


def render_portfolio_html(
    payload: dict[str, Any],
    *,
    view: str = "overview",
    token: str = "",
) -> str:
    view = _view(view)
    summary = payload["summary"]
    generated_at = _short_datetime(payload.get("generated_at"))
    exception_count = len(payload.get("exceptions", []))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Live Portfolio Console</title>
  <style>{_styles()}</style>
</head>
<body>
  <header class="topbar">
    <div class="brand"><span class="brand-mark">L</span><span><strong>LIVE PORTFOLIO</strong><small>Exchange execution console</small></span></div>
    <div class="execution-state"><i></i> Execution live</div>
    <div class="asof">As of {_escape(generated_at)} UTC</div>
  </header>
  <div class="app-shell">
    <nav class="sidebar" aria-label="Portfolio navigation">
      {_nav_link("Overview", "overview", view, token, "OV")}
      {_nav_link("Positions", "positions", view, token, "PX")}
      {_nav_link("Orders", "orders", view, token, "OR")}
      {_nav_link("Closed trades", "closed", view, token, "CL")}
      {_nav_link("Performance", "performance", view, token, "PF")}
      {_nav_link("Reconciliation", "reconciliation", view, token, "RC", count=exception_count)}
    </nav>
    <main>
      {_view_header(view, summary, token)}
      {_overview_html(payload) if view == "overview" else ""}
      {_positions_html(payload.get("positions", [])) if view == "positions" else ""}
      {_orders_html(payload.get("orders", [])) if view == "orders" else ""}
      {_closed_html(payload.get("closed_trades", []), summary) if view == "closed" else ""}
      {_performance_html(payload) if view == "performance" else ""}
      {_reconciliation_html(payload) if view == "reconciliation" else ""}
    </main>
  </div>
</body>
</html>"""


def _overview_html(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    return f"""
      {_kpi_strip(summary)}
      <div class="overview-grid">
        <section class="panel positions-panel">
          <div class="panel-head"><h2>Open positions</h2><span>{summary['open_positions']} positions · {_money(summary['open_position_risk'])} risk</span></div>
          {_positions_table(payload.get("positions", [])[:8], compact=True)}
        </section>
        <div class="side-stack">
          <section class="panel">
            <div class="panel-head"><h2>Venue funds</h2><span>Balance / available</span></div>
            {_account_list(payload.get("accounts", []))}
          </section>
          <section class="panel">
            <div class="panel-head"><h2>Performance</h2><span>Confirmed and score-estimated</span></div>
            {_performance_snapshot(summary, payload.get("pnl_series", []))}
          </section>
        </div>
      </div>
      <section class="panel lower-panel">
        <div class="panel-head"><h2>Recent activity</h2><span>Latest live venue orders</span></div>
        {_orders_table(payload.get("orders", [])[:10], compact=True)}
      </section>"""


def _positions_html(rows: list[dict[str, Any]]) -> str:
    risk = sum(_float(item.get("matched_risk")) for item in rows)
    mtm_rows = [item for item in rows if item.get("mark_to_market_clv") is not None]
    return f"""<div class="compact-stats">
      {_compact_stat("Positions", len(rows))}
      {_compact_stat("Matched risk", _money(risk))}
      {_compact_stat("Avg MTM CLV", _pct(_risk_weighted_mtm_clv(mtm_rows)) if mtm_rows else "Pending", tone=_tone(_risk_weighted_mtm_clv(mtm_rows)) if mtm_rows else "")}
      {_compact_stat("MTM measured", f"{len(mtm_rows)} / {len(rows)}")}
    </div>
    <section class="panel"><div class="panel-head"><h2>Matched, unsettled exposure</h2><span>Failed and unmatched orders excluded</span></div>{_positions_table(rows)}</section>"""


def _orders_html(rows: list[dict[str, Any]]) -> str:
    return f"""<div class="state-tabs"><span class="active">All {len(rows)}</span><span>Open {sum(1 for item in rows if _is_open_order(item))}</span><span>Matched {sum(1 for item in rows if _float(item.get('matched_size')) > 0)}</span><span>Failed {sum(1 for item in rows if item.get('status') in FAILED_ORDER_STATUSES)}</span></div>
    <section class="panel"><div class="panel-head"><h2>Unified order blotter</h2><span>Venue lifecycle and fill state</span></div>{_orders_table(rows)}</section>"""


def _closed_html(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    return f"""<div class="compact-stats">
      {_compact_stat("Settled", len(rows))}
      {_compact_stat("Confirmed / estimated", f"{summary['confirmed_settlements']} / {summary['estimated_settlements']}")}
      {_compact_stat("Confirmed P&L", _signed_money(summary['realized_pnl']), tone=_tone(summary['realized_pnl']))}
      {_compact_stat("Estimated P&L", _signed_money(summary['estimated_pnl']), tone=_tone(summary['estimated_pnl']))}
    </div>
    <section class="panel"><div class="panel-head"><h2>Closed trades</h2><span>Score-settled and exchange-confirmed</span></div>{_closed_table(rows)}</section>"""


def _performance_html(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    return f"""{_kpi_strip(summary)}
    <div class="performance-grid">
      <section class="panel"><div class="panel-head"><h2>Cumulative realized P&amp;L</h2><span>{summary['confirmed_settlements']} confirmed settlements</span></div>{_pnl_chart(payload.get('pnl_series', []), large=True)}</section>
      <section class="panel"><div class="panel-head"><h2>CLV quality</h2><span>Actual matched price vs close</span></div>{_clv_quality(summary)}</section>
    </div>
    <section class="panel lower-panel"><div class="panel-head"><h2>Performance by venue</h2><span>Net of recorded commission</span></div>{_venue_table(payload.get('venue_summary', []))}</section>"""


def _reconciliation_html(payload: dict[str, Any]) -> str:
    exceptions = payload.get("exceptions", [])
    return f"""<div class="compact-stats">
      {_compact_stat("Open exceptions", len(exceptions), tone="warn" if exceptions else "good")}
      {_compact_stat("Venue accounts", f"{sum(1 for item in payload.get('accounts', []) if item.get('status') == 'ok')} / 3")}
      {_compact_stat("Failed orders", payload['summary']['failed_orders'], tone="bad" if payload['summary']['failed_orders'] else "")}
      {_compact_stat("Dry runs excluded", payload.get('excluded_dry_run_orders', 0))}
    </div>
    <section class="panel"><div class="panel-head"><h2>Reconciliation exceptions</h2><span>Local ledger vs venue state</span></div>{_exception_list(exceptions)}</section>
    <section class="panel lower-panel"><div class="panel-head"><h2>Venue account freshness</h2><span>Missing data is never treated as zero</span></div>{_account_table(payload.get('accounts', []))}</section>"""


def _kpi_strip(summary: dict[str, Any]) -> str:
    available = _money(summary["available_funds"]) if summary["account_venues"] else "Unavailable"
    available_note = (
        f"{summary['account_venues']} of 3 venues reporting"
        if summary["account_venues"] < 3
        else f"{_money(summary['total_balance'])} total balance"
    )
    clv_value = _pct(summary["average_clv"]) if summary["clv_trades"] else "Pending"
    return f"""<section class="kpi-strip">
      {_kpi("Available funds", available, available_note)}
      {_kpi("Open position risk", _money(summary['open_position_risk']), f"{summary['open_positions']} matched positions")}
      {_kpi("Realized P&L", _signed_money(summary['realized_pnl']), f"{summary['confirmed_settlements']} venue-confirmed", _tone(summary['realized_pnl']))}
      {_kpi("Estimated P&L", _signed_money(summary['estimated_pnl']), f"{summary['estimated_settlements']} score-settled", _tone(summary['estimated_pnl']))}
      {_kpi("Closed CLV", clv_value, f"{_pct(summary['clv_beat_rate'])} beat close" if summary['clv_trades'] else "Awaiting closing prices", _tone(summary['average_clv']) if summary['clv_trades'] else "")}
      {_kpi("Execution quality", _pct(summary['fill_rate']), f"fill rate · {_money(summary['open_order_risk'])} open orders")}
    </section>"""


def _positions_table(rows: list[dict[str, Any]], *, compact: bool = False) -> str:
    if not rows:
        return _empty("No matched, unsettled positions.")
    visible = rows[:8] if compact else rows
    body = "".join(
        f"<tr><td>{_short_datetime(item.get('commence_time'))}</td>"
        f"<td>{_venue(item.get('venue'))}</td>"
        f"<td class='event'><strong>{_escape(item.get('event_name'))}</strong><small>{_escape(item.get('risk_selection'))}</small></td>"
        f"<td>{_escape(item.get('bet_side', '')).title()}</td>"
        f"<td class='num'>{_money(item.get('matched_risk'))}</td>"
        f"<td class='num'>{_number(item.get('risk_odds'))}</td>"
        f"<td class='num {_tone(item.get('edge'))}'>{_pct(item.get('edge'))}</td>"
        f"{_position_mtm_cell(item)}"
        f"<td>{_escape(item.get('status'))}</td></tr>"
        for item in visible
    )
    return f"""<div class="table-wrap"><table><thead><tr><th>Starts</th><th>Venue</th><th>Event / selection</th><th>Side</th><th class="num">Matched risk</th><th class="num">Risk odds</th><th class="num">Entry edge</th><th class="num">MTM CLV</th><th>Status</th></tr></thead><tbody>{body}</tbody></table></div>"""


def _position_mtm_cell(item: dict[str, Any]) -> str:
    value = item.get("mark_to_market_clv")
    checked_at = _short_datetime(item.get("mark_to_market_checked_at"))
    market_odds = _number(item.get("mark_to_market_odds"))
    detail = f"Current market odds {market_odds}; priced {checked_at} UTC"
    return (
        f"<td class='num {_tone(value)}' title='{_escape_attr(detail)}'>"
        f"{_pct_or_pending(value)}</td>"
    )


def _orders_table(rows: list[dict[str, Any]], *, compact: bool = False) -> str:
    if not rows:
        return _empty("No live orders.")
    visible = rows[:10] if compact else rows[:250]
    body = "".join(
        f"<tr><td>{_short_datetime(item.get('logged_at'))}</td>"
        f"<td class='{_status_tone(item.get('status'))}'>{_escape(item.get('status'))}</td>"
        f"<td>{_venue(item.get('venue'))}</td>"
        f"<td class='event'><strong>{_escape(item.get('event_name'))}</strong><small>{_escape(item.get('risk_selection'))}</small></td>"
        f"<td>{_escape(item.get('bet_side', '')).title()}</td>"
        f"<td class='num'>{_number(item.get('limit_odds'))}</td>"
        f"<td class='num'>{_money(item.get('requested_risk'))}</td>"
        f"<td class='num'>{_money(item.get('matched_risk'))}</td>"
        f"<td class='num'>{_money(item.get('remaining_risk'))}</td>"
        f"<td class='order-id'>{_escape(item.get('venue_order_id') or '—')}</td>"
        f"<td class='error'>{_escape(item.get('error') or '')}</td></tr>"
        for item in visible
    )
    return f"""<div class="table-wrap"><table><thead><tr><th>Submitted</th><th>State</th><th>Venue</th><th>Event / selection</th><th>Side</th><th class="num">Limit odds</th><th class="num">Requested risk</th><th class="num">Matched risk</th><th class="num">Remaining risk</th><th>Venue order</th><th>Error</th></tr></thead><tbody>{body}</tbody></table></div>"""


def _closed_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return _empty("No exchange-confirmed settlements yet.")
    body = "".join(
        f"<tr><td>{_short_datetime(item.get('settled_at') or item.get('commence_time'))}</td>"
        f"<td>{_venue(item.get('venue'))}</td>"
        f"<td class='event'><strong>{_escape(item.get('event_name'))}</strong><small>{_escape(item.get('risk_selection'))}</small></td>"
        f"<td>{_escape(item.get('bet_side', '')).title()}</td>"
        f"<td>{_escape(item.get('result') or 'Settled')}</td>"
        f"<td class='{_status_tone(item.get('pnl_status'))}'>{_escape(item.get('pnl_status', '').title())}</td>"
        f"<td class='num'>{_money(item.get('matched_risk'))}</td>"
        f"<td class='num'>{_number(item.get('risk_odds'))}</td>"
        f"<td class='num {_tone(item.get('clv'))}'>{_pct_or_pending(item.get('clv'))}</td>"
        f"<td class='num'>{_signed_money(item.get('gross_profit'))}</td>"
        f"<td class='num'>{_money(item.get('commission'))}</td>"
        f"<td class='num {_tone(item.get('net_profit'))}'>{_signed_money(item.get('net_profit'))}</td></tr>"
        for item in rows[:250]
    )
    return f"""<div class="table-wrap"><table><thead><tr><th>Settled</th><th>Venue</th><th>Event / selection</th><th>Side</th><th>Result</th><th>P&amp;L state</th><th class="num">Matched risk</th><th class="num">Risk odds</th><th class="num">CLV</th><th class="num">Gross P&amp;L</th><th class="num">Commission</th><th class="num">Net P&amp;L</th></tr></thead><tbody>{body}</tbody></table></div>"""


def _account_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return _empty("Account snapshots have not been configured.")
    return "".join(
        f"<div class='account-row'><span>{_venue(item.get('venue'))}</span>"
        f"<span class='account-values'><strong>{_money(item.get('balance'))} / {_money(item.get('available_funds'))}</strong>"
        f"<small>{_money(item.get('reserved_funds'))} reserved · {_escape(item.get('freshness'))}</small></span></div>"
        for item in rows
    )


def _account_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return _empty("No venue account snapshots are available.")
    body = "".join(
        f"<tr><td>{_venue(item.get('venue'))}</td><td class='num'>{_money(item.get('balance'))}</td>"
        f"<td class='num'>{_money(item.get('available_funds'))}</td><td class='num'>{_money(item.get('reserved_funds'))}</td>"
        f"<td class='{_status_tone(item.get('status'))}'>{_escape(item.get('status'))}</td>"
        f"<td>{_short_datetime(item.get('checked_at'))}</td><td class='error'>{_escape(item.get('error'))}</td></tr>"
        for item in rows
    )
    return f"<div class='table-wrap'><table><thead><tr><th>Venue</th><th class='num'>Balance</th><th class='num'>Available</th><th class='num'>Reserved</th><th>Status</th><th>Checked</th><th>Error</th></tr></thead><tbody>{body}</tbody></table></div>"


def _venue_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return _empty("No venue performance is available.")
    body = "".join(
        f"<tr><td>{_venue(item.get('venue'))}</td><td class='num'>{item['orders']}</td>"
        f"<td class='num'>{item['positions']}</td><td class='num'>{_money(item['matched_risk'])}</td>"
        f"<td class='num'>{item['closed_trades']}</td><td class='num {_tone(item['realized_pnl'])}'>{_signed_money(item['realized_pnl'])}</td>"
        f"<td class='num {_tone(item['estimated_pnl'])}'>{_signed_money(item['estimated_pnl'])}</td>"
        f"<td class='num {_tone(item['average_clv'])}'>{_pct_or_pending(item['average_clv'] if item['clv_trades'] else None)}</td>"
        f"<td class='num'>{_pct(item['fill_rate'])}</td></tr>"
        for item in rows
    )
    return f"<div class='table-wrap'><table><thead><tr><th>Venue</th><th class='num'>Orders</th><th class='num'>Positions</th><th class='num'>Matched risk</th><th class='num'>Settled</th><th class='num'>Realized P&amp;L</th><th class='num'>Estimated P&amp;L</th><th class='num'>Avg CLV</th><th class='num'>Fill rate</th></tr></thead><tbody>{body}</tbody></table></div>"


def _performance_snapshot(summary: dict[str, Any], series: list[dict[str, Any]]) -> str:
    return f"""<div class="performance-meta"><span>Realized P&amp;L<strong class="{_tone(summary['realized_pnl'])}">{_signed_money(summary['realized_pnl'])}</strong></span><span>Estimated P&amp;L<strong class="{_tone(summary['estimated_pnl'])}">{_signed_money(summary['estimated_pnl'])}</strong></span></div>{_pnl_chart(series)}"""


def _pnl_chart(series: list[dict[str, Any]], *, large: bool = False) -> str:
    height = 210 if large else 100
    if not series:
        return f"<div class='chart-empty' style='height:{height}px'>P&amp;L chart begins after the first confirmed settlement.</div>"
    values = [0.0, *[_float(item.get("cumulative_pnl")) for item in series]]
    minimum = min(values)
    maximum = max(values)
    span = maximum - minimum or 1.0
    width = 600.0
    points = []
    for index, value in enumerate(values):
        x = width * index / max(1, len(values) - 1)
        y = (height - 20) - ((value - minimum) / span * (height - 40))
        points.append(f"{x:.1f},{y:.1f}")
    return f"""<svg class="pnl-chart" viewBox="0 0 600 {height}" role="img" aria-label="Cumulative realized profit chart"><line x1="0" y1="{height / 2:.1f}" x2="600" y2="{height / 2:.1f}"></line><polyline points="{' '.join(points)}"></polyline></svg>"""


def _clv_quality(summary: dict[str, Any]) -> str:
    if not summary["clv_trades"]:
        return _empty("Closing prices have not been recorded for settled trades yet.")
    return f"""<div class="clv-block"><div><span>Average CLV</span><strong class="{_tone(summary['average_clv'])}">{_pct(summary['average_clv'])}</strong></div><div><span>Beat / miss / tie</span><strong>{summary['clv_beats']} / {summary['clv_misses']} / {summary['clv_ties']}</strong></div><div><span>Beat rate</span><strong class="{_tone(summary['clv_beat_rate'])}">{_pct(summary['clv_beat_rate'])}</strong></div><p>CLV uses the recorded matched price. For lay bets, risk-normalized closing EV should be the primary comparison once available.</p></div>"""


def _exception_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return _empty("No reconciliation exceptions.", tone="good")
    return "".join(
        f"<div class='exception-row'><span class='exception-severity {item['severity']}'>{_escape(item['severity'])}</span>"
        f"<span><strong>{_escape(item['title'])}</strong><small>{_escape(item['detail'])}</small></span>"
        f"<time>{_escape(item.get('venue') or '')}</time></div>"
        for item in rows
    )


def _portfolio_summary(
    orders: list[dict[str, Any]],
    *,
    accounts: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
) -> dict[str, Any]:
    confirmed = [item for item in closed_trades if item.get("pnl_status") != "estimated"]
    estimated = [item for item in closed_trades if item.get("pnl_status") == "estimated"]
    pnl = sum(_float(item.get("net_profit")) for item in confirmed)
    settled_risk = sum(_float(item.get("matched_risk")) for item in confirmed)
    clv_rows = [item for item in closed_trades if item.get("clv") is not None]
    clv_values = [_float(item.get("clv")) for item in clv_rows]
    beats = sum(1 for value in clv_values if value > 0)
    misses = sum(1 for value in clv_values if value < 0)
    placed = [item for item in orders if item.get("venue_order_id")]
    filled = [item for item in placed if _float(item.get("matched_size")) > 0]
    mtm_positions = [item for item in positions if item.get("mark_to_market_clv") is not None]
    return {
        "total_balance": sum(_float(item.get("balance")) for item in accounts if item["status"] == "ok"),
        "available_funds": sum(
            _float(item.get("available_funds")) for item in accounts if item["status"] == "ok"
        ),
        "account_exposure": sum(
            abs(_float(item.get("exposure"))) for item in accounts if item["status"] == "ok"
        ),
        "account_venues": sum(1 for item in accounts if item["status"] == "ok"),
        "open_positions": len(positions),
        "open_position_risk": sum(_float(item.get("matched_risk")) for item in positions),
        "open_position_mtm_clv": _risk_weighted_mtm_clv(mtm_positions),
        "open_position_mtm_clv_positions": len(mtm_positions),
        "open_orders": len(open_orders),
        "open_order_risk": sum(_float(item.get("remaining_risk")) for item in open_orders),
        "closed_trades": len(closed_trades),
        "confirmed_settlements": len(confirmed),
        "settled_won": sum(1 for item in closed_trades if _float(item.get("net_profit")) > 0),
        "settled_lost": sum(1 for item in closed_trades if _float(item.get("net_profit")) < 0),
        "realized_pnl": pnl,
        "estimated_pnl": sum(_float(item.get("net_profit")) for item in estimated),
        "estimated_settlements": len(estimated),
        "settled_risk": settled_risk,
        "risk_roi": pnl / settled_risk if settled_risk else 0.0,
        "clv_trades": len(clv_rows),
        "average_clv": sum(clv_values) / len(clv_values) if clv_values else 0.0,
        "clv_beats": beats,
        "clv_misses": misses,
        "clv_ties": len(clv_values) - beats - misses,
        "clv_beat_rate": beats / len(clv_values) if clv_values else 0.0,
        "orders": len(orders),
        "failed_orders": sum(1 for item in orders if item["status"] in FAILED_ORDER_STATUSES),
        "fill_rate": len(filled) / len(placed) if placed else 0.0,
    }


def _venue_summary(
    orders: list[dict[str, Any]],
    *,
    accounts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    account_by_venue = {item["venue"].casefold(): item for item in accounts}
    rows = []
    for venue in EXPECTED_VENUES:
        venue_orders = [item for item in orders if item["venue"].casefold() == venue.casefold()]
        positions = [item for item in venue_orders if _is_open_position(item)]
        closed = [item for item in venue_orders if _is_closed_trade(item)]
        confirmed_closed = [item for item in closed if item.get("pnl_status") != "estimated"]
        estimated_closed = [item for item in closed if item.get("pnl_status") == "estimated"]
        placed = [item for item in venue_orders if item.get("venue_order_id")]
        filled = [item for item in placed if _float(item.get("matched_size")) > 0]
        clv_values = [_float(item["clv"]) for item in closed if item.get("clv") is not None]
        account = account_by_venue.get(venue.casefold(), {})
        rows.append(
            {
                "venue": venue,
                "orders": len(venue_orders),
                "positions": len(positions),
                "matched_risk": sum(_float(item.get("matched_risk")) for item in positions),
                "closed_trades": len(closed),
                "realized_pnl": sum(
                    _float(item.get("net_profit")) for item in confirmed_closed
                ),
                "estimated_pnl": sum(
                    _float(item.get("net_profit")) for item in estimated_closed
                ),
                "clv_trades": len(clv_values),
                "average_clv": sum(clv_values) / len(clv_values) if clv_values else 0.0,
                "fill_rate": len(filled) / len(placed) if placed else 0.0,
                "balance": account.get("balance"),
                "available_funds": account.get("available_funds"),
            }
        )
    return rows


def _pnl_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cumulative = 0.0
    result = []
    confirmed = [item for item in rows if item.get("pnl_status") != "estimated"]
    for item in sorted(
        confirmed,
        key=lambda row: row.get("settled_at") or row.get("commence_time", ""),
    ):
        cumulative += _float(item.get("net_profit"))
        result.append(
            {
                "at": item.get("settled_at") or item.get("commence_time") or item.get("logged_at"),
                "pnl": _float(item.get("net_profit")),
                "cumulative_pnl": cumulative,
            }
        )
    return result


def _reconciliation_exceptions(
    orders: list[dict[str, Any]],
    *,
    accounts: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    account_by_venue = {item["venue"].casefold(): item for item in accounts}
    for venue in EXPECTED_VENUES:
        account = account_by_venue.get(venue.casefold())
        if account is None:
            result.append(
                {
                    "severity": "warn",
                    "title": "Account snapshot missing",
                    "detail": "Available funds and venue exposure are not yet being reconciled.",
                    "venue": venue,
                }
            )
        elif account["status"] != "ok":
            result.append(
                {
                    "severity": "bad",
                    "title": "Account snapshot unavailable",
                    "detail": account.get("error") or "The most recent venue balance request failed.",
                    "venue": venue,
                }
            )
        elif account.get("stale"):
            result.append(
                {
                    "severity": "warn",
                    "title": "Account snapshot stale",
                    "detail": f"Last successful balance check was {account.get('freshness')}.",
                    "venue": venue,
                }
            )
    for item in orders:
        if item["status"] in FAILED_ORDER_STATUSES:
            result.append(
                {
                    "severity": "bad",
                    "title": "Venue order failed",
                    "detail": item.get("error") or f"Order {item.get('order_id')} failed without detail.",
                    "venue": item["venue"],
                }
            )
        if _float(item.get("matched_size")) > 0 and not item.get("venue_order_id"):
            result.append(
                {
                    "severity": "bad",
                    "title": "Matched position has no venue order id",
                    "detail": f"{item.get('event_name')} cannot be reconciled to the exchange.",
                    "venue": item["venue"],
                }
            )
        if item.get("status") == "settled" and item.get("pnl_status") == "estimated":
            result.append(
                {
                    "severity": "warn",
                    "title": "Settlement awaiting venue confirmation",
                    "detail": (
                        f"{item.get('event_name')} has a score-derived result; "
                        "realized P&L remains excluded until the exchange confirms it."
                    ),
                    "venue": item["venue"],
                }
            )
        commence = _parse_datetime(item.get("commence_time"))
        if _is_open_order(item) and commence and commence < now:
            result.append(
                {
                    "severity": "warn",
                    "title": "Order remains open after event start",
                    "detail": f"{item.get('event_name')} started at {_short_datetime(commence)} UTC.",
                    "venue": item["venue"],
                }
            )
    return result


def _account_rows(table: Any | None, *, now: datetime) -> list[dict[str, Any]]:
    if table is None:
        return []
    rows = []
    for item in _scan_all(table):
        checked_at = _parse_datetime(item.get("checked_at") or item.get("updated_at"))
        stale = checked_at is None or now - checked_at > ACCOUNT_STALE_AFTER
        venue = _canonical_venue(item.get("venue") or item.get("bookmaker"))
        exposure = _optional_float(item.get("exposure")) or 0.0
        rows.append(
            {
                "venue": venue,
                "currency": str(item.get("currency") or "GBP"),
                "balance": _optional_float(item.get("balance")),
                "available_funds": _optional_float(
                    item.get("available_funds")
                    if item.get("available_funds") is not None
                    else item.get("available_balance")
                ),
                "exposure": exposure,
                "reserved_funds": abs(exposure),
                "status": str(item.get("status") or ("ok" if checked_at else "missing")).casefold(),
                "checked_at": checked_at.isoformat() if checked_at else "",
                "freshness": _freshness(checked_at, now=now),
                "stale": stale,
                "error": str(item.get("error") or ""),
            }
        )
    return sorted(rows, key=lambda item: EXPECTED_VENUES.index(item["venue"]) if item["venue"] in EXPECTED_VENUES else 99)


def _normalise_order(item: dict[str, Any]) -> dict[str, Any]:
    row = {key: _jsonable(value) for key, value in item.items()}
    bet_side = "lay" if str(row.get("bet_side") or "back").casefold() == "lay" else "back"
    odds = _float(row.get("avg_matched_odds")) or _float(
        row.get("limit_odds") or row.get("target_odds")
    )
    matched_size = _float(row.get("matched_size"))
    remaining_size = _float(row.get("remaining_size"))
    requested_stake = _float(row.get("stake"))
    requested_risk = _float(row.get("liability")) or _risk_from_stake(
        requested_stake,
        odds=odds,
        bet_side=bet_side,
    )
    commission_rate = _float(row.get("commission_rate"))
    matched_risk = _risk_from_stake(matched_size, odds=odds, bet_side=bet_side)
    remaining_risk = _risk_from_stake(remaining_size, odds=odds, bet_side=bet_side)
    profit = _optional_float(
        row.get("net_profit") if row.get("net_profit") is not None else row.get("profit")
    )
    gross_profit = _optional_float(row.get("gross_profit"))
    commission = _optional_float(row.get("commission")) or 0.0
    if gross_profit is None and profit is not None:
        gross_profit = profit + commission
    clv = _optional_float(
        row.get("closing_ev_per_risk")
        if row.get("closing_ev_per_risk") is not None
        else row.get("target_clv")
    )
    mark_to_market_clv = _optional_float(
        row.get("mark_to_market_clv")
        if row.get("mark_to_market_clv") is not None
        else row.get("target_clv")
    )
    status = str(row.get("status") or "unknown").casefold()
    return {
        **row,
        "venue": _canonical_venue(row.get("target_bookmaker")),
        "status": status,
        "execution_mode": str(row.get("execution_mode") or "live").casefold(),
        "bet_side": bet_side,
        "risk_selection": _risk_selection(row, bet_side=bet_side),
        "limit_odds": _float(row.get("limit_odds") or row.get("target_odds")),
        "avg_matched_odds": odds,
        "requested_risk": requested_risk,
        "matched_size": matched_size,
        "matched_risk": matched_risk,
        "remaining_size": remaining_size,
        "remaining_risk": remaining_risk,
        "risk_odds": _risk_odds(odds, bet_side=bet_side, commission_rate=commission_rate),
        "edge": _optional_float(row.get("edge")),
        "clv": clv,
        "beat_close": clv > 0 if clv is not None else None,
        "mark_to_market_clv": mark_to_market_clv,
        "mark_to_market_odds": _optional_float(row.get("closing_target_odds")),
        "mark_to_market_checked_at": str(row.get("closing_checked_at") or ""),
        "gross_profit": gross_profit,
        "commission": commission,
        "net_profit": profit,
        "pnl_status": str(row.get("pnl_status") or "confirmed").casefold(),
        "result": str(row.get("result") or row.get("winner") or ""),
        "settled_at": str(row.get("settled_at") or row.get("settlement_checked_at") or ""),
    }


def _filter_orders(
    orders: list[dict[str, Any]],
    *,
    filters: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    venue = str(filters.get("venue") or "").casefold()
    days = _float(filters.get("days"))
    cutoff = now - timedelta(days=days) if days > 0 else None
    result = []
    for item in orders:
        if venue and item["venue"].casefold() != venue:
            continue
        logged_at = _parse_datetime(item.get("logged_at"))
        if cutoff and logged_at and logged_at < cutoff:
            continue
        result.append(item)
    return result


def _is_open_position(item: dict[str, Any]) -> bool:
    return _float(item.get("matched_size")) > 0 and item.get("status") not in SETTLED_STATUSES


def _is_closed_trade(item: dict[str, Any]) -> bool:
    return _float(item.get("matched_size")) > 0 and item.get("status") in SETTLED_STATUSES


def _is_open_order(item: dict[str, Any]) -> bool:
    return (
        item.get("status") in OPEN_ORDER_STATUSES
        and _float(item.get("remaining_size")) > 0.001
        and item.get("execution_mode") == "live"
    )


def _risk_from_stake(stake: float, *, odds: float, bet_side: str) -> float:
    if stake <= 0:
        return 0.0
    if bet_side == "lay":
        return stake * max(0.0, odds - 1.0)
    return stake


def _risk_weighted_mtm_clv(rows: list[dict[str, Any]]) -> float:
    total_risk = sum(_float(item.get("matched_risk")) for item in rows)
    if total_risk <= 0:
        return 0.0
    return sum(
        _float(item.get("mark_to_market_clv")) * _float(item.get("matched_risk"))
        for item in rows
    ) / total_risk


def _risk_odds(odds: float, *, bet_side: str, commission_rate: float) -> float:
    if odds <= 1:
        return 0.0
    if bet_side == "lay":
        return 1.0 + ((1.0 - commission_rate) / (odds - 1.0))
    return 1.0 + ((odds - 1.0) * (1.0 - commission_rate))


def _risk_selection(item: dict[str, Any], *, bet_side: str) -> str:
    outcome = str(item.get("outcome_name") or "")
    return f"Not {outcome}" if bet_side == "lay" and outcome else outcome


def _scan_all(table: Any) -> list[dict[str, Any]]:
    items = []
    scan_kwargs: dict[str, Any] = {}
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        scan_kwargs["ExclusiveStartKey"] = last_key


def _view(value: str) -> str:
    allowed = {"overview", "positions", "orders", "closed", "performance", "reconciliation"}
    return value if value in allowed else "overview"


def _view_header(view: str, summary: dict[str, Any], token: str) -> str:
    titles = {
        "overview": ("Portfolio command center", "Cash, risk, execution and performance"),
        "positions": ("Open positions", "Matched, unsettled exposure across all venues"),
        "orders": ("Orders", "Venue submissions, fills, cancellations and failures"),
        "closed": ("Closed trades", "Settlement, commission and realized P&L"),
        "performance": ("Performance", "Returns, closing-line quality and execution"),
        "reconciliation": ("Reconciliation", "Exchange state compared with the local ledger"),
    }
    title, subtitle = titles[view]
    json_href = "?" + urlencode({"token": token, "view": view, "format": "json"})
    return f"<div class='page-head'><div><h1>{title}</h1><p>{subtitle}</p></div><a href='{_escape_attr(json_href)}'>JSON</a></div>"


def _nav_link(
    label: str,
    target: str,
    current: str,
    token: str,
    icon: str,
    *,
    count: int | None = None,
) -> str:
    href = "?" + urlencode({"token": token, "view": target})
    badge = f"<b>{count}</b>" if count else ""
    return f"<a class='nav-item {'active' if target == current else ''}' href='{_escape_attr(href)}'><i>{icon}</i><span>{_escape(label)}</span>{badge}</a>"


def _kpi(label: str, value: str, note: str, tone: str = "") -> str:
    return f"<div class='kpi'><span>{_escape(label)}</span><strong class='{tone}'>{_escape(value)}</strong><small>{_escape(note)}</small></div>"


def _compact_stat(label: str, value: Any, *, tone: str = "") -> str:
    return f"<div class='compact-stat'><span>{_escape(label)}</span><strong class='{tone}'>{_escape(value)}</strong></div>"


def _venue(value: Any) -> str:
    venue = _canonical_venue(value)
    return f"<span class='venue'><i class='{venue.casefold()}'></i>{_escape(venue)}</span>"


def _canonical_venue(value: Any) -> str:
    text = str(value or "Unknown")
    folded = text.casefold()
    if "betfair" in folded:
        return "Betfair"
    if "matchbook" in folded:
        return "Matchbook"
    if "smarkets" in folded:
        return "Smarkets"
    return text.title()


def _status_tone(value: Any) -> str:
    status = str(value or "").casefold()
    if status in FAILED_ORDER_STATUSES:
        return "bad"
    if status in {"matched", "settled"}:
        return "good"
    if status in OPEN_ORDER_STATUSES or status in {
        "cancelled",
        "partially_matched_cancelled",
        "estimated",
    }:
        return "warn"
    return ""


def _tone(value: Any) -> str:
    number = _float(value)
    if number > 0:
        return "good"
    if number < 0:
        return "bad"
    return ""


def _starts_today(item: dict[str, Any]) -> bool:
    commence = _parse_datetime(item.get("commence_time"))
    return bool(commence and commence.date() == datetime.now(timezone.utc).date())


def _freshness(checked_at: datetime | None, *, now: datetime) -> str:
    if checked_at is None:
        return "never"
    seconds = max(0, int((now - checked_at).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _short_datetime(value: Any) -> str:
    parsed = _parse_datetime(value)
    return parsed.strftime("%d %b %H:%M") if parsed else "—"


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    return _float(value)


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(value: Any) -> str:
    if value is None:
        return "—"
    return f"£{_float(value):,.2f}"


def _signed_money(value: Any) -> str:
    if value is None:
        return "—"
    number = _float(value)
    return f"{'+' if number > 0 else ''}£{number:,.2f}"


def _pct(value: Any) -> str:
    return f"{_float(value):.2%}"


def _pct_or_pending(value: Any) -> str:
    return "Pending" if value is None else _pct(value)


def _number(value: Any) -> str:
    return "—" if value is None else f"{_float(value):.2f}"


def _empty(message: str, *, tone: str = "") -> str:
    return f"<div class='empty {tone}'>{_escape(message)}</div>"


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _escape_attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _styles() -> str:
    return """
    :root { color-scheme: dark; --bg:#0e141b; --nav:#121920; --panel:#151d25; --panel2:#111820; --line:#303b46; --line2:#29343f; --text:#e6edf3; --muted:#8d9aa7; --good:#56d6a0; --bad:#ff817a; --warn:#f2c14e; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size:13px; letter-spacing:0; }
    .topbar { align-items:center; background:var(--nav); border-bottom:1px solid var(--line); display:grid; grid-template-columns:1fr auto auto; min-height:54px; padding:0 18px; position:sticky; top:0; z-index:5; }
    .brand { align-items:center; display:flex; gap:9px; }
    .brand-mark { align-items:center; background:var(--good); border-radius:3px; color:#102017; display:flex; font-weight:700; height:27px; justify-content:center; width:27px; }
    .brand strong,.brand small { display:block; letter-spacing:0; }
    .brand strong { font-size:13px; font-weight:600; }
    .brand small,.asof { color:#aab7c4; font-size:11px; }
    .execution-state { align-items:center; display:flex; font-size:12px; gap:7px; margin-right:20px; }
    .execution-state i { background:var(--good); border-radius:50%; height:7px; width:7px; }
    .app-shell { display:grid; grid-template-columns:182px minmax(0,1fr); min-height:calc(100vh - 54px); }
    .sidebar { background:var(--nav); border-right:1px solid var(--line); padding:14px 8px; }
    .nav-item { align-items:center; border-radius:4px; color:#aeb9c4; display:grid; gap:9px; grid-template-columns:28px 1fr auto; margin-bottom:3px; padding:8px 9px; text-decoration:none; }
    .nav-item:hover,.nav-item.active { background:#26313c; color:#f3f6f9; }
    .nav-item i { align-items:center; border:1px solid #43505d; border-radius:3px; display:flex; font-size:9px; font-style:normal; height:23px; justify-content:center; width:23px; }
    .nav-item b { background:var(--warn); border-radius:8px; color:#1c1604; font-size:9px; padding:1px 5px; }
    main { min-width:0; padding:18px; }
    .page-head { align-items:flex-end; display:flex; justify-content:space-between; margin:0 0 14px; }
    .page-head h1 { font-size:20px; font-weight:600; margin:0 0 3px; }
    .page-head p { color:var(--muted); font-size:11px; margin:0; }
    .page-head a { border:1px solid var(--line); border-radius:3px; color:#b8c3cd; font-size:11px; padding:6px 9px; text-decoration:none; }
    .kpi-strip { display:grid; gap:1px; grid-template-columns:repeat(5,minmax(0,1fr)); margin-bottom:14px; }
    .kpi { background:var(--panel); border:1px solid var(--line); min-height:84px; padding:11px 12px; }
    .kpi:first-child { border-radius:4px 0 0 4px; }.kpi:last-child { border-radius:0 4px 4px 0; }
    .kpi span,.kpi small,.compact-stat span { color:var(--muted); display:block; font-size:10px; }
    .kpi strong { display:block; font-size:20px; font-variant-numeric:tabular-nums; font-weight:600; margin:7px 0 3px; }
    .overview-grid { display:grid; gap:14px; grid-template-columns:minmax(0,1.45fr) minmax(280px,.55fr); }
    .side-stack { display:grid; gap:14px; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:4px; min-width:0; }
    .panel-head { align-items:center; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; min-height:40px; padding:0 12px; }
    .panel-head h2 { font-size:12px; font-weight:600; margin:0; }.panel-head span { color:var(--muted); font-size:10px; }
    .lower-panel { margin-top:14px; }
    .table-wrap { overflow-x:auto; }
    table { border-collapse:collapse; min-width:760px; width:100%; }
    th { background:var(--panel2); color:var(--muted); font-size:9px; font-weight:600; padding:8px 9px; text-align:left; text-transform:uppercase; white-space:nowrap; }
    td { border-top:1px solid var(--line2); color:#ccd5dd; font-size:10px; padding:8px 9px; vertical-align:middle; white-space:nowrap; }
    td.event { max-width:260px; white-space:normal; }td.event strong,td.event small { display:block; }td.event strong { color:#f0f4f7; font-weight:600; }td.event small { color:var(--muted); margin-top:2px; }
    .num { font-variant-numeric:tabular-nums; text-align:right; }.order-id { color:#9facb9; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }.error { color:var(--bad); max-width:280px; overflow:hidden; text-overflow:ellipsis; }
    .venue { align-items:center; display:inline-flex; gap:7px; }.venue i { border-radius:2px; display:inline-block; height:14px; width:3px; }.venue i.betfair { background:#f6a623; }.venue i.matchbook { background:#e04a59; }.venue i.smarkets { background:#2ba6cb; }
    .account-row { align-items:center; border-top:1px solid var(--line2); display:flex; justify-content:space-between; padding:10px 12px; }.account-row:first-of-type { border-top:0; }.account-values { font-variant-numeric:tabular-nums; text-align:right; }.account-values strong,.account-values small { display:block; }.account-values strong { font-size:11px; }.account-values small { color:var(--muted); font-size:9px; margin-top:2px; }
    .performance-meta { display:flex; gap:28px; padding:11px 12px 0; }.performance-meta span { color:var(--muted); font-size:10px; }.performance-meta strong { display:block; font-size:16px; font-variant-numeric:tabular-nums; margin-top:3px; }
    .pnl-chart { display:block; height:auto; padding:8px 12px 10px; width:100%; }.pnl-chart line { stroke:var(--line); }.pnl-chart polyline { fill:none; stroke:var(--good); stroke-width:2; vector-effect:non-scaling-stroke; }
    .chart-empty,.empty { align-items:center; color:var(--muted); display:flex; font-size:11px; justify-content:center; min-height:100px; padding:20px; text-align:center; }.empty.good { color:var(--good); }
    .compact-stats { display:grid; gap:12px; grid-template-columns:repeat(4,minmax(0,1fr)); margin-bottom:14px; }.compact-stat { border-left:2px solid #657483; padding:3px 10px; }.compact-stat strong { display:block; font-size:17px; font-variant-numeric:tabular-nums; font-weight:600; margin-top:4px; }
    .state-tabs { display:flex; gap:3px; margin:0 0 10px; }.state-tabs span { border-bottom:2px solid transparent; color:var(--muted); font-size:11px; padding:6px 9px; }.state-tabs .active { border-color:var(--text); color:var(--text); }
    .performance-grid { display:grid; gap:14px; grid-template-columns:minmax(0,1.35fr) minmax(280px,.65fr); }.clv-block { display:grid; gap:10px; grid-template-columns:repeat(3,1fr); padding:16px; }.clv-block span,.clv-block strong { display:block; }.clv-block span { color:var(--muted); font-size:10px; }.clv-block strong { font-size:17px; margin-top:5px; }.clv-block p { color:var(--muted); font-size:10px; grid-column:1/-1; margin:4px 0 0; }
    .exception-row { align-items:center; border-top:1px solid var(--line2); display:grid; gap:11px; grid-template-columns:44px 1fr auto; padding:10px 12px; }.exception-row:first-of-type { border-top:0; }.exception-row strong,.exception-row small { display:block; }.exception-row strong { font-size:11px; }.exception-row small { color:var(--muted); font-size:10px; margin-top:2px; }.exception-row time { color:var(--muted); font-size:10px; }.exception-severity { border-radius:3px; font-size:9px; font-weight:700; padding:3px 5px; text-align:center; text-transform:uppercase; }.exception-severity.warn { background:#3d3210; color:var(--warn); }.exception-severity.bad { background:#3b2020; color:var(--bad); }
    .good { color:var(--good)!important; }.bad { color:var(--bad)!important; }.warn { color:var(--warn)!important; }
    @media (max-width:900px) { .app-shell { grid-template-columns:58px minmax(0,1fr); }.nav-item { grid-template-columns:1fr; justify-items:center; padding:8px 0; }.nav-item span,.nav-item b { display:none; }.kpi-strip { grid-template-columns:repeat(3,minmax(0,1fr)); gap:6px; }.kpi:first-child,.kpi:last-child { border-radius:4px; }.overview-grid,.performance-grid { grid-template-columns:1fr; } }
    @media (max-width:560px) { .topbar { grid-template-columns:1fr auto; padding:0 10px; }.execution-state { display:none; }.app-shell { display:block; }.sidebar { border-bottom:1px solid var(--line); border-right:0; display:flex; overflow-x:auto; padding:5px; }.nav-item { flex:0 0 42px; margin:0 2px 0 0; }.nav-item i { border:0; }.asof { font-size:9px; }main { padding:11px; }.page-head { align-items:flex-start; }.kpi-strip { grid-template-columns:repeat(2,minmax(0,1fr)); }.compact-stats { grid-template-columns:repeat(2,minmax(0,1fr)); }.clv-block { grid-template-columns:1fr; }.clv-block p { grid-column:auto; } }
    """
