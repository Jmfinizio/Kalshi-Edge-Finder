"""Pure logic (no network, no database): odds math, Kalshi fees, parsing, matching, edge evaluation.

Vocabulary
----------
fair probability : the sportsbook consensus after removing the bookmaker's margin ("de-vig").
contract         : one Kalshi YES/NO market. YES pays $1 if the statement is true.
edge             : fair probability of the side you buy  -  what you pay  -  Kalshi's fee.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timezone
from statistics import NormalDist, pstdev
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .sports import SPORTS, Sport
from .teams import get_teambook

ND = NormalDist()
ET = ZoneInfo("America/New_York")
_SERIES_KIND_RE = re.compile(r"^KX[A-Z0-9]+?(GAME|SPREAD|TOTAL)$")
_KIND_OF_SUFFIX = {"GAME": "ML", "SPREAD": "SP", "TOTAL": "TOT"}


def series_kind(series: str) -> Optional[str]:
    """'KXNBASPREAD' -> 'SP'.  Works for every sport because Kalshi names series KX<LEAGUE><GAME|SPREAD|TOTAL>."""
    m = _SERIES_KIND_RE.match(series)
    return _KIND_OF_SUFFIX[m.group(1)] if m else None


# ───────────────────────────── settings ─────────────────────────────
@dataclass
class Settings:
    min_net_edge: float = 0.02        # $ per contract after fees needed to be called an opportunity
    max_contracts: int = 100          # size used to estimate fees (and an upper bound for the quoted fill)
    min_size: int = 5                 # minimum contracts available at the best price
    max_spread: float = 0.08          # widest bid/ask gap we will trust
    min_price: float = 0.05           # ignore near-certain contracts
    max_price: float = 0.95
    min_books: int = 2                # sportsbooks required for a trustworthy fair price
    book_max_age_secs: int = 2700     # book snapshot older than this is flagged stale (45 min)
    max_dispersion: float = 0.035     # books disagreeing by more than this (std-dev of prob) are flagged
    allow_approx: bool = True         # allow Kalshi strikes that differ slightly from the book line
    approx_max_points: float = 1.0
    devig_method: str = "power"       # "power" or "proportional"
    pinnacle_weight: float = 3.0      # sharpest book gets more say in the consensus
    taker_fee_coef: float = 0.07
    bankroll: float = 1000.0          # paper bankroll for stake sizing
    kelly_fraction: float = 0.25
    max_stake_pct: float = 0.05
    maker_fee_coef: float = 0.0175    # resting-order fee on spread/total series (moneylines: none). Verify on Kalshi's fee page.
    book_interval_secs: int = 1800    # poll books this often once a game is "hot" (kicks off within book_hot_hours)
    book_hot_hours: int = 3
    book_warm_interval_secs: int = 5400   # ...and this often while the next game is within book_window_hours
    book_window_hours: int = 12       # never poll for games further out than this (far-out lines are noise)
    daily_credit_cap: int = 16        # hard cap on Odds API credits per rolling 24h, shared by ALL sports (500/mo free = ~16/day)
    credit_reserve: int = 25          # never spend the last N credits
    kalshi_max_age_secs: int = 90     # pregame: if Kalshi data is older than this the collector has stalled: rows stop being tradable
    # ── Live (in-game) mode. Everything here only applies while Live mode is switched ON.
    live_kalshi_secs: int = 3         # refresh Kalshi this often for games in progress (free)
    live_book_secs: int = 30          # refresh sportsbooks this often (1 credit per call per sport: this is what costs money)
    live_budget: int = 100            # most credits one Live session may spend before it pauses sportsbook polling
    live_prewarm_min: int = 5         # start polling this many minutes before a game starts so the first minutes are covered
    live_min_net_edge: float = 0.03   # in-game prices are volatile: demand a bigger edge than pregame
    live_max_spread: float = 0.06
    live_min_books: int = 2
    live_book_max_age_secs: int = 90  # a sportsbook quote older than this is ignored (suspended / not updating)
    live_kalshi_max_age_secs: int = 15  # Kalshi data older than this is not traded against
    kalshi_interval_secs: int = 20

    def update(self, patch: dict[str, Any]) -> None:
        types = {f.name: f.type for f in fields(self)}
        for k, v in patch.items():
            if k not in types:
                continue
            cur = getattr(self, k)
            if isinstance(cur, bool):
                setattr(self, k, bool(v))
            elif isinstance(cur, int):
                setattr(self, k, int(float(v)))
            elif isinstance(cur, float):
                setattr(self, k, float(v))
            else:
                setattr(self, k, str(v))
        if self.devig_method not in ("power", "proportional"):
            self.devig_method = "power"
        self.live_book_secs = max(10, self.live_book_secs)               # never hammer the sportsbook API faster than every 10 s
        self.live_kalshi_secs = max(1, self.live_kalshi_secs)
        self.live_budget = max(0, self.live_budget)
        self.max_stake_pct = min(max(self.max_stake_pct, 0.0), 1.0)
        self.kelly_fraction = min(max(self.kelly_fraction, 0.0), 1.0)


def live_settings(st: Settings) -> Settings:
    """The thresholds that apply to games in progress (stricter edge, fresher data)."""
    return replace(st, min_net_edge=st.live_min_net_edge, max_spread=st.live_max_spread, min_books=st.live_min_books,
                   book_max_age_secs=st.live_book_max_age_secs + st.live_book_secs)      # + one poll interval of grace


# ───────────────────────────── odds math ─────────────────────────────
def american_to_prob(odds: float) -> float:
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def decimal_to_prob(d: float) -> float:
    return 1.0 / d


def devig_two_way(p1: float, p2: float, method: str = "power") -> tuple[float, float]:
    """Remove the bookmaker margin from a two-outcome market.

    proportional: scale both probabilities by 1/sum.
    power       : find k with p1^k + p2^k = 1 (puts more of the margin on the longshot, which is the
                  usual finding for sharp books).
    """
    s = p1 + p2
    if s <= 0:
        raise ValueError("probabilities must be positive")
    if method == "proportional" or abs(s - 1) < 1e-9:
        return p1 / s, p2 / s
    lo, hi = 0.05, 20.0
    for _ in range(80):
        k = (lo + hi) / 2
        if p1 ** k + p2 ** k > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    a, b = p1 ** k, p2 ** k
    return a / (a + b), b / (a + b)


def kalshi_fee(price: float, qty: int, mult: float = 1.0, coef: float = 0.07) -> float:
    """Taker fee in dollars: ceil_to_cent(mult * coef * qty * P * (1-P))."""
    if qty <= 0:
        return 0.0
    raw = mult * coef * qty * price * (1 - price)
    return math.ceil(round(raw * 100, 6)) / 100


def asks_from_bids(opposite_bids: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Kalshi's order book lists only BIDS. To BUY a side you hit the OTHER side's bids: a NO bid at 56c is a YES ask at 44c.
    Returns [(ask price, contracts)] cheapest first."""
    return sorted((round(1 - p, 4), n) for p, n in opposite_bids if n > 0)


def walk_book(asks: list[tuple[float, float]], qty: float, coef: float = 0.07) -> dict:
    """Price an order of `qty` contracts by walking up the asks, cheapest level first.

    Returns the average price, how much actually filled, the fee (estimated per price level, rounded up to the cent each),
    and the all-in cost per contract. A thin book means the average is worse than the best ask you saw on the board."""
    remaining, fills, cost, fee = float(qty), [], 0.0, 0.0
    for price, size in asks:
        take = min(remaining, size)
        if take <= 0:
            break
        fills.append({"price": price, "qty": take})
        cost += price * take
        fee += kalshi_fee(price, take, coef=coef)
        remaining -= take
    filled = float(qty) - remaining
    return {"requested": float(qty), "filled": filled, "complete": remaining <= 1e-9, "fills": fills,
            "best": asks[0][0] if asks else None, "worst": fills[-1]["price"] if fills else None,
            "avg_price": cost / filled if filled else None, "cost": cost, "fee": fee,
            "all_in_per": (cost + fee) / filled if filled else None, "all_in_total": cost + fee}


def trade_pnl(side: str, price: float, qty: int, fee: float, result: str) -> float:
    won = result.lower() == side.lower()
    return qty * (1 - price) - fee if won else -qty * price - fee


# ───────────────────────────── data classes ─────────────────────────────
@dataclass
class FairPoint:
    p: float                       # fair probability the named outcome happens
    line: Optional[float]          # SP: required winning margin L (team covers if margin > L); TOT: total line
    n_books: int
    dispersion: float
    oldest_update: float           # epoch seconds of the oldest quote used
    books: list[str] = field(default_factory=list)


@dataclass
class BookGame:
    game_id: str
    sport: str
    home: str
    away: str
    commence: datetime
    fair: dict[str, FairPoint] = field(default_factory=dict)   # "ML:TB", "SP:BUF", "TOT:OVER", ...

    @property
    def label(self) -> str:
        return f"{self.away} @ {self.home}"

    @property
    def start(self) -> float:
        return self.commence.timestamp()


@dataclass
class KalshiContract:
    sport: str
    ticker: str
    event_ticker: str
    series: str
    kind: str                       # ML | SP | TOT
    team: Optional[str]
    line: Optional[float]           # SP: margin strike k (YES if margin > k); TOT: total strike (YES if total > k)
    title: str
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    yes_bid_size: Optional[float]
    yes_ask_size: Optional[float]
    volume: Optional[float]
    status: str
    close_time: Optional[str]
    event_teams: Optional[tuple[str, str]]
    event_date: Optional[date]
    event_minutes: Optional[int] = None      # HHMM in the ticker (MLB), as minutes after midnight; used to tell doubleheaders apart


# ───────────────────────────── The Odds API parsing ─────────────────────────────
def _ts(s: Optional[str]) -> float:
    if not s:
        return 0.0
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def build_book_games(events: list[dict], settings: Settings, sport_key: str = "nfl", now: Optional[float] = None,
                     max_quote_age: Optional[float] = None) -> dict[str, BookGame]:
    """Convert raw Odds API v4 /odds JSON (decimal odds) into de-vigged consensus fair probabilities.
    With `now` and `max_quote_age`, any bookmaker quote older than that is ignored (used for in-game odds, where a book that
    has stopped updating is probably suspended and its price means nothing)."""
    games: dict[str, BookGame] = {}
    tb = get_teambook(sport_key)
    for ev in events:
        home, away = tb.resolve(ev.get("home_team", "")), tb.resolve(ev.get("away_team", ""))
        if not home or not away:
            continue
        g = BookGame(ev["id"], sport_key, home, away, datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")))
        ml: list[tuple[str, float, dict[str, float], float]] = []                 # (book, weight, {team: p}, ts)
        sp: list[tuple[str, float, float, dict[str, float], float]] = []          # (book, weight, L_home, {team: p}, ts)
        tot: list[tuple[str, float, float, dict[str, float], float]] = []         # (book, weight, line, {OVER/UNDER: p}, ts)
        for bk in ev.get("bookmakers", []):
            key = bk.get("key", "")
            w = settings.pinnacle_weight if key == "pinnacle" else 1.0
            for mk in bk.get("markets", []):
                ts = _ts(mk.get("last_update") or bk.get("last_update"))
                if max_quote_age is not None and now is not None and now - ts > max_quote_age:
                    continue
                outs = mk.get("outcomes", [])
                if len(outs) != 2 or any(o.get("price") in (None, 0) for o in outs):
                    continue
                probs = [decimal_to_prob(float(o["price"])) for o in outs]
                f1, f2 = devig_two_way(probs[0], probs[1], settings.devig_method)
                if mk["key"] == "h2h":
                    t = [tb.resolve(o["name"]) for o in outs]
                    if None in t or set(t) != {home, away}:
                        continue
                    ml.append((key, w, {t[0]: f1, t[1]: f2}, ts))
                elif mk["key"] == "spreads":
                    t = [tb.resolve(o["name"]) for o in outs]
                    if None in t or set(t) != {home, away} or outs[0].get("point") is None:
                        continue
                    pts = {t[0]: float(outs[0]["point"]), t[1]: float(outs[1]["point"])}
                    sp.append((key, w, -pts[home], {t[0]: f1, t[1]: f2}, ts))      # L = -point, from the home side
                elif mk["key"] == "totals":
                    names = [o["name"].upper() for o in outs]
                    if set(names) != {"OVER", "UNDER"} or outs[0].get("point") is None:
                        continue
                    tot.append((key, w, float(outs[0]["point"]), {names[0]: f1, names[1]: f2}, ts))

        def consensus(rows: list[tuple[float, float, float]]) -> tuple[float, float, float]:
            wsum = sum(w for w, _, _ in rows)
            p = sum(w * pr for w, pr, _ in rows) / wsum
            disp = pstdev([pr for _, pr, _ in rows]) if len(rows) > 1 else 0.0
            return p, disp, min(t for _, _, t in rows)

        if ml:
            for team in (home, away):
                rows = [(w, d[team], ts) for _, w, d, ts in ml]
                p, disp, old = consensus(rows)
                g.fair[f"ML:{team}"] = FairPoint(p, None, len(rows), disp, old, [b for b, *_ in ml])
        if sp:
            groups: dict[float, list] = {}
            for row in sp:
                groups.setdefault(row[2], []).append(row)
            line, grp = max(groups.items(), key=lambda kv: (len(kv[1]), any(r[0] == "pinnacle" for r in kv[1])))
            for team, L in ((home, line), (away, -line)):
                rows = [(w, d[team], ts) for _, w, _, d, ts in grp]
                p, disp, old = consensus(rows)
                g.fair[f"SP:{team}"] = FairPoint(p, L, len(rows), disp, old, [r[0] for r in grp])
        if tot:
            groups = {}
            for row in tot:
                groups.setdefault(row[2], []).append(row)
            line, grp = max(groups.items(), key=lambda kv: (len(kv[1]), any(r[0] == "pinnacle" for r in kv[1])))
            for side in ("OVER", "UNDER"):
                rows = [(w, d[side], ts) for _, w, _, d, ts in grp]
                p, disp, old = consensus(rows)
                g.fair[f"TOT:{side}"] = FairPoint(p, line, len(rows), disp, old, [r[0] for r in grp])
        if g.fair:
            games[g.game_id] = g
    return games


# ───────────────────────────── Kalshi parsing ─────────────────────────────
_EVENT_RE = re.compile(r"^(KX[A-Z0-9]+)-(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z]*)$")
_MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def parse_event_ticker(event_ticker: str, sport_key: str = "nfl") -> tuple[Optional[date], Optional[tuple[str, str]], Optional[int]]:
    """'KXNFLGAME-26SEP20CLETB' -> (date(2026,9,20), ('CLE','TB'), None)
       'KXMLBGAME-26SEP301400PHIATL' -> (date(2026,9,30), ('PHI','ATL'), 840)   [14:00 -> 840 minutes]"""
    m = _EVENT_RE.match(event_ticker)
    if not m:
        return None, None, None
    _, yy, mon, dd, hhmm, code = m.groups()
    d = None
    if mon in _MONTHS:
        try:
            d = date(2000 + int(yy), _MONTHS[mon], int(dd))
        except ValueError:
            d = None
    minutes = int(hhmm[:2]) * 60 + int(hhmm[2:]) if hhmm and int(hhmm[:2]) < 24 and int(hhmm[2:]) < 60 else None
    return d, (get_teambook(sport_key).split_code(code) if code else None), minutes


def _dollars(m: dict, name: str) -> Optional[float]:
    v = m.get(f"{name}_dollars")
    if v not in (None, ""):
        return float(v)
    v = m.get(name)                                     # legacy integer cents
    return float(v) / 100 if v not in (None, "") else None


def _num(m: dict, *names: str) -> Optional[float]:
    for n in names:
        v = m.get(n)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


_NUM_AFTER_OVER = re.compile(r"(?:over|above|more than)\s+(\d+(?:\.\d+)?)", re.I)


def parse_kalshi_market(m: dict, series: str, sport_key: str = "nfl") -> Optional[KalshiContract]:
    kind = series_kind(series)
    if kind is None:
        return None
    tb = get_teambook(sport_key)
    ev_t = m.get("event_ticker", "")
    d, teams, minutes = parse_event_ticker(ev_t, sport_key)
    title = m.get("title") or m.get("yes_sub_title") or ""
    sub = m.get("yes_sub_title") or ""
    team: Optional[str] = None
    line: Optional[float] = None
    if kind in ("ML", "SP"):
        suffix = re.sub(r"[0-9.]+$", "", m["ticker"].rsplit("-", 1)[-1])
        # NEVER fall back to a matchup title ("CLE at TB Winner?"): it names both teams and could pick the wrong one.
        title_team = tb.resolve(title.split(" wins")[0]) if " wins by" in title.lower() else None
        team = tb.resolve(sub.split(" wins")[0]) or title_team or tb.resolve(suffix)
        if team is None:
            return None
        if teams and team not in teams:
            return None                                                   # the named team is not even in this matchup: refuse
    if kind in ("SP", "TOT"):
        line = _num(m, "floor_strike")
        if line is None:
            mm = _NUM_AFTER_OVER.search(sub) or _NUM_AFTER_OVER.search(title)
            line = float(mm.group(1)) if mm else None
        if line is None:
            return None
    yes_bid, yes_ask = _dollars(m, "yes_bid"), _dollars(m, "yes_ask")
    return KalshiContract(
        sport=sport_key, ticker=m["ticker"], event_ticker=ev_t, series=series, kind=kind, team=team, line=line, title=title,
        yes_bid=yes_bid if yes_bid and yes_bid > 0 else None,
        yes_ask=yes_ask if yes_ask and 0 < yes_ask < 1 else None,
        yes_bid_size=_num(m, "yes_bid_size_fp", "yes_bid_size"),
        yes_ask_size=_num(m, "yes_ask_size_fp", "yes_ask_size"),
        volume=_num(m, "volume_fp", "volume"), status=m.get("status", ""), close_time=m.get("close_time"),
        event_teams=teams, event_date=d, event_minutes=minutes)


# ───────────────────────────── matching ─────────────────────────────
def _et_date(dt: datetime) -> date:
    return dt.astimezone(ET).date()


def match_games(contracts: list[KalshiContract], games: dict[str, BookGame]) -> tuple[dict[str, Optional[BookGame]], list[dict]]:
    """Map each contract ticker to a BookGame (or None). Returns (mapping, diagnostics for unmatched).
    Games only ever match contracts of the same sport. If a team plays twice in the window (MLB doubleheaders, or a
    series), the ticker's start time picks the right game."""
    by_team: dict[tuple[str, str], list[BookGame]] = {}
    for g in games.values():
        by_team.setdefault((g.sport, g.home), []).append(g)
        by_team.setdefault((g.sport, g.away), []).append(g)

    def find(c: KalshiContract, team_a: str, team_b: Optional[str]) -> Optional[BookGame]:
        cands = [g for g in by_team.get((c.sport, team_a), []) if team_b is None or {g.home, g.away} == {team_a, team_b}]
        if c.event_date is not None:
            # A ticker with a start time (MLB: daily games, series, doubleheaders) must match the game's date EXACTLY: the day before or
            # after is a DIFFERENT game with different pitchers and odds. Tickers without a time (NFL/NBA/NHL/WNBA) tolerate +-1 day
            # for a UTC-vs-Eastern date slip: those teams do not meet on consecutive days.
            tol = 0 if c.event_minutes is not None else 1
            cands = [g for g in cands if abs((_et_date(g.commence) - c.event_date).days) <= tol]
        if not cands:
            return None

        def rank(g: BookGame):                                          # ...but the exact date always wins, then the start time
            et = g.commence.astimezone(ET)
            days = abs((et.date() - c.event_date).days) if c.event_date else 0
            miss = abs(et.hour * 60 + et.minute - c.event_minutes) if c.event_minutes is not None else 0
            return (days, miss, g.commence)
        return min(cands, key=rank)

    ev_team: dict[str, str] = {}
    for c in contracts:
        if c.team and c.event_ticker not in ev_team:
            ev_team[c.event_ticker] = c.team
    out: dict[str, Optional[BookGame]] = {}
    unmatched: list[dict] = []
    for c in contracts:
        g = None
        if c.event_teams:
            g = find(c, c.event_teams[0], c.event_teams[1])
        elif c.team:
            g = find(c, c.team, None)
        elif c.event_ticker in ev_team:
            g = find(c, ev_team[c.event_ticker], None)
        out[c.ticker] = g
        if g is None:
            unmatched.append({"ticker": c.ticker, "title": c.title, "sport": c.sport, "reason": "no sportsbook game found for these teams/date"})

    # Two Kalshi events (e.g. Tuesday's and Wednesday's game of a series) must never both be priced against ONE sportsbook game.
    claims: dict[tuple, list[KalshiContract]] = {}
    for c in contracts:
        if out[c.ticker] is not None:
            claims.setdefault((out[c.ticker].game_id, c.kind, c.team, c.line), []).append(c)
    for cs in claims.values():
        if len(cs) < 2:
            continue
        g = out[cs[0].ticker]
        et = g.commence.astimezone(ET)

        def fit(c: KalshiContract):
            days = abs((et.date() - c.event_date).days) if c.event_date else 9
            miss = abs(et.hour * 60 + et.minute - c.event_minutes) if c.event_minutes is not None else 0
            return (days, miss)
        best = min(cs, key=fit)
        for c in cs:
            if c is not best:
                out[c.ticker] = None
                unmatched.append({"ticker": c.ticker, "title": c.title, "sport": c.sport,
                                  "reason": f"dated {c.event_date}, but {best.event_ticker} fits the sportsbook's {et:%b %d} game better (this game is probably "
                                            f"in progress or not listed by the books yet)"})
    return out, unmatched


# ───────────────────────────── edge evaluation ─────────────────────────────
def _crosses_key_number(a: float, b: float, keys: tuple) -> bool:
    lo, hi = min(a, b), max(a, b)
    return any(lo < k < hi for k in keys)


def fair_yes_for_contract(c: KalshiContract, g: BookGame, st: Settings) -> tuple[Optional[FairPoint], Optional[float], bool, str]:
    """Return (fair point used, fair probability that YES wins, is_approximation, note)."""
    sp: Sport = SPORTS[c.sport]
    reach = min(st.approx_max_points, sp.approx_max)
    can_bridge = st.allow_approx and sp.approx_ok
    if c.kind == "ML":
        fp = g.fair.get(f"ML:{c.team}")
        return (fp, fp.p, False, "") if fp else (None, None, False, "no moneyline from books")
    if c.kind == "SP":
        fp = g.fair.get(f"SP:{c.team}")
        if not fp:
            return None, None, False, "no spread from books"
        L, k = fp.line, c.line
        if abs(L - k) < 1e-9:
            return fp, fp.p, False, ""
        if not can_bridge:
            why = "" if sp.approx_ok else f" ({sp.name} lines are exact-match only)"
            return fp, None, True, f"book line {L:+g} differs from Kalshi {k:+g}{why}"
        if abs(L - k) > reach or float(L).is_integer() or _crosses_key_number(L, k, sp.key_numbers):
            return fp, None, True, f"book line {L:+g} too far from Kalshi {k:+g}"
        p = ND.cdf(ND.inv_cdf(min(max(fp.p, 1e-4), 1 - 1e-4)) + (L - k) / sp.margin_sigma)
        return fp, p, True, f"approximated from book line {L:+g}"
    fp = g.fair.get("TOT:OVER")
    if not fp:
        return None, None, False, "no total from books"
    L, k = fp.line, c.line
    if abs(L - k) < 1e-9:
        return fp, fp.p, False, ""
    if not can_bridge:
        why = "" if sp.approx_ok else f" ({sp.name} totals are exact-match only)"
        return fp, None, True, f"book total {L:g} differs from Kalshi {k:g}{why}"
    if abs(L - k) > reach or float(L).is_integer():
        return fp, None, True, f"book total {L:g} too far from Kalshi {k:g}"
    p = ND.cdf(ND.inv_cdf(min(max(fp.p, 1e-4), 1 - 1e-4)) + (L - k) / sp.total_sigma)
    return fp, p, True, f"approximated from book total {L:g}"


def contract_label(c: KalshiContract) -> str:
    if c.kind == "ML":
        return f"{c.team} to win"
    if c.kind == "SP":
        return f"{c.team} wins by over {c.line:g}"
    return f"Total over {c.line:g}"


def evaluate(c: KalshiContract, g: BookGame, st: Settings, now: float, live: bool = False, kalshi_age: Optional[float] = None,
             kalshi_ts: Optional[float] = None) -> dict:
    """Score one Kalshi contract against the sportsbook fair price. Always returns a row (so it can be shown on
    the 'gap board'); `tradable` says whether it would pass the execution filters."""
    fp, fair_yes, approx, note = fair_yes_for_contract(c, g, st)
    kickoff = g.commence.timestamp()
    if kalshi_ts is None and kalshi_age is not None:
        kalshi_ts = now - kalshi_age
    row: dict[str, Any] = {
        "sport": c.sport, "ticker": c.ticker, "event_ticker": c.event_ticker, "kind": c.kind, "game": g.label, "label": contract_label(c),
        "team": c.team, "line": c.line, "kickoff": g.commence.isoformat(), "mins_to_kickoff": round((kickoff - now) / 60),
        "yes_bid": c.yes_bid, "yes_ask": c.yes_ask, "volume": c.volume, "approx": approx, "note": note,
        "fair_yes": fair_yes, "n_books": fp.n_books if fp else 0, "dispersion": fp.dispersion if fp else None,
        "book_age_secs": round(now - fp.oldest_update) if fp and fp.oldest_update else None, "books": fp.books if fp else [],
        "book_line": fp.line if fp else None, "live": live, "kalshi_age": None,
        # raw timestamps: every age and every staleness flag is recomputed from these each time rows are READ (see restamp)
        "event_date": c.event_date.isoformat() if c.event_date else None,
        "date_off_days": abs((g.commence.astimezone(ET).date() - c.event_date).days) if c.event_date else 0,
        "kickoff_ts": kickoff, "book_oldest": fp.oldest_update if fp and fp.oldest_update else None, "kalshi_ts": kalshi_ts,
    }
    flags: list[str] = []
    if row["date_off_days"]:
        flags.append("date +/-1 day")                                    # informational: tolerated for tickers without a start time
    start_gap = None
    if c.event_minutes is not None:                                     # MLB tickers embed a start time (assumed Eastern)
        et = g.commence.astimezone(ET)
        start_gap = abs(et.hour * 60 + et.minute - c.event_minutes)
        if start_gap > 180:
            flags.append("start time differs")
    row["start_gap_min"] = start_gap
    mid = (c.yes_bid + c.yes_ask) / 2 if c.yes_bid is not None and c.yes_ask is not None else None
    row["mid"] = mid
    row["spread"] = (c.yes_ask - c.yes_bid) if mid is not None else None
    row["gap"] = (fair_yes - mid) if (fair_yes is not None and mid is not None) else None
    row.update(best_side=None, price=None, qty=None, fee_per=None, fair_side=None, gross_edge=None, net_edge=None, roi=None,
               kelly_qty=None, maker_limit=None, maker_edge=None, all_in=None)
    if fair_yes is None:
        flags.append("no fair price")
    if c.yes_ask is None and c.yes_bid is None:
        flags.append("no quotes")
    if fp:
        if fp.n_books < st.min_books:
            flags.append("few books")
        if fp.dispersion > st.max_dispersion:
            flags.append("books disagree")
    if approx and fair_yes is not None:
        flags.append("approx line")
    if row["spread"] is not None and row["spread"] > st.max_spread:
        flags.append("wide spread")

    if fair_yes is not None:
        options = []
        if c.yes_ask is not None:
            options.append(("YES", c.yes_ask, fair_yes, c.yes_ask_size))
        if c.yes_bid is not None:
            options.append(("NO", 1 - c.yes_bid, 1 - fair_yes, c.yes_bid_size))     # buying NO hits the YES bids
        scored = []
        for side, price, q, size in options:
            qty = max(1, int(min(st.max_contracts, size))) if size else st.max_contracts
            fee_per = kalshi_fee(price, qty, coef=st.taker_fee_coef) / qty
            scored.append((q - price - fee_per, side, price, q, size, qty, fee_per))
        if scored:
            net, side, price, q, size, qty, fee_per = max(scored)
            cost = price + fee_per
            kelly = max(0.0, (q - cost) / (1 - cost)) if cost < 1 else 0.0
            stake = min(st.bankroll * st.kelly_fraction * kelly, st.bankroll * st.max_stake_pct)
            # Resting-bid alternative: sit one cent below the ask (so it cannot cross), but never above the price that still
            # leaves `min_net_edge`. A filled maker order skips the taker fee on moneylines; spreads/totals pay a small one.
            maker = math.floor(min(q - st.min_net_edge, price - 0.01) * 100 + 1e-9) / 100
            maker_fee = st.maker_fee_coef * maker * (1 - maker) if c.kind in ("SP", "TOT") else 0.0
            row.update(best_side=side, price=price, qty=qty, fee_per=fee_per, fair_side=q, gross_edge=q - price,
                       net_edge=net, roi=net / cost, all_in=cost, kelly_qty=int(stake // cost), side_size=size,
                       maker_limit=maker if maker >= 0.01 else None, maker_edge=(q - maker - maker_fee) if maker >= 0.01 else None)
            if size is not None and size < st.min_size:
                flags.append("thin size")
            if not (st.min_price <= price <= st.max_price):
                flags.append("extreme price")
    row["flags"] = flags
    return restamp(row, now, st, live)


AGE_FLAGS = {"started", "past expected length", "stale book", "stale quote"}      # flags that depend on the CLOCK, not on the prices
BLOCKING_FLAGS = {"no fair price", "no quotes", "started", "few books", "stale book", "wide spread", "thin size", "extreme price",
                  "start time differs", "past expected length", "stale quote", "duplicate market"}


def restamp(row: dict, now: float, st: Settings, live: bool = False) -> dict:
    """Recompute everything that depends on the current time: ages, started / past-expected-length, stale book, stale quote,
    and therefore tradable / opportunity.  Called when a row is built AND every time rows are read, so a stalled collector or a
    dead API makes rows go stale on screen instead of freezing at their last 'fresh' look. Pure: returns a new dict."""
    r = dict(row)
    flags = [f for f in row["flags"] if f not in AGE_FLAGS]
    ko = row["kickoff_ts"]
    r["mins_to_kickoff"] = round((ko - now) / 60)
    if live:
        if now - ko > SPORTS[row["sport"]].live_max_hours * 3600:
            flags.append("past expected length")
    elif ko <= now + 120:
        flags.append("started")
    if row.get("book_oldest"):
        r["book_age_secs"] = round(now - row["book_oldest"])
        if r["book_age_secs"] > st.book_max_age_secs:
            flags.append("stale book")
    if row.get("kalshi_ts"):
        r["kalshi_age"] = round(now - row["kalshi_ts"], 1)
        if r["kalshi_age"] > (st.live_kalshi_max_age_secs if live else st.kalshi_max_age_secs):
            flags.append("stale quote")
    r["flags"] = flags
    r["tradable"] = not (set(flags) & BLOCKING_FLAGS) and r["net_edge"] is not None
    r["opportunity"] = bool(r["tradable"] and r["net_edge"] >= st.min_net_edge)
    return r
