"""The Engine: pulls both markets for every enabled sport, compares them, logs snapshots, settles paper trades, and
rations Odds API credits across sports."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from .core import (BookGame, KalshiContract, Settings, asks_from_bids, build_book_games, evaluate, kalshi_fee, live_settings, match_games,
                   restamp, walk_book)
from .sports import KIND_TO_ODDS_MARKET, SPORTS, Sport
from .store import Store

log = logging.getLogger("kalshi_edge")
SNAPSHOT_EVERY_SECS = 60
BACKOFF_SECS = 120                  # after ANY attempt (esp. a failure) wait at least this long before retrying that source
IDLE_RECHECK_SECS = 6 * 3600
LIVE_SNAPSHOT_EVERY_SECS = 10        # in-game price history is kept at this resolution while Live mode is on        # a sport with no upcoming games (off-season / between weeks) is re-checked this often


@dataclass
class SportState:
    sport: Sport
    games: dict[str, BookGame] = field(default_factory=dict)
    contracts: list[KalshiContract] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    unmatched_games: list[dict] = field(default_factory=list)
    book_loaded: bool = False          # True after one SUCCESSFUL fetch, even if the sport returned zero games
    book_ts: float = 0.0
    kalshi_ts: float = 0.0
    book_attempt_ts: float = 0.0
    kalshi_attempt_ts: float = 0.0
    live_games: dict[str, BookGame] = field(default_factory=dict)     # started games, built from fresh in-game quotes only
    live_rows: list[dict] = field(default_factory=list)
    live_book_ts: float = 0.0
    live_book_attempt_ts: float = 0.0
    mid_hist: dict[str, deque] = field(default_factory=dict)          # ticker -> recent (ts, Kalshi mid) for the "moved" columns


@dataclass
class LiveState:
    enabled: bool = False
    since: float = 0.0
    budget: int = 0
    spent: int = 0
    paused_reason: str = ""


class Engine:
    def __init__(self, settings: Settings, odds, kalshi, store: Store, sports: list[Sport], kinds: tuple[str, ...] = ("ML",),
                 demo: bool = False, now_fn: Callable[[], float] = time.time, demo_world=None):
        self.st, self.odds, self.kalshi, self.store, self.demo, self.now = settings, odds, kalshi, store, demo, now_fn
        self.demo_world = demo_world
        self.demo_reason = ""
        self.kinds = kinds
        self.states: dict[str, SportState] = {s.key: SportState(s) for s in sports}
        self.errors: deque = deque(maxlen=40)
        self.last_snapshot = self.last_settle = self.last_live_snapshot = 0.0
        self.live = LiveState()                                  # Live mode always starts OFF: it spends credits, so it must be a deliberate choice
        self.credits: dict[str, Optional[int]] = store.kv_get("odds_credits", {"remaining": None, "used": None, "last_cost": None})

    # ── views ────────────────────────────────────────────────────────
    def _stamp(self, rows: list[dict], live: bool = False) -> list[dict]:
        now = self.now()
        st = live_settings(self.st) if live else self.st
        return [restamp(r, now, st, live) for r in rows]

    @property
    def rows(self) -> list[dict]:
        out = self._stamp([r for s in self.states.values() for r in s.rows])
        return sorted(out, key=lambda r: -(abs(r["gap"]) if r["gap"] is not None else -1))

    def sport_rows(self, sport: Optional[str]) -> list[dict]:
        if not sport:
            return self.rows
        if sport not in self.states:
            return []
        return sorted(self._stamp(self.states[sport].rows), key=lambda r: -(abs(r["gap"]) if r["gap"] is not None else -1))

    def opportunities(self, min_edge: Optional[float] = None, sport: Optional[str] = None) -> list[dict]:
        me = self.st.min_net_edge if min_edge is None else min_edge
        out = [r for r in self.sport_rows(sport) if r["tradable"] and r["net_edge"] is not None and r["net_edge"] >= me]
        return sorted(out, key=lambda r: -r["net_edge"])

    def odds_markets(self, sport: Sport) -> tuple[str, ...]:
        return tuple(KIND_TO_ODDS_MARKET[k] for k in sport.kinds_available(self.kinds)) or ("h2h",)

    def _err(self, where: str, e: Exception) -> None:
        msg = f"{where}: {type(e).__name__}: {e}"
        log.warning(msg)
        self.errors.appendleft({"ts": self.now(), "msg": msg[:300]})

    # ── Odds API credit budget ───────────────────────────────────────
    def spent_24h(self) -> int:
        return self.store.spent_since(self.now() - 86400)

    def credit_gate(self, cost: int, force: bool = False) -> tuple[bool, str]:
        """May we spend `cost` credits right now?  The reserve is never broken; the daily cap can be overridden by a manual refresh."""
        rem = self.credits.get("remaining")
        if not self.demo and rem is not None and rem - cost < self.st.credit_reserve:
            return False, f"credit reserve reached ({rem} left, reserve {self.st.credit_reserve}); not spending more"
        if not self.demo and not force and self.spent_24h() + cost > self.st.daily_credit_cap:
            return False, f"daily credit cap reached ({self.spent_24h()}/{self.st.daily_credit_cap} in 24h)"
        return True, ""

    # ── refresh ──────────────────────────────────────────────────────
    async def refresh_kalshi(self, sport_key: Optional[str] = None) -> bool:
        ok = True
        for key, stt in self.states.items():
            if sport_key and key != sport_key:
                continue
            stt.kalshi_attempt_ts = self.now()
            try:
                stt.contracts = await self.kalshi.fetch_contracts(stt.sport, self.kinds)
                stt.kalshi_ts = self.now()
                self.scan(key)
            except Exception as e:                                     # one sport failing must not block the others
                self._err(f"kalshi refresh [{key}]", e)
                ok = False
        return ok

    async def refresh_book(self, sport_key: str, force: bool = False, live: bool = False) -> bool:
        if self.odds is None or sport_key not in self.states:
            return False
        stt = self.states[sport_key]
        stt.book_attempt_ts = self.now()
        if live:
            stt.live_book_attempt_ts = self.now()
        markets = self.odds_markets(stt.sport)
        allowed, why = self.credit_gate(len(markets), force or live)         # live polling answers to its own session budget, not the daily cap
        if allowed and live and self.live.spent + len(markets) > self.live.budget:
            allowed, why = False, f"live session budget used ({self.live.spent}/{self.live.budget} credits): sportsbook polling paused"
            self.live.paused_reason = why
        if not allowed:
            self._err(f"odds refresh [{sport_key}]", RuntimeError(why))
            return False
        try:
            events, meta = await self.odds.fetch(stt.sport.odds_key, markets)
            now = self.now()
            stt.games = build_book_games(events, self.st, sport_key)
            fresh = build_book_games(events, self.st, sport_key, now=now, max_quote_age=self.st.live_book_max_age_secs)
            stt.live_games = {gid: g for gid, g in fresh.items() if g.start <= now}
            stt.live_book_ts = now
            if live:
                self.live.spent += meta.get("last_cost") or len(markets)
            stt.book_ts, stt.book_loaded = self.now(), True
            self.credits = {**self.credits, **{k: v for k, v in meta.items() if v is not None}}
            self.store.kv_set("odds_credits", self.credits)
            self.store.record_spend(self.now(), meta.get("last_cost") or len(markets))
            self.scan(sport_key)
            return True
        except Exception as e:
            self._err(f"odds refresh [{sport_key}]", e)
            return False

    # ── scheduling ───────────────────────────────────────────────────
    def next_kickoff_in(self, stt: SportState) -> Optional[float]:
        now = self.now()
        ahead = [g.start - now for g in stt.games.values() if g.start > now]
        return min(ahead) if ahead else None

    def book_target(self) -> Optional[str]:
        """Which sport (if any) should spend an Odds API credit right now?

        Free-tier math: 500 credits/month is ~16/day for ALL sports together. So we poll only when it matters:
          - never loaded                      -> once, immediately
          - next game within book_hot_hours   -> every book_interval_secs
          - next game within book_window_hours-> every book_warm_interval_secs
          - nothing within the window         -> do not poll (far-out lines are noise)
          - nothing upcoming at all           -> re-check every 6h to notice a new week/season
        The most urgent sport (soonest kickoff) wins, and the daily cap is a hard stop."""
        if self.odds is None:
            return None
        now, best = self.now(), None
        live_keys = set(self.live_sports()) if self.live.enabled else set()
        for key, stt in self.states.items():
            if key in live_keys or now - stt.book_attempt_ts < BACKOFF_SECS:
                continue                                                      # Live mode owns polling for sports with a game on
            nxt = self.next_kickoff_in(stt)
            if not stt.book_loaded:
                urgency = -1.0
            elif nxt is None:
                if now - stt.book_ts < IDLE_RECHECK_SECS:
                    continue
                urgency = 1e9
            elif nxt > self.st.book_window_hours * 3600:
                continue
            else:
                interval = self.st.book_interval_secs if nxt <= self.st.book_hot_hours * 3600 else self.st.book_warm_interval_secs
                if now - stt.book_ts < interval:
                    continue
                urgency = nxt
            if best is None or urgency < best[0]:
                best = (urgency, key)
        if best and self.credit_gate(len(self.odds_markets(self.states[best[1]].sport)))[0]:
            return best[1]
        return None

    # ── scanning ─────────────────────────────────────────────────────
    def scan(self, sport_key: Optional[str] = None) -> None:
        now = self.now()
        for key, stt in self.states.items():
            if sport_key and key != sport_key:
                continue
            mapping, unmatched = match_games(stt.contracts, stt.games)
            skipped = getattr(self.kalshi, "skipped", None)
            stt.unmatched = unmatched + list(skipped.get(key, []) if isinstance(skipped, dict) else [])
            stt.rows = [evaluate(c, mapping[c.ticker], self.st, now, kalshi_ts=stt.kalshi_ts or None) for c in stt.contracts if mapping.get(c.ticker)]
            seen: dict[tuple, list[dict]] = {}                            # last line of defence: no two rows may describe the same bet
            for r in stt.rows:
                seen.setdefault((r["sport"], r["kickoff"], r["kind"], r["team"], r["line"]), []).append(r)
            for dup in (g for g in seen.values() if len(g) > 1):
                for r in dup:
                    r["flags"] = r["flags"] + ["duplicate market"]
                    log.warning("duplicate market rows for %s %s: %s", r["game"], r["label"], [x["ticker"] for x in dup])
                stt.rows = self._stamp(stt.rows)                         # re-derive tradable/opportunity so stored rows agree with their flags
            matched_games = {mapping[c.ticker].game_id for c in stt.contracts if mapping.get(c.ticker)}
            stt.unmatched_games = [{"sport": key, "game": g.label, "kickoff": g.commence.isoformat()}
                                   for g in stt.games.values() if g.game_id not in matched_games]
            self._scan_live(key, stt, now)
        if now - self.last_snapshot >= SNAPSHOT_EVERY_SECS and self.rows:
            self.store.add_snapshots(self.rows, now)
            self.last_snapshot = now
        live_all = self.live_rows()
        if live_all and now - self.last_live_snapshot >= LIVE_SNAPSHOT_EVERY_SECS:
            self.store.add_snapshots(live_all, now)
            self.last_live_snapshot = now

    def _scan_live(self, key: str, stt: SportState, now: float) -> None:
        """In-game rows: judged against FRESH in-game sportsbook quotes only, with stricter thresholds."""
        if not self.live.enabled:
            stt.live_rows = []
            return
        mapping, _ = match_games(stt.contracts, stt.live_games)
        ls = live_settings(self.st)
        rows = [evaluate(c, mapping[c.ticker], ls, now, live=True, kalshi_ts=stt.kalshi_ts or None) for c in stt.contracts if mapping.get(c.ticker)]
        for r in rows:                                                            # how far has Kalshi's price moved lately?
            h = stt.mid_hist.setdefault(r["ticker"], deque(maxlen=600))
            if r["mid"] is not None and (not h or now - h[-1][0] >= 0.9):
                h.append((now, r["mid"]))
            for label, secs in (("move_30s", 30), ("move_120s", 120)):
                past = next((m for t, m in reversed(h) if t <= now - secs), None)
                r[label] = (r["mid"] - past) if (past is not None and r["mid"] is not None) else None
        stt.live_rows = rows

    def live_rows(self, sport: Optional[str] = None) -> list[dict]:
        rows = self._stamp([r for k, s in self.states.items() if not sport or k == sport for r in s.live_rows], live=True)
        return sorted(rows, key=lambda r: (-(r["net_edge"] if r["net_edge"] is not None else -9), -(abs(r["gap"]) if r["gap"] is not None else 0)))

    # ── live mode ────────────────────────────────────────────────────
    def game_is_live(self, g: BookGame, now: float) -> bool:
        return g.start - self.st.live_prewarm_min * 60 <= now < g.start + SPORTS[g.sport].live_max_hours * 3600

    def live_sports(self) -> list[str]:
        now = self.now()
        return [k for k, s in self.states.items() if any(self.game_is_live(g, now) for g in s.games.values())]

    def set_live(self, enabled: bool, budget: Optional[int] = None) -> LiveState:
        if enabled and not self.live.enabled:
            self.live = LiveState(True, self.now(), int(budget if budget is not None else self.st.live_budget), 0, "")
        elif enabled and budget is not None:
            self.live.budget = int(budget)
            self.live.paused_reason = ""
        elif not enabled:
            self.live = LiveState()
            for s in self.states.values():
                s.live_rows = []
        return self.live

    def live_status(self) -> dict:
        now = self.now()
        per_hour = lambda k: round(3600 / self.st.live_book_secs * len(self.odds_markets(self.states[k].sport)), 1)
        live_keys = self.live_sports()
        sports = []
        for k, s in self.states.items():
            n_live = sum(self.game_is_live(g, now) for g in s.games.values())
            sports.append({"key": k, "name": s.sport.name, "live_games": n_live, "live_rows": len(s.live_rows),
                           "book_age": round(now - s.live_book_ts) if s.live_book_ts else None,
                           "kalshi_age": round(now - s.kalshi_ts) if s.kalshi_ts else None, "credits_per_hour": per_hour(k)})
        return {"enabled": self.live.enabled, "since": self.live.since or None, "budget": self.live.budget or self.st.live_budget,
                "spent": self.live.spent, "paused_reason": self.live.paused_reason, "live_sports": live_keys,
                "est_credits_per_hour": round(sum(per_hour(k) for k in live_keys), 1), "sports": sports, "has_odds_key": self.odds is not None,
                "credits_remaining": self.credits.get("remaining"), "demo": self.demo,
                "settings": {k: getattr(self.st, k) for k in ("live_kalshi_secs", "live_book_secs", "live_budget", "live_min_net_edge",
                                                              "live_max_spread", "live_book_max_age_secs", "live_kalshi_max_age_secs")}}

    @property
    def unmatched(self) -> list[dict]:
        return [u for s in self.states.values() for u in s.unmatched]

    @property
    def unmatched_games(self) -> list[dict]:
        return [u for s in self.states.values() for u in s.unmatched_games]

    # ── paper trading ────────────────────────────────────────────────
    def paper_trade(self, ticker: str, side: str, qty: int, note: str = "") -> dict:
        row = next((r for r in self.live_rows() if r["ticker"] == ticker), None) or next((r for r in self.rows if r["ticker"] == ticker), None)
        if row is None:
            raise ValueError("ticker not in the current scan")
        side = side.upper()
        if side not in ("YES", "NO"):
            raise ValueError("side must be YES or NO")
        yes_bid, yes_ask = row["yes_bid"], row["yes_ask"]
        price = yes_ask if side == "YES" else (1 - yes_bid if yes_bid is not None else None)
        if price is None:
            raise ValueError("no quote on that side")
        qty = max(1, int(qty))
        fair_side = None if row["fair_yes"] is None else (row["fair_yes"] if side == "YES" else 1 - row["fair_yes"])
        fee = kalshi_fee(price, qty, coef=self.st.taker_fee_coef)
        net = None if fair_side is None else fair_side - price - fee / qty
        tid = self.store.add_trade({"ticker": ticker, "label": row["label"], "game": row["game"], "side": side, "price": price, "qty": qty,
                                    "fee": fee, "fair_side": fair_side, "net_edge": net, "note": note, "sport": row["sport"], "kickoff": row["kickoff"],
                                    "live": bool(row.get("live"))})
        return {"id": tid, "price": price, "qty": qty, "fee": fee}

    async def order_quote(self, ticker: str, side: str, qty: int) -> dict:
        """What would buying `qty` contracts cost RIGHT NOW? Pulls the live order book and walks it level by level."""
        row = next((r for r in self.live_rows() if r["ticker"] == ticker), None) or next((r for r in self.rows if r["ticker"] == ticker), None)
        if row is None:
            raise KeyError("ticker not in the current scan")
        side = side.upper()
        if side not in ("YES", "NO") or qty < 1:
            raise ValueError("side must be YES or NO and qty at least 1")
        book = await self.kalshi.fetch_orderbook(ticker)
        asks = asks_from_bids(book["no_bids"] if side == "YES" else book["yes_bids"])
        fill = walk_book(asks, qty, self.st.taker_fee_coef)
        fair = None if row["fair_yes"] is None else (row["fair_yes"] if side == "YES" else 1 - row["fair_yes"])
        board_price = row["yes_ask"] if side == "YES" else (1 - row["yes_bid"] if row["yes_bid"] is not None else None)
        return {"ticker": ticker, "side": side, "qty": qty, "fair_side": fair, "board_price": board_price, **fill,
                "slippage": (fill["avg_price"] - board_price) if fill["avg_price"] is not None and board_price is not None else None,
                "edge_all_in": (fair - fill["all_in_per"]) if fair is not None and fill["all_in_per"] is not None else None,
                "levels": [{"price": p, "qty": n} for p, n in asks[:8]]}

    async def settle(self) -> int:
        n = 0
        for t in self.store.trades("open"):
            try:
                m = await self.kalshi.fetch_market(t["ticker"])
            except Exception as e:
                self._err(f"settle {t['ticker']}", e)
                continue
            result = (m.get("result") or "").lower()
            if result in ("yes", "no"):
                self.store.settle_trade(t["id"], result)
                n += 1
            elif m.get("status") in ("finalized", "settled") and result == "":
                self.store.settle_trade(t["id"], "void")
                n += 1
        self.last_settle = self.now()
        return n

    def performance(self, sport: Optional[str] = None, live: Optional[bool] = None) -> dict:
        trades = self.store.trades(sport=sport, live=live)
        done = [t for t in trades if t["status"] == "settled"]
        pnl = sum(t["pnl"] for t in done)
        staked = sum(t["price"] * t["qty"] + t["fee"] for t in done)
        wins = sum(1 for t in done if t["pnl"] > 0)
        exp = sum((t["net_edge"] or 0) * t["qty"] for t in done)
        return {"open": sum(t["status"] == "open" for t in trades), "settled": len(done), "wins": wins, "losses": len(done) - wins,
                "pnl": pnl, "staked": staked, "roi": pnl / staked if staked else None, "expected_pnl": exp,
                "convergence": self.store.convergence(self.st.live_min_net_edge if live else self.st.min_net_edge, 60 if live else 600, sport=sport, live=bool(live)),
                "live": live, "trades": trades[:200]}

    # ── status ───────────────────────────────────────────────────────
    def status(self) -> dict:
        now = self.now()
        books_ts = [s.book_ts for s in self.states.values() if s.book_ts]
        k_ts = [s.kalshi_ts for s in self.states.values() if s.kalshi_ts]
        sports = []
        for key, s in self.states.items():
            nxt = self.next_kickoff_in(s)
            sports.append({"key": key, "name": s.sport.name, "games": len(s.games), "contracts": len(s.contracts), "matched": len(s.rows),
                           "opportunities": len(self.opportunities(sport=key)), "book_age": round(now - s.book_ts) if s.book_ts else None,
                           "kalshi_age": round(now - s.kalshi_ts) if s.kalshi_ts else None,
                           "next_kickoff_hours": round(nxt / 3600, 1) if nxt is not None else None, "loaded": s.book_loaded,
                           "live_games": sum(self.game_is_live(g, now) for g in s.games.values())})
        live_on = self.live.enabled
        return {"mode": "demo" if self.demo else "live", "now": now, "sports": sports, "kinds": list(self.kinds),
                "book_ts": max(books_ts) if books_ts else None, "kalshi_ts": max(k_ts) if k_ts else None,
                "book_age": round(now - min(books_ts)) if books_ts else None,         # the OLDEST sport's data: the number to worry about
                "kalshi_age": round(now - min(k_ts)) if k_ts else None,
                "credits": {**self.credits, "spent_24h": self.spent_24h(), "daily_cap": self.st.daily_credit_cap}, "live_on": live_on,
                "demo_reason": self.demo_reason, "has_odds_key": self.odds is not None, "games_from_books": sum(len(s.games) for s in self.states.values()),
                "kalshi_contracts": sum(len(s.contracts) for s in self.states.values()), "matched": len(self.rows),
                "opportunities": len(self.opportunities()), "errors": list(self.errors)[:10], "settings": asdict(self.st)}

    # ── background loop ──────────────────────────────────────────────
    async def tick(self) -> float:
        """One scheduling pass (everything the background loop does). Returns how long to sleep before the next pass."""
        now = self.now()
        for _ in range(len(self.states)):                              # drain every sport that is due this tick (cap still enforced)
            target = self.book_target()
            if not target:
                break
            await self.refresh_book(target)
        live_keys = self.live_sports() if self.live.enabled else []
        for key in live_keys:                                          # Live mode: Kalshi every few seconds (free), books on a budget
            stt = self.states[key]
            if now - stt.kalshi_attempt_ts >= self.st.live_kalshi_secs:
                await self.refresh_kalshi(key)
            if not self.live.paused_reason and now - stt.live_book_attempt_ts >= self.st.live_book_secs:
                await self.refresh_book(key, live=True)
        for key, stt in self.states.items():
            if key in live_keys:
                continue
            every = self.st.kalshi_interval_secs if stt.games else 600          # nothing to compare against: check rarely
            if now - stt.kalshi_attempt_ts >= every:
                await self.refresh_kalshi(key)
        if now - self.last_settle >= 180 and self.store.trades("open"):
            await self.settle()
        return 1 if live_keys else 3

    async def run_forever(self) -> None:
        log.info("collector started (%s mode, sports: %s)", "demo" if self.demo else "live", ",".join(self.states))
        while True:
            try:
                pause = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:                                         # absolutely never die
                self._err("loop", e)
                pause = 3
            await asyncio.sleep(pause)
