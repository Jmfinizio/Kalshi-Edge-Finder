"""SQLite persistence: price snapshots (for convergence analysis), paper trades, small key/value state.

Existing databases are migrated in place (columns are only ever ADDED), so upgrading never loses snapshots or trades."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

from .core import trade_pnl

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
  ts REAL, ticker TEXT, kind TEXT, game TEXT, label TEXT, fair_yes REAL, yes_bid REAL, yes_ask REAL,
  best_side TEXT, price REAL, net_edge REAL, n_books INTEGER, approx INTEGER, tradable INTEGER, sport TEXT DEFAULT 'nfl', live INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(ticker, ts);
CREATE TABLE IF NOT EXISTS paper_trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT, label TEXT, game TEXT, side TEXT, price REAL, qty INTEGER,
  fee REAL, fair_side REAL, net_edge REAL, status TEXT, result TEXT, pnl REAL, settled_ts REAL, note TEXT, sport TEXT DEFAULT 'nfl',
  kickoff TEXT, live INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""
# columns added after v1: (table, column, definition)
MIGRATIONS = [("snapshots", "sport", "TEXT DEFAULT 'nfl'"), ("paper_trades", "sport", "TEXT DEFAULT 'nfl'"), ("paper_trades", "kickoff", "TEXT"),
              ("snapshots", "live", "INTEGER DEFAULT 0"), ("paper_trades", "live", "INTEGER DEFAULT 0")]


class Store:
    def __init__(self, path: str = "kalshi_edge.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            for table, col, ddl in MIGRATIONS:
                have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")     # pre-existing rows become 'nfl'
            self.db.commit()

    # ── key/value ──
    def kv_get(self, k: str, default: Any = None) -> Any:
        with self.lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def kv_set(self, k: str, v: Any) -> None:
        with self.lock:
            self.db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
            self.db.commit()

    # ── Odds API credit ledger (rolling 24h) ──
    def record_spend(self, ts: float, cost: int) -> None:
        log = [x for x in self.kv_get("credit_log", []) if x[0] > ts - 86400] + [[ts, cost]]
        self.kv_set("credit_log", log)

    def spent_since(self, since: float) -> int:
        return sum(c for t, c in self.kv_get("credit_log", []) if t > since)

    # ── snapshots ──
    def add_snapshots(self, rows: list[dict], ts: float) -> None:
        data = [(ts, r["ticker"], r["kind"], r["game"], r["label"], r["fair_yes"], r["yes_bid"], r["yes_ask"], r["best_side"],
                 r["price"], r["net_edge"], r["n_books"], int(r["approx"]), int(r["tradable"]), r.get("sport", "nfl"), int(bool(r.get("live"))))
                for r in rows if r["fair_yes"] is not None]
        with self.lock:
            self.db.executemany("INSERT INTO snapshots(ts,ticker,kind,game,label,fair_yes,yes_bid,yes_ask,best_side,price,net_edge,"
                                "n_books,approx,tradable,sport,live) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
            self.db.execute("DELETE FROM snapshots WHERE ts < ?", (ts - 60 * 86400,))
            self.db.commit()

    def history(self, ticker: str, limit: int = 500) -> list[dict]:
        with self.lock:
            rs = self.db.execute("SELECT ts, fair_yes, yes_bid, yes_ask, net_edge FROM snapshots WHERE ticker=? ORDER BY ts DESC LIMIT ?",
                                 (ticker, limit)).fetchall()
        return [dict(r) for r in reversed(rs)]

    def convergence(self, min_edge: float, min_gap_secs: int = 600, sport: Optional[str] = None, live: bool = False) -> dict:
        """Did Kalshi move toward the sportsbook after we flagged an opportunity?

        For each ticker, take the first snapshot where a *tradable* net edge >= min_edge. Compare the mid price of the
        side we would have bought at that moment to its mid in the latest snapshot at least `min_gap_secs` later.
        move > 0 means Kalshi's price drifted toward the book's view (positive closing-line value)."""
        lv = int(bool(live))                                            # pregame and in-game prices are separate regimes: never mix them
        q, args = "SELECT DISTINCT ticker FROM snapshots WHERE tradable=1 AND net_edge>=? AND live=?", [min_edge, lv]
        if sport:
            q, args = q + " AND sport=?", args + [sport]
        with self.lock:
            tickers = [r["ticker"] for r in self.db.execute(q, args)]
        moves, details = [], []
        for t in tickers:
            with self.lock:
                snaps = self.db.execute("SELECT * FROM snapshots WHERE ticker=? AND live=? ORDER BY ts", (t, lv)).fetchall()
            entry = next((s for s in snaps if s["tradable"] and s["net_edge"] is not None and s["net_edge"] >= min_edge), None)
            if not entry or entry["yes_bid"] is None or entry["yes_ask"] is None:
                continue
            later = [s for s in snaps if s["ts"] >= entry["ts"] + min_gap_secs and s["yes_bid"] is not None and s["yes_ask"] is not None]
            if not later:
                continue
            last = later[-1]
            e_mid, l_mid = (entry["yes_bid"] + entry["yes_ask"]) / 2, (last["yes_bid"] + last["yes_ask"]) / 2
            sign = 1 if entry["best_side"] == "YES" else -1
            move = sign * (l_mid - e_mid)
            moves.append(move)
            details.append({"ticker": t, "sport": entry["sport"], "label": entry["label"], "game": entry["game"], "side": entry["best_side"],
                            "entry_edge": entry["net_edge"], "move": move, "hours_later": (last["ts"] - entry["ts"]) / 3600})
        n = len(moves)
        return {"signals": n, "avg_move_cents": 100 * sum(moves) / n if n else None,
                "pct_toward_book": 100 * sum(m > 0 for m in moves) / n if n else None,
                "detail": sorted(details, key=lambda d: -d["entry_edge"])[:50]}

    # ── paper trades ──
    def add_trade(self, row: dict) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO paper_trades(ts,ticker,label,game,side,price,qty,fee,fair_side,net_edge,status,note,sport,kickoff,live) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?)",
                (time.time(), row["ticker"], row["label"], row["game"], row["side"], row["price"], row["qty"], row["fee"],
                 row.get("fair_side"), row.get("net_edge"), row.get("note", ""), row.get("sport", "nfl"), row.get("kickoff"), int(bool(row.get("live")))))
            self.db.commit()
            return cur.lastrowid

    def trades(self, status: Optional[str] = None, sport: Optional[str] = None, live: Optional[bool] = None) -> list[dict]:
        where, args = [], []
        if live is not None:
            where.append("live=?"); args.append(int(bool(live)))
        if status:
            where.append("status=?"); args.append(status)
        if sport:
            where.append("sport=?"); args.append(sport)
        q = "SELECT * FROM paper_trades" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts DESC"
        with self.lock:
            return [dict(r) for r in self.db.execute(q, args)]

    def delete_trade(self, tid: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM paper_trades WHERE id=?", (tid,))
            self.db.commit()

    def settle_trade(self, tid: int, result: str) -> None:
        with self.lock:
            r = self.db.execute("SELECT * FROM paper_trades WHERE id=?", (tid,)).fetchone()
            if not r:
                return
            if result in ("yes", "no"):
                pnl, status = trade_pnl(r["side"], r["price"], r["qty"], r["fee"], result), "settled"
            else:
                pnl, status = 0.0, "void"
            self.db.execute("UPDATE paper_trades SET status=?, result=?, pnl=?, settled_ts=? WHERE id=?",
                            (status, result, pnl, time.time(), tid))
            self.db.commit()
