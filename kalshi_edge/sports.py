"""Sport registry: everything that differs between leagues lives here, so adding one is a data change, not a code change.

Verified against Kalshi/Odds API docs: KXNFLGAME, KXNBAGAME, KXNHLGAME, KXMLBGAME, KXWNBAGAME, KXNFLSPREAD, KXNFLTOTAL,
KXMLBSPREAD, KXMLBTOTAL, KXWNBASPREAD, KXWNBATOTAL.  NOT verified: KXNBASPREAD/TOTAL and KXNHLSPREAD/TOTAL (assumed from the
naming pattern). The app lists any market it cannot read in Diagnostics, so a wrong guess is visible rather than silent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

# comparison kinds -> The Odds API market key
KIND_TO_ODDS_MARKET = {"ML": "h2h", "SP": "spreads", "TOT": "totals"}


@dataclass(frozen=True)
class Sport:
    key: str                          # internal id, lowercase
    name: str                         # display name
    odds_key: str                     # The Odds API sport key
    kalshi_game: str                  # Kalshi moneyline series
    kalshi_spread: Optional[str]
    kalshi_total: Optional[str]
    margin_sigma: float               # std-dev of final margin (only used to bridge nearby spread lines)
    total_sigma: float
    unit: str                         # "points" | "runs" | "goals"
    key_numbers: tuple = ()           # margins with outsized probability mass: never approximate across them
    approx_ok: bool = False           # may a Kalshi strike that differs from the book line be bridged with a normal curve?
    approx_max: float = 1.0           # ...by at most this many units
    live_max_hours: float = 4.0       # a game 'in progress' longer than this is assumed finished

    def series(self, kind: str) -> Optional[str]:
        return {"ML": self.kalshi_game, "SP": self.kalshi_spread, "TOT": self.kalshi_total}.get(kind)

    def kinds_available(self, wanted: Iterable[str]) -> list[str]:
        return [k for k in wanted if self.series(k)]


SPORTS: dict[str, Sport] = {s.key: s for s in [
    Sport("nfl", "NFL", "americanfootball_nfl", "KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", 13.45, 13.0, "points", (3, 7), True, 1.0, 4.5),
    Sport("nba", "NBA", "basketball_nba", "KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", 12.0, 18.0, "points", (), True, 1.5, 3.5),
    Sport("wnba", "WNBA", "basketball_wnba", "KXWNBAGAME", "KXWNBASPREAD", "KXWNBATOTAL", 11.5, 16.0, "points", (), True, 1.5, 3.0),
    # Hockey/baseball are low-scoring and discrete: a normal curve is a poor bridge, so only exact line matches are allowed.
    Sport("nhl", "NHL", "icehockey_nhl", "KXNHLGAME", "KXNHLSPREAD", "KXNHLTOTAL", 2.35, 2.4, "goals", (), False, 1.0, 3.5),
    Sport("mlb", "MLB", "baseball_mlb", "KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL", 4.4, 4.5, "runs", (), False, 1.0, 5.5),
]}


def parse_sports(spec: Optional[str]) -> list[Sport]:
    """'nfl,nhl' -> [Sport, Sport].  Unknown names raise so a typo in .env is loud, not silent."""
    names = [s.strip().lower() for s in (spec or "nfl").split(",") if s.strip()]
    bad = [n for n in names if n not in SPORTS]
    if bad:
        raise ValueError(f"unknown sport(s) {bad}; choose from {sorted(SPORTS)}")
    seen: list[Sport] = []
    for n in names:
        if SPORTS[n] not in seen:
            seen.append(SPORTS[n])
    return seen


def parse_kinds(spec: Optional[str]) -> tuple[str, ...]:
    kinds = tuple(k.strip().upper() for k in (spec or "ML").split(",") if k.strip())
    bad = [k for k in kinds if k not in KIND_TO_ODDS_MARKET]
    if bad:
        raise ValueError(f"unknown comparison kind(s) {bad}; choose from {sorted(KIND_TO_ODDS_MARKET)}")
    return kinds or ("ML",)
