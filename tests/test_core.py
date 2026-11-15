import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kalshi_edge.clients import KalshiClient, OddsApiClient, OddsApiError
from kalshi_edge.core import (Settings, american_to_prob, build_book_games, devig_two_way, evaluate, kalshi_fee, match_games,
                              parse_event_ticker, parse_kalshi_market, trade_pnl)
from kalshi_edge.demo import DemoKalshi, DemoOdds, DemoWorld
from kalshi_edge.service import Engine
from kalshi_edge.sports import SPORTS
from kalshi_edge.store import Store
from kalshi_edge.teams import resolve_team, split_matchup_code

NOW = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)


# ── odds math ──
def test_american_to_prob():
    assert american_to_prob(-110) == pytest.approx(0.5238, abs=1e-4)
    assert american_to_prob(+150) == pytest.approx(0.4, abs=1e-9)


@pytest.mark.parametrize("method", ["power", "proportional"])
def test_devig_sums_to_one_and_symmetric(method):
    a, b = devig_two_way(0.5238, 0.5238, method)
    assert a == pytest.approx(0.5) and b == pytest.approx(0.5)
    a, b = devig_two_way(0.70, 0.35, method)
    assert a + b == pytest.approx(1.0) and a > 0.5


def test_power_devig_shades_favorite_less_than_proportional():
    assert devig_two_way(0.70, 0.35, "power")[0] > devig_two_way(0.70, 0.35, "proportional")[0]


# ── fees ──
@pytest.mark.parametrize("price,qty,expected", [(0.5, 100, 1.75), (0.5, 10, 0.18), (0.10, 100, 0.63), (0.10, 10, 0.07), (0.5, 1, 0.02)])
def test_kalshi_fee(price, qty, expected):
    assert kalshi_fee(price, qty) == pytest.approx(expected)


def test_trade_pnl():
    assert trade_pnl("YES", 0.60, 100, 1.68, "yes") == pytest.approx(40 - 1.68)
    assert trade_pnl("YES", 0.60, 100, 1.68, "no") == pytest.approx(-60 - 1.68)
    assert trade_pnl("NO", 0.40, 100, 1.68, "no") == pytest.approx(60 - 1.68)


# ── teams ──
def test_team_resolution():
    assert resolve_team("Tampa Bay") == "TB" and resolve_team("Tampa Bay Buccaneers") == "TB" and resolve_team("TB") == "TB"
    assert resolve_team("Jacksonville") == "JAX" and resolve_team("JAC") == "JAX" and resolve_team("Washington") == "WAS"
    assert resolve_team("Los Angeles Rams") == "LA" and resolve_team("LAR") == "LA" and resolve_team("Los Angeles C") == "LAC"
    assert resolve_team("New York Giants") == "NYG" and resolve_team("NY Jets") == "NYJ" and resolve_team("San Francisco 49ers") == "SF"
    assert resolve_team("Los Angeles") is None and resolve_team("New York") is None       # ambiguous -> refuse
    assert resolve_team("Notre Dame") is None


def test_matchup_code():
    assert split_matchup_code("CLETB") == ("CLE", "TB")
    assert split_matchup_code("LACLV") == ("LAC", "LV")
    assert split_matchup_code("NYGNYJ") == ("NYG", "NYJ")
    assert split_matchup_code("XXYY") is None


def test_event_ticker():
    d, teams, minutes = parse_event_ticker("KXNFLGAME-26SEP20CLETB")
    assert (d.year, d.month, d.day) == (2026, 9, 20) and teams == ("CLE", "TB") and minutes is None


# ── payload builders ──
def odds_event(home="Tampa Bay Buccaneers", away="Cleveland Browns", kick=None, ph=0.60, books=("pinnacle", "draftkings"), age=60):
    kick = kick or NOW + timedelta(hours=5)
    lu = (NOW - timedelta(seconds=age)).isoformat()
    bks = []
    for b in books:
        vig = 1.02 if b == "pinnacle" else 1.05
        bks.append({"key": b, "last_update": lu, "markets": [{"key": "h2h", "last_update": lu, "outcomes": [
            {"name": home, "price": round(1 / (ph * vig), 4)}, {"name": away, "price": round(1 / ((1 - ph) * vig), 4)}]}]})
    return {"id": "g1", "commence_time": kick.isoformat().replace("+00:00", "Z"), "home_team": home, "away_team": away, "bookmakers": bks}


def kalshi_market(team_code="TB", sub="Tampa Bay", bid="0.5400", ask="0.5600", bsize="50.00", asize="80.00", event="KXNFLGAME-26OCT04CLETB", extra=None):
    m = {"ticker": f"{event}-{team_code}", "event_ticker": event, "title": "Cleveland at Tampa Bay Winner?", "yes_sub_title": sub,
         "status": "active", "yes_bid_dollars": bid, "yes_ask_dollars": ask, "yes_bid_size_fp": bsize, "yes_ask_size_fp": asize,
         "volume_fp": "1000.00", "close_time": "2026-10-05T00:00:00Z"}
    m.update(extra or {})
    return m


def setup_eval(ph=0.60, bid="0.5400", ask="0.5600", asize="80.00", bsize="50.00", st=None, **kw):
    st = st or Settings()
    games = build_book_games([odds_event(ph=ph, **kw)], st)
    c = parse_kalshi_market(kalshi_market(bid=bid, ask=ask, asize=asize, bsize=bsize), "KXNFLGAME")
    mapping, unmatched = match_games([c], games)
    return evaluate(c, mapping[c.ticker], st, NOW.timestamp()), unmatched, games


# ── book consensus ──
def test_book_consensus_removes_vig():
    g = build_book_games([odds_event(ph=0.60)], Settings())["g1"]
    assert g.fair["ML:TB"].p == pytest.approx(0.60, abs=0.01)
    assert g.fair["ML:TB"].p + g.fair["ML:CLE"].p == pytest.approx(1.0)
    assert g.fair["ML:TB"].n_books == 2


def test_pinnacle_weighted_more():
    ev = odds_event(ph=0.60)
    ev["bookmakers"][1]["markets"][0]["outcomes"][0]["price"] = round(1 / (0.66 * 1.05), 4)
    ev["bookmakers"][1]["markets"][0]["outcomes"][1]["price"] = round(1 / (0.34 * 1.05), 4)
    p_default = build_book_games([ev], Settings())["g1"].fair["ML:TB"].p
    p_equal = build_book_games([ev], Settings(pinnacle_weight=1.0))["g1"].fair["ML:TB"].p
    assert p_default < p_equal


# ── Kalshi parsing ──
def test_parse_market_dollar_strings_and_legacy_cents():
    c = parse_kalshi_market(kalshi_market(), "KXNFLGAME")
    assert (c.team, c.kind, c.yes_bid, c.yes_ask, c.yes_ask_size) == ("TB", "ML", 0.54, 0.56, 80.0)
    legacy = {"ticker": "KXNFLGAME-26OCT04CLETB-CLE", "event_ticker": "KXNFLGAME-26OCT04CLETB", "title": "x", "yes_sub_title": "Cleveland",
              "status": "active", "yes_bid": 41, "yes_ask": 43}
    c2 = parse_kalshi_market(legacy, "KXNFLGAME")
    assert c2.team == "CLE" and c2.yes_bid == pytest.approx(0.41) and c2.yes_ask == pytest.approx(0.43)


def test_parse_spread_and_total():
    sp = parse_kalshi_market(kalshi_market("TB", "Tampa Bay wins by over 3.5 Points", event="KXNFLSPREAD-26OCT04CLETB",
                                           extra={"ticker": "KXNFLSPREAD-26OCT04CLETB-TB3", "floor_strike": 3.5}), "KXNFLSPREAD")
    assert sp.kind == "SP" and sp.team == "TB" and sp.line == 3.5
    tot = parse_kalshi_market({"ticker": "KXNFLTOTAL-26OCT04CLETB-44", "event_ticker": "KXNFLTOTAL-26OCT04CLETB", "title": "Over 44.5 points scored?",
                               "yes_sub_title": "Over 44.5 points scored", "status": "active", "yes_bid_dollars": "0.48", "yes_ask_dollars": "0.50"}, "KXNFLTOTAL")
    assert tot.kind == "TOT" and tot.line == 44.5 and tot.team is None


def test_unreadable_market_is_skipped():
    assert parse_kalshi_market(kalshi_market("ZZ", "Mystery Team"), "KXNFLGAME") is None


# ── evaluation ──
def test_buy_yes_when_kalshi_underprices():
    r, unmatched, _ = setup_eval(ph=0.60, bid="0.5400", ask="0.5600")
    assert not unmatched
    assert r["best_side"] == "YES" and r["price"] == 0.56
    assert r["net_edge"] == pytest.approx(r["fair_yes"] - 0.56 - kalshi_fee(0.56, 80) / 80)
    assert r["net_edge"] > 0.02 and r["opportunity"]
    assert r["gap"] == pytest.approx(r["fair_yes"] - 0.55)


def test_buy_no_when_kalshi_overprices():
    r, _, _ = setup_eval(ph=0.60, bid="0.6400", ask="0.6600", bsize="30.00")
    assert r["best_side"] == "NO" and r["price"] == pytest.approx(1 - 0.64)
    assert r["net_edge"] == pytest.approx((1 - r["fair_yes"]) - 0.36 - kalshi_fee(0.36, 30) / 30)
    assert r["qty"] == 30


def test_fees_can_erase_small_gap():
    r, *_ = setup_eval(ph=0.60, bid="0.5700", ask="0.5900")
    assert not r["opportunity"]


def test_filters_block_untradeable():
    r, *_ = setup_eval(ph=0.60, bid="0.40", ask="0.56")
    assert "wide spread" in r["flags"] and not r["tradable"]
    r, *_ = setup_eval(ph=0.60, asize="2.00", bsize="2.00")
    assert "thin size" in r["flags"] and not r["opportunity"]
    r, *_ = setup_eval(ph=0.60, books=("pinnacle",))
    assert "few books" in r["flags"] and not r["tradable"]
    r, *_ = setup_eval(ph=0.60, age=4000)
    assert "stale book" in r["flags"] and not r["tradable"]
    r, *_ = setup_eval(ph=0.60, kick=NOW - timedelta(minutes=30))
    assert "started" in r["flags"] and not r["tradable"]


def test_kelly_and_maker_limit_sensible():
    r, *_ = setup_eval(ph=0.65, bid="0.5400", ask="0.5600")
    st = Settings()
    assert r["kelly_qty"] > 0
    assert r["kelly_qty"] * (r["price"] + r["fee_per"]) <= st.bankroll * st.max_stake_pct + 1e-6
    # ask already has edge: the resting-bid alternative sits one cent below the ask so it can never cross the spread
    assert r["maker_limit"] == pytest.approx(0.55) and r["maker_edge"] == pytest.approx(r["fair_side"] - 0.55)   # moneyline: no maker fee
    assert r["maker_edge"] > r["net_edge"]
    r2, *_ = setup_eval(ph=0.61, bid="0.5800", ask="0.6000")
    assert not r2["opportunity"] and r2["kelly_qty"] == 0
    assert r2["maker_limit"] == pytest.approx(0.59) and r2["maker_limit"] < r2["price"]
    assert r2["maker_limit"] <= r2["fair_side"] - st.min_net_edge + 1e-9


def test_approximate_spread_line():
    st = Settings()
    ev = odds_event()
    lu = ev["bookmakers"][0]["last_update"]

    def spread_book(key, home_pt, p_home):
        return {"key": key, "last_update": lu, "markets": [{"key": "spreads", "last_update": lu, "outcomes": [
            {"name": "Tampa Bay Buccaneers", "price": round(1 / (p_home * 1.04), 4), "point": home_pt},
            {"name": "Cleveland Browns", "price": round(1 / ((1 - p_home) * 1.04), 4), "point": -home_pt}]}]}
    ev["bookmakers"] = [spread_book("pinnacle", -4.5, 0.5), spread_book("fanduel", -4.5, 0.5)]
    g = build_book_games([ev], st)["g1"]
    assert g.fair["SP:TB"].line == 4.5 and g.fair["SP:TB"].p == pytest.approx(0.5, abs=0.01)

    def sp(k):
        return parse_kalshi_market(kalshi_market("TB", f"Tampa Bay wins by over {k} Points", event="KXNFLSPREAD-26OCT04CLETB",
                                                 extra={"ticker": f"KXNFLSPREAD-26OCT04CLETB-TB{int(k)}", "floor_strike": k}), "KXNFLSPREAD")
    e = evaluate(sp(4.5), g, st, NOW.timestamp())
    assert not e["approx"] and e["fair_yes"] == pytest.approx(0.5, abs=0.01)
    n = evaluate(sp(5.5), g, st, NOW.timestamp())
    assert n["approx"] and 0.40 < n["fair_yes"] < 0.5
    k = evaluate(sp(2.5), g, st, NOW.timestamp())
    assert k["fair_yes"] is None and "no fair price" in k["flags"]          # would cross key number 3
    assert evaluate(sp(5.5), g, Settings(allow_approx=False), NOW.timestamp())["fair_yes"] is None


def test_matching_by_team_and_date_and_unmatched():
    games = build_book_games([odds_event()], Settings())
    ok = parse_kalshi_market(kalshi_market(), "KXNFLGAME")
    other = parse_kalshi_market(kalshi_market("DAL", "Dallas", event="KXNFLGAME-26OCT04PHIDAL"), "KXNFLGAME")
    wrong_day = parse_kalshi_market(kalshi_market(event="KXNFLGAME-26OCT20CLETB"), "KXNFLGAME")
    mapping, unmatched = match_games([ok, other, wrong_day], games)
    assert mapping[ok.ticker] is not None and mapping[other.ticker] is None and mapping[wrong_day.ticker] is None
    assert len(unmatched) == 2


# ── clients with mocked HTTP ──
def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_odds_client_headers_and_key_scrubbing():
    def handler(req: httpx.Request):
        assert req.url.path.endswith("/sports/americanfootball_nfl/odds")
        assert req.url.params["apiKey"] == "SECRETKEY" and req.url.params["oddsFormat"] == "decimal" and req.url.params["markets"] == "h2h"
        return httpx.Response(200, json=[odds_event()], headers={"x-requests-remaining": "487", "x-requests-used": "13", "x-requests-last": "1"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            return await OddsApiClient("SECRETKEY", h).fetch("americanfootball_nfl")
    ev, meta = run(go())
    assert len(ev) == 1 and meta == {"remaining": 487, "used": 13, "last_cost": 1}

    async def go2():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, text='{"message":"Invalid key SECRETKEY"}'))) as h:
            await OddsApiClient("SECRETKEY", h).fetch("americanfootball_nfl")
    with pytest.raises(OddsApiError) as e:
        run(go2())
    assert "SECRETKEY" not in str(e.value)


def test_kalshi_client_pagination_and_skips():
    pages = {None: {"events": [{"event_ticker": "KXNFLGAME-26OCT04CLETB", "markets": [kalshi_market("TB"), kalshi_market("CLE", "Cleveland")]}], "cursor": "c2"},
             "c2": {"events": [{"event_ticker": "KXNFLGAME-26OCT04PHIDAL", "markets": [
                 kalshi_market("DAL", "Dallas", event="KXNFLGAME-26OCT04PHIDAL"), kalshi_market("ZZ", "Mystery", event="KXNFLGAME-26OCT04PHIDAL")]}], "cursor": ""}}

    def handler(req):
        assert req.url.path.endswith("/events") and req.url.params["with_nested_markets"] == "true"
        return httpx.Response(200, json=pages[req.url.params.get("cursor")])

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            k = KalshiClient(h)
            return await k.fetch_contracts(SPORTS["nfl"]), k.skipped["nfl"]
    cs, skipped = run(go())
    assert [c.team for c in cs] == ["TB", "CLE", "DAL"] and len(skipped) == 1


# ── engine end to end (demo world) ──
def make_engine(sports=("nfl",)):
    sl = [SPORTS[s] for s in sports]
    w = DemoWorld(sl, seed=1)
    return Engine(Settings(), DemoOdds(w), DemoKalshi(w), Store(":memory:"), sl, demo=True, demo_world=w), w


def test_engine_scan_finds_planted_gaps_and_sorts():
    e, _ = make_engine()
    run(e.refresh_book("nfl"))
    run(e.refresh_kalshi())
    assert len(e.rows) == 12 and not e.unmatched
    gaps = [abs(r["gap"]) for r in e.rows]
    assert gaps == sorted(gaps, reverse=True)
    opp = e.opportunities()
    assert opp and all(r["net_edge"] >= e.st.min_net_edge and r["tradable"] for r in opp)


def test_paper_trade_settlement_roundtrip():
    e, w = make_engine()
    run(e.refresh_book("nfl"))
    run(e.refresh_kalshi())
    top = e.opportunities()[0]
    t = e.paper_trade(top["ticker"], top["best_side"], 10)
    assert t["fee"] == kalshi_fee(t["price"], 10)
    assert run(e.settle()) == 0
    w.finish_all()
    assert run(e.settle()) == 1
    perf = e.performance()
    assert perf["settled"] == 1 and perf["open"] == 0
    tr = perf["trades"][0]
    assert tr["pnl"] == pytest.approx(trade_pnl(tr["side"], tr["price"], tr["qty"], tr["fee"], tr["result"]))
    assert tr["sport"] == "nfl"
    with pytest.raises(ValueError):
        e.paper_trade("NOPE", "YES", 1)


def test_credit_reserve_blocks_spending():
    e, _ = make_engine()
    e.demo = False
    e.credits = {"remaining": 10, "used": 490, "last_cost": 1}
    assert run(e.refresh_book("nfl", force=True)) is False and not e.states["nfl"].games and "reserve" in e.errors[0]["msg"]
    assert e.book_target() is None


def test_convergence_metric():
    s = Store(":memory:")
    base = dict(kind="ML", game="A @ B", label="B to win", fair_yes=0.60, yes_bid=0.54, yes_ask=0.56, best_side="YES", price=0.56,
                net_edge=0.03, n_books=3, approx=False, tradable=True, ticker="T1")
    s.add_snapshots([base], 1000.0)
    s.add_snapshots([dict(base, yes_bid=0.58, yes_ask=0.60, net_edge=0.0, tradable=False)], 1000.0 + 3600)
    c = s.convergence(0.02)
    assert c["signals"] == 1 and c["avg_move_cents"] == pytest.approx(4.0) and c["pct_toward_book"] == 100.0
