from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from exchange_scanner.the_odds_api import (
    MATCHBOOK_COMMISSION_RATE,
    ValueSignal,
    effective_decimal_odds,
)


@dataclass(frozen=True)
class PaperTrade:
    id: int
    logged_at: datetime
    sport_key: str
    event_id: str
    event_name: str
    commence_time: datetime
    market_key: str
    outcome_name: str
    target_bookmaker: str
    target_odds: float
    reference_fair_odds: float
    reference_probability: float
    edge: float
    reference_bookmakers: str
    stake: float
    status: str
    closing_checked_at: datetime | None
    closing_target_odds: float | None
    closing_reference_fair_odds: float | None
    closing_edge: float | None
    result: str | None
    profit: float | None
    matchbook_event_id: str | None
    matchbook_market_id: str | None
    matchbook_runner_id: str | None
    liquidity_status: str | None
    available_at_or_above_target: float | None
    best_back_odds: float | None
    best_back_available: float | None
    best_lay_odds: float | None
    best_lay_available: float | None
    back_lay_spread_pct: float | None

    @property
    def target_clv(self) -> float | None:
        if not self.closing_target_odds:
            return None
        return (self.target_odds / self.closing_target_odds) - 1


def init_paper_db(path: Path) -> None:
    with _connect(path) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                market_key TEXT NOT NULL,
                outcome_name TEXT NOT NULL,
                target_bookmaker TEXT NOT NULL,
                target_odds REAL NOT NULL,
                reference_fair_odds REAL NOT NULL,
                reference_probability REAL NOT NULL,
                edge REAL NOT NULL,
                reference_bookmakers TEXT NOT NULL,
                stake REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                closing_checked_at TEXT,
                closing_target_odds REAL,
                closing_reference_fair_odds REAL,
                closing_edge REAL,
                result TEXT,
                profit REAL,
                matchbook_event_id TEXT,
                matchbook_market_id TEXT,
                matchbook_runner_id TEXT,
                liquidity_status TEXT,
                available_at_or_above_target REAL,
                best_back_odds REAL,
                best_back_available REAL,
                best_lay_odds REAL,
                best_lay_available REAL,
                back_lay_spread_pct REAL,
                UNIQUE(event_id, market_key, outcome_name)
            )
            """
        )
        _ensure_columns(
            db,
            {
                "matchbook_event_id": "TEXT",
                "matchbook_market_id": "TEXT",
                "matchbook_runner_id": "TEXT",
                "liquidity_status": "TEXT",
                "available_at_or_above_target": "REAL",
                "best_back_odds": "REAL",
                "best_back_available": "REAL",
                "best_lay_odds": "REAL",
                "best_lay_available": "REAL",
                "back_lay_spread_pct": "REAL",
            },
        )


def log_signals(
    path: Path,
    signals: list[ValueSignal],
    *,
    stake: float,
    logged_at: datetime | None = None,
    liquidity_by_key: dict[tuple[str, str, str, str], dict[str, str]] | None = None,
) -> int:
    init_paper_db(path)
    logged_at = logged_at or datetime.now(timezone.utc)
    inserted = 0
    with _connect(path) as db:
        for signal in signals:
            liquidity = (liquidity_by_key or {}).get(_signal_key(signal), {})
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO paper_trades (
                    logged_at,
                    sport_key,
                    event_id,
                    event_name,
                    commence_time,
                    market_key,
                    outcome_name,
                    target_bookmaker,
                    target_odds,
                    reference_fair_odds,
                    reference_probability,
                    edge,
                    reference_bookmakers,
                    stake,
                    matchbook_event_id,
                    matchbook_market_id,
                    matchbook_runner_id,
                    liquidity_status,
                    available_at_or_above_target,
                    best_back_odds,
                    best_back_available,
                    best_lay_odds,
                    best_lay_available,
                    back_lay_spread_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    logged_at.isoformat(),
                    signal.sport_key,
                    signal.event_id,
                    signal.event_name,
                    signal.commence_time.isoformat(),
                    signal.market_key,
                    signal.outcome_name,
                    signal.target_bookmaker,
                    signal.target_odds,
                    signal.reference_fair_odds,
                    signal.reference_probability,
                    signal.edge,
                    ", ".join(signal.reference_bookmakers),
                    stake,
                    _empty_to_none(liquidity.get("matchbook_event_id")),
                    _empty_to_none(liquidity.get("matchbook_market_id")),
                    _empty_to_none(liquidity.get("matchbook_runner_id")),
                    _empty_to_none(liquidity.get("liquidity_status")),
                    _optional_float(liquidity.get("available_at_or_above_target")),
                    _optional_float(liquidity.get("best_back_odds")),
                    _optional_float(liquidity.get("best_back_available")),
                    _optional_float(liquidity.get("best_lay_odds")),
                    _optional_float(liquidity.get("best_lay_available")),
                    _optional_float(liquidity.get("back_lay_spread_pct")),
                ),
            )
            inserted += cursor.rowcount
    return inserted


def list_trades(path: Path, *, status: str | None = None) -> list[PaperTrade]:
    init_paper_db(path)
    query = "SELECT * FROM paper_trades"
    params: tuple[str, ...] = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY logged_at, commence_time, id"
    with _connect(path) as db:
        return [_trade_from_row(row) for row in db.execute(query, params).fetchall()]


def update_closing_values(
    path: Path,
    signals: list[ValueSignal],
    *,
    checked_at: datetime | None = None,
) -> int:
    init_paper_db(path)
    checked_at = checked_at or datetime.now(timezone.utc)
    by_key = {
        (
            signal.event_id.casefold(),
            signal.market_key.casefold(),
            signal.outcome_name.casefold(),
            signal.target_bookmaker.casefold(),
        ): signal
        for signal in signals
    }
    updated = 0
    with _connect(path) as db:
        open_rows = db.execute(
            "SELECT * FROM paper_trades WHERE status = 'open'"
        ).fetchall()
        for row in open_rows:
            trade = _trade_from_row(row)
            signal = by_key.get(
                (
                    trade.event_id.casefold(),
                    trade.market_key.casefold(),
                    trade.outcome_name.casefold(),
                    trade.target_bookmaker.casefold(),
                )
            )
            if signal is None:
                continue
            closing_effective_odds = effective_decimal_odds(
                trade.target_odds,
                _commission_rate_for_bookmaker(trade.target_bookmaker),
            )
            closing_edge = (closing_effective_odds / signal.reference_fair_odds) - 1
            cursor = db.execute(
                """
                UPDATE paper_trades
                SET
                    closing_checked_at = ?,
                    closing_target_odds = ?,
                    closing_reference_fair_odds = ?,
                    closing_edge = ?
                WHERE id = ?
                """,
                (
                    checked_at.isoformat(),
                    signal.target_odds,
                    signal.reference_fair_odds,
                    closing_edge,
                    trade.id,
                ),
            )
            updated += cursor.rowcount
    return updated


def settle_results(
    path: Path,
    winners: dict[str, str],
) -> int:
    init_paper_db(path)
    settled = 0
    with _connect(path) as db:
        open_rows = db.execute("SELECT * FROM paper_trades WHERE status = 'open'").fetchall()
        for row in open_rows:
            trade = _trade_from_row(row)
            winner = winners.get(trade.event_id)
            if winner is None:
                continue
            won = winner.casefold() == trade.outcome_name.casefold()
            effective_odds = effective_decimal_odds(
                trade.target_odds,
                _commission_rate_for_bookmaker(trade.target_bookmaker),
            )
            profit = trade.stake * (effective_odds - 1) if won else -trade.stake
            cursor = db.execute(
                """
                UPDATE paper_trades
                SET status = 'settled', result = ?, profit = ?
                WHERE id = ?
                """,
                (winner, profit, trade.id),
            )
            settled += cursor.rowcount
    return settled


def paper_summary(
    trades: list[PaperTrade],
    *,
    now: datetime | None = None,
) -> dict[str, float | int]:
    now = now or datetime.now(timezone.utc)
    clv_trades = [
        trade for trade in trades if trade.target_clv is not None and _trade_has_closed(trade, now)
    ]
    edge_trades = [
        trade for trade in trades if trade.closing_edge is not None and _trade_has_closed(trade, now)
    ]
    settled_trades = [trade for trade in trades if trade.status == "settled"]
    beat_close = sum(1 for trade in clv_trades if trade.target_clv and trade.target_clv > 0)
    positive_close_edge = sum(
        1 for trade in edge_trades if trade.closing_edge and trade.closing_edge > 0
    )
    settled_staked = sum(trade.stake for trade in settled_trades)
    settled_profit = sum(trade.profit or 0.0 for trade in settled_trades)
    settled_wins = sum(1 for trade in settled_trades if (trade.profit or 0.0) > 0)
    return {
        "trades": len(trades),
        "open": sum(1 for trade in trades if trade.status == "open"),
        "settled": len(settled_trades),
        "settled_wins": settled_wins,
        "settled_profit": settled_profit,
        "settled_roi": settled_profit / settled_staked if settled_staked else 0.0,
        "closing_checked": len(clv_trades),
        "beat_closing_line": beat_close,
        "beat_closing_line_rate": beat_close / len(clv_trades) if clv_trades else 0.0,
        "average_target_clv": (
            sum(trade.target_clv or 0.0 for trade in clv_trades) / len(clv_trades)
            if clv_trades
            else 0.0
        ),
        "positive_closing_edge": positive_close_edge,
        "positive_closing_edge_rate": (
            positive_close_edge / len(edge_trades) if edge_trades else 0.0
        ),
        "average_closing_edge": (
            sum(trade.closing_edge or 0.0 for trade in edge_trades) / len(edge_trades)
            if edge_trades
            else 0.0
        ),
    }


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def _ensure_columns(db: sqlite3.Connection, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in db.execute("PRAGMA table_info(paper_trades)")}
    for name, column_type in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {column_type}")


def _signal_key(signal: ValueSignal) -> tuple[str, str, str, str]:
    return (
        signal.event_id.casefold(),
        signal.market_key.casefold(),
        signal.outcome_name.casefold(),
        signal.target_bookmaker.casefold(),
    )


def _commission_rate_for_bookmaker(bookmaker: str) -> float:
    if bookmaker.casefold() in {"matchbook", "smarkets", "betfair"}:
        return MATCHBOOK_COMMISSION_RATE
    return 0.0


def _trade_has_closed(trade: PaperTrade, now: datetime) -> bool:
    return trade.status == "settled" or trade.commence_time <= now


def _trade_from_row(row: sqlite3.Row) -> PaperTrade:
    return PaperTrade(
        id=int(row["id"]),
        logged_at=_parse_time(row["logged_at"]),
        sport_key=row["sport_key"],
        event_id=row["event_id"],
        event_name=row["event_name"],
        commence_time=_parse_time(row["commence_time"]),
        market_key=row["market_key"],
        outcome_name=row["outcome_name"],
        target_bookmaker=row["target_bookmaker"],
        target_odds=float(row["target_odds"]),
        reference_fair_odds=float(row["reference_fair_odds"]),
        reference_probability=float(row["reference_probability"]),
        edge=float(row["edge"]),
        reference_bookmakers=row["reference_bookmakers"],
        stake=float(row["stake"]),
        status=row["status"],
        closing_checked_at=_parse_optional_time(row["closing_checked_at"]),
        closing_target_odds=_optional_float(row["closing_target_odds"]),
        closing_reference_fair_odds=_optional_float(row["closing_reference_fair_odds"]),
        closing_edge=_optional_float(row["closing_edge"]),
        result=row["result"],
        profit=_optional_float(row["profit"]),
        matchbook_event_id=row["matchbook_event_id"],
        matchbook_market_id=row["matchbook_market_id"],
        matchbook_runner_id=row["matchbook_runner_id"],
        liquidity_status=row["liquidity_status"],
        available_at_or_above_target=_optional_float(row["available_at_or_above_target"]),
        best_back_odds=_optional_float(row["best_back_odds"]),
        best_back_available=_optional_float(row["best_back_available"]),
        best_lay_odds=_optional_float(row["best_lay_odds"]),
        best_lay_available=_optional_float(row["best_lay_available"]),
        back_lay_spread_pct=_optional_float(row["back_lay_spread_pct"]),
    )


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_optional_time(value: str | None) -> datetime | None:
    return _parse_time(value) if value else None


def _optional_float(value) -> float | None:
    return float(value) if value not in {None, ""} else None


def _empty_to_none(value: str | None) -> str | None:
    return value if value else None
