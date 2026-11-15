"""Thin async HTTP clients. Both are read-only: nothing here can place an order."""
from __future__ import annotations

from typing import Optional

import httpx

from .core import KalshiContract, parse_kalshi_market
from .sports import Sport

ODDS_BASE = "https://api.the-odds-api.com/v4"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"   # serves ALL Kalshi markets despite the subdomain name


class OddsApiError(RuntimeError):
    pass


class OddsApiClient:
    """The Odds API v4. Cost per call = markets x regions (or x ceil(bookmakers/10) when `bookmakers` is given).
    One call returns EVERY upcoming game for one sport, so credits scale with sports x markets, not with games."""

    def __init__(self, key: str, http: httpx.AsyncClient, regions: str = "us", bookmakers: Optional[str] = None):
        self.key, self.http, self.regions, self.bookmakers = key, http, regions, bookmakers

    def _scrub(self, text: str) -> str:
        return text.replace(self.key, "***") if self.key else text

    async def fetch(self, odds_key: str, markets: tuple[str, ...] = ("h2h",)) -> tuple[list[dict], dict]:
        params = {"apiKey": self.key, "markets": ",".join(markets), "oddsFormat": "decimal", "dateFormat": "iso"}
        if self.bookmakers:
            params["bookmakers"] = self.bookmakers
        else:
            params["regions"] = self.regions
        try:
            r = await self.http.get(f"{ODDS_BASE}/sports/{odds_key}/odds", params=params, timeout=25)
        except httpx.HTTPError as e:                                  # never leak the API key in error text
            raise OddsApiError(self._scrub(f"network error: {type(e).__name__}: {e}")) from None
        if r.status_code != 200:
            raise OddsApiError(self._scrub(f"HTTP {r.status_code}: {r.text[:200]}"))

        def hdr(name: str) -> Optional[int]:
            v = r.headers.get(name)
            return int(float(v)) if v not in (None, "") else None
        return r.json(), {"remaining": hdr("x-requests-remaining"), "used": hdr("x-requests-used"), "last_cost": hdr("x-requests-last")}


class KalshiClient:
    def __init__(self, http: httpx.AsyncClient, base: str = KALSHI_BASE):
        self.http, self.base = http, base.rstrip("/")
        self.skipped: dict[str, list[dict]] = {}                       # sport -> markets we could not parse (shown in Diagnostics)

    async def fetch_contracts(self, sport: Sport, kinds: tuple[str, ...] = ("ML",)) -> list[KalshiContract]:
        out: list[KalshiContract] = []
        self.skipped[sport.key] = []
        for kind in sport.kinds_available(kinds):
            series = sport.series(kind)
            cursor: Optional[str] = None
            for _ in range(10):                                        # pagination guard
                params = {"series_ticker": series, "status": "open", "with_nested_markets": "true", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                r = await self.http.get(f"{self.base}/events", params=params, timeout=25)
                r.raise_for_status()
                data = r.json()
                for ev in data.get("events", []):
                    for m in ev.get("markets") or []:
                        m.setdefault("event_ticker", ev.get("event_ticker", ""))
                        c = parse_kalshi_market(m, series, sport.key)
                        if c:
                            out.append(c)
                        else:
                            self.skipped[sport.key].append({"ticker": m.get("ticker"), "title": m.get("title") or m.get("yes_sub_title"),
                                                            "sport": sport.key, "reason": "could not read team / strike from this market"})
                cursor = data.get("cursor")
                if not cursor:
                    break
        return out

    async def fetch_orderbook(self, ticker: str) -> dict:
        """Live depth for one market: {'yes_bids': [(price, contracts)], 'no_bids': [...]} (prices in dollars, ascending)."""
        r = await self.http.get(f"{self.base}/markets/{ticker}/orderbook", timeout=20)
        r.raise_for_status()
        j = r.json()
        fp = j.get("orderbook_fp")
        if fp:                                                           # current format: dollar strings
            conv = lambda arr: [(float(p), float(n)) for p, n in (arr or [])]
            return {"yes_bids": conv(fp.get("yes_dollars")), "no_bids": conv(fp.get("no_dollars"))}
        legacy = j.get("orderbook") or {}                                # older format: integer cents
        conv_c = lambda arr: [(float(p) / 100, float(n)) for p, n in (arr or [])]
        return {"yes_bids": conv_c(legacy.get("yes")), "no_bids": conv_c(legacy.get("no"))}

    async def fetch_market(self, ticker: str) -> dict:
        r = await self.http.get(f"{self.base}/markets/{ticker}", timeout=20)
        r.raise_for_status()
        return r.json().get("market", {})
