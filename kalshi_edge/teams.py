"""Per-sport team resolution.

Kalshi, The Odds API and tickers all name teams differently ("Tampa Bay" / "Tampa Bay Buccaneers" / "TB"). Every name is
normalised to the sport's canonical abbreviation. Resolution REFUSES to guess: ambiguous input ("New York", "Los Angeles")
returns None so the market lands in Diagnostics instead of being traded as the wrong team.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Optional

from .team_tables import TABLES

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()      # Montréal -> Montreal
    return re.sub(r"\s+", " ", _NON_ALNUM.sub(" ", s.lower())).strip()


class TeamBook:
    def __init__(self, rows):
        self.abbr_map: dict[str, str] = {}
        self.full: dict[str, str] = {}
        self.nicks: list[tuple[str, str]] = []
        self.city: dict[str, list[tuple[str, str]]] = {}
        self.names: dict[str, str] = {}
        for abbr, city, nick, aliases, extras in rows:
            self.abbr_map[abbr] = abbr
            for a in aliases:
                self.abbr_map[a.upper()] = abbr
            self.names[abbr] = f"{city} {nick}"
            self.full[_norm(f"{city} {nick}")] = abbr
            for e in extras:
                self.full[_norm(e)] = abbr
            self.nicks.append((_norm(nick), abbr))
            self.city.setdefault(_norm(city), []).append((abbr, _norm(nick)))
        self.nicks.sort(key=lambda x: -len(x[0]))                  # "red sox" must be tried before any shorter nickname

    def abbr(self, code: str) -> Optional[str]:
        """Strict: exact abbreviation or alias only (used when splitting ticker codes)."""
        return self.abbr_map.get(code.strip().upper())

    def resolve(self, text: str) -> Optional[str]:
        if not text:
            return None
        hit = self.abbr_map.get(text.strip().upper())
        if hit:
            return hit
        n = _norm(text)
        if n.startswith("ny "):
            n = "new york " + n[3:]
        elif n.startswith("la "):
            n = "los angeles " + n[3:]
        if n in self.full:
            return self.full[n]
        for nick, abbr in self.nicks:                               # nickname anywhere in the text, on word boundaries
            if re.search(rf"\b{re.escape(nick)}\b", n):
                return abbr
        m = re.match(r"^(.+) ([a-z0-9]{1,3})$", n)                  # "los angeles d", "chicago ws": city + initial(s)
        if m and m.group(1) in self.city and len(self.city[m.group(1)]) > 1:
            tok = m.group(2)
            hits = [a for a, nick in self.city[m.group(1)] if nick.startswith(tok) or "".join(w[0] for w in nick.split()) == tok]
            return hits[0] if len(hits) == 1 else None
        for city, pool in self.city.items():                        # bare city is fine only if the league has one team there
            if len(pool) == 1 and re.search(rf"\b{re.escape(city)}\b", n):
                return pool[0][0]
        return None

    def split_code(self, code: str) -> Optional[tuple[str, str]]:
        """'CLETB' -> ('CLE', 'TB').  None unless exactly one valid split exists."""
        code = code.strip().upper()
        found = set()
        for i in (2, 3, 4):
            a, b = self.abbr(code[:i]), self.abbr(code[i:])
            if a and b and a != b:
                found.add((a, b))
        return next(iter(found)) if len(found) == 1 else None


@lru_cache(maxsize=None)
def get_teambook(sport: str = "nfl") -> TeamBook:
    return TeamBook(TABLES[sport])


def resolve_team(text: str, sport: str = "nfl") -> Optional[str]:
    return get_teambook(sport).resolve(text)


def resolve_abbr(code: str, sport: str = "nfl") -> Optional[str]:
    return get_teambook(sport).abbr(code)


def split_matchup_code(code: str, sport: str = "nfl") -> Optional[tuple[str, str]]:
    return get_teambook(sport).split_code(code)


def full_name(abbr: str, sport: str = "nfl") -> str:
    return get_teambook(sport).names.get(abbr, abbr)


def short_name(abbr: str, sport: str = "nfl") -> str:
    """How Kalshi labels a team in a market subtitle: the city, plus an initial when the city has two teams."""
    tb = get_teambook(sport)
    city = full_name(abbr, sport).rsplit(" ", 1)[0] if " " in full_name(abbr, sport) else full_name(abbr, sport)
    for c, pool in tb.city.items():
        if any(a == abbr for a, _ in pool):
            nick = next(n for a, n in pool if a == abbr)
            return (c.title() if len(pool) == 1 else f"{c.title()} {nick[0].upper()}")
    return city
