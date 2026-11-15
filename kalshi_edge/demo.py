"""Synthetic data for DEMO mode (all enabled sports).

Produces payloads in the *same shape* as The Odds API and Kalshi and feeds them through the same parsing code as live
mode, including MLB's time-stamped event tickers. Prices wiggle over time and a few contracts carry planted gaps so the UI
has something to show. THIS IS FAKE DATA: the app shows a banner whenever it is in use.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from .core import ET, KalshiContract, parse_kalshi_market
from .sports import SPORTS, Sport
from .teams import full_name, short_name

# sport -> [(away, home, hours until start, true home win prob, planted gap on the HOME contract)]
GAMES = {
    "nfl": [("CLE", "TB", 5, 0.64, +0.050), ("DET", "BUF", 8, 0.47, -0.060), ("PHI", "DAL", 11, 0.38, +0.012),
            ("KC", "LV", 29, 0.85, -0.030), ("SF", "SEA", 31, 0.55, +0.055), ("GB", "CHI", 34, 0.42, -0.012)],
    "nba": [("BOS", "NYK", 4, 0.41, -0.075), ("LAL", "GSW", 6, 0.58, +0.020), ("DEN", "OKC", 7, 0.44, +0.010)],
    "nhl": [("BOS", "TOR", -1.0, 0.50, +0.060), ("TBL", "NYR", 3, 0.55, +0.080), ("TOR", "MTL", 5, 0.40, -0.015), ("VGK", "COL", 9, 0.52, +0.030)],
    "mlb": [("SF", "COL", -1.5, 0.62, -0.065), ("LAD", "SD", 2, 0.46, -0.085), ("NYY", "BOS", 4, 0.50, +0.010), ("CHC", "CWS", 6, 0.58, +0.028)],
    "wnba": [("NYL", "LVA", 6, 0.62, -0.025)],
}
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
BOOKS = (("pinnacle", 0.02, 0.004), ("draftkings", 0.045, 0.012), ("fanduel", 0.045, 0.012), ("betmgm", 0.05, 0.015))


def _dec(p: float) -> float:
    return round(1 / min(max(p, 0.01), 0.99), 3)


class DemoWorld:
    def __init__(self, sports: list[Sport] | None = None, seed: int = 7):
        self.sports = sports or [SPORTS["nfl"]]
        self.rng = random.Random(seed)
        self.t0 = time.time()
        self.base = datetime.now(timezone.utc)
        self.finished: dict[str, str] = {}          # market ticker -> 'yes' | 'no'

    def _kick(self, hours: float) -> datetime:
        return self.base + timedelta(hours=hours)

    def _noise(self, key: str, amp: float, every: int = 20) -> float:
        bucket = int((time.time() - self.t0) // every)       # prices wiggle once per `every` seconds
        return random.Random(f"{key}{bucket}").uniform(-amp, amp)

    def _event_ticker(self, sport: Sport, away: str, home: str, hrs: float) -> str:
        d = self._kick(hrs)
        et = d.astimezone(ET)
        stamp = f"{et:%y}{MONTHS[et.month - 1]}{et:%d}" + (f"{et:%H%M}" if sport.key == "mlb" else "")     # MLB tickers carry a start time
        return f"{sport.kalshi_game}-{stamp}{away}{home}"

    # ── Odds API shaped ──
    async def fetch_odds(self, odds_key: str):
        events = []
        sport = next((s for s in self.sports if s.odds_key == odds_key), None)
        for i, (away, home, hrs, ph, _) in enumerate(GAMES.get(sport.key, []) if sport else []):
            now = datetime.now(timezone.utc).isoformat()
            books = []
            for b, vig, jitter in BOOKS:
                p = min(max(ph + random.Random(f"{b}{sport.key}{i}").uniform(-jitter, jitter), 0.03), 0.97)
                books.append({"key": b, "title": b, "last_update": now, "markets": [{"key": "h2h", "last_update": now, "outcomes": [
                    {"name": full_name(home, sport.key), "price": _dec(p * (1 + vig))},
                    {"name": full_name(away, sport.key), "price": _dec((1 - p) * (1 + vig))}]}]})
            events.append({"id": f"demo-{sport.key}{i}", "sport_key": odds_key, "commence_time": self._kick(hrs).isoformat(),
                           "home_team": full_name(home, sport.key), "away_team": full_name(away, sport.key), "bookmakers": books})
        return events, {"remaining": 480, "used": 20, "last_cost": 1}

    # ── Kalshi shaped ──
    def _market(self, sport: Sport, ticker: str, event_ticker: str, team: str, away: str, home: str, fair: float, gap: float,
                live: bool = False) -> dict:
        wiggle = self._noise(ticker, 0.025, every=3) if live else self._noise(ticker, 0.006)     # in-game prices jump around far more
        mid = min(max(fair + gap + wiggle, 0.04), 0.96)
        spread = 0.01 + abs(self._noise(ticker + "s", 0.012))
        m = {"ticker": ticker, "event_ticker": event_ticker, "title": f"{away} at {home} Winner?",
             "yes_sub_title": short_name(team, sport.key), "status": "active",
             "yes_bid_dollars": f"{max(0.01, mid - spread / 2):.4f}", "yes_ask_dollars": f"{min(0.99, mid + spread / 2):.4f}",
             "yes_bid_size_fp": f"{40 + int(abs(self._noise(ticker + 'b', 80)))}.00",
             "yes_ask_size_fp": f"{25 + int(abs(self._noise(ticker + 'a', 120)))}.00",
             "volume_fp": f"{1000 + int(abs(self._noise(ticker + 'v', 9000)))}.00", "close_time": self._kick(60).isoformat()}
        if ticker in self.finished:
            m["status"], m["result"] = "finalized", self.finished[ticker]
        return m

    def all_markets(self) -> list[tuple[Sport, dict]]:
        out = []
        for sport in self.sports:
            for away, home, hrs, ph, gap in GAMES.get(sport.key, []):
                ev = self._event_ticker(sport, away, home, hrs)
                live = hrs < 0
                out.append((sport, self._market(sport, f"{ev}-{home}", ev, home, away, home, ph, gap, live)))
                out.append((sport, self._market(sport, f"{ev}-{away}", ev, away, away, home, 1 - ph, -gap * 0.6, live)))
        return out

    async def fetch_contracts(self, sport: Sport, kinds=("ML",)) -> list[KalshiContract]:
        if "ML" not in kinds:
            return []
        res = []
        for s, m in self.all_markets():
            if s.key == sport.key and m["status"] == "active":
                c = parse_kalshi_market(m, sport.kalshi_game, sport.key)
                if c:
                    res.append(c)
        return res

    async def fetch_market(self, ticker: str) -> dict:
        return next((m for _, m in self.all_markets() if m["ticker"] == ticker), {"ticker": ticker, "status": "unknown"})

    async def orderbook(self, ticker: str) -> dict:
        m = next((m for _, m in self.all_markets() if m["ticker"] == ticker), None)
        if m is None:
            raise KeyError(ticker)
        bid, ask = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
        bsz, asz = float(m["yes_bid_size_fp"]), float(m["yes_ask_size_fp"])
        yes = [(round(bid - 0.01 * i, 4), round(bsz * (0.8 ** i) + 5 * i)) for i in range(6)]
        no = [(round((1 - ask) - 0.01 * i, 4), round(asz * (0.8 ** i) + 5 * i)) for i in range(6)]
        return {"yes_bids": sorted(yes), "no_bids": sorted(no)}

    def finish_all(self) -> int:
        """Demo-only: end every game with a random result (weighted by the true probability) so paper trades can settle."""
        n = 0
        for sport in self.sports:
            for away, home, hrs, ph, _ in GAMES.get(sport.key, []):
                home_wins = self.rng.random() < ph
                ev = self._event_ticker(sport, away, home, hrs)
                self.finished[f"{ev}-{home}"] = "yes" if home_wins else "no"
                self.finished[f"{ev}-{away}"] = "no" if home_wins else "yes"
                n += 1
        return n


class DemoOdds:
    def __init__(self, world: DemoWorld):
        self.world = world

    async def fetch(self, odds_key: str, markets=("h2h",)):
        return await self.world.fetch_odds(odds_key)


class DemoKalshi:
    skipped: dict = {}

    def __init__(self, world: DemoWorld):
        self.world = world

    async def fetch_contracts(self, sport: Sport, kinds=("ML",)):
        return await self.world.fetch_contracts(sport, kinds)

    async def fetch_market(self, ticker):
        return await self.world.fetch_market(ticker)

    async def fetch_orderbook(self, ticker):
        return await self.world.orderbook(ticker)
