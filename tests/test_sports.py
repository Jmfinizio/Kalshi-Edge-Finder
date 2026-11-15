"""Tests for everything added with multi-sport support."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from kalshi_edge.core import (BookGame, FairPoint, Settings, build_book_games, evaluate, match_games, parse_event_ticker, parse_kalshi_market,
                              series_kind)
from kalshi_edge.demo import DemoKalshi, DemoOdds, DemoWorld
from kalshi_edge.server import create_app
from kalshi_edge.service import BACKOFF_SECS, IDLE_RECHECK_SECS, Engine
from kalshi_edge.sports import SPORTS, parse_kinds, parse_sports
from kalshi_edge.store import Store
from kalshi_edge.team_tables import EXPECTED_COUNTS, TABLES
from kalshi_edge.teams import full_name, get_teambook, resolve_team, short_name, split_matchup_code

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── registry ──
def test_parse_sports_and_kinds():
    assert [s.key for s in parse_sports("nfl, NHL ,nfl")] == ["nfl", "nhl"]
    assert [s.key for s in parse_sports(None)] == ["nfl"]
    with pytest.raises(ValueError, match="unknown sport"):
        parse_sports("nfl,cricket")
    assert parse_kinds("ml, sp") == ("ML", "SP") and parse_kinds(None) == ("ML",)
    with pytest.raises(ValueError):
        parse_kinds("ML,PROPS")
    assert SPORTS["mlb"].kinds_available(("ML", "SP", "TOT")) == ["ML", "SP", "TOT"]


@pytest.mark.parametrize("series,kind", [("KXNFLGAME", "ML"), ("KXNBASPREAD", "SP"), ("KXMLBTOTAL", "TOT"), ("KXWNBAGAME", "ML"), ("KXNHLGAME", "ML")])
def test_series_kind(series, kind):
    assert series_kind(series) == kind


def test_series_kind_rejects_non_game_series():
    assert series_kind("KXNBAPTS") is None and series_kind("KXNFL") is None and series_kind("FED") is None


# ── team tables ──
@pytest.mark.parametrize("sport", list(TABLES))
def test_every_team_resolves_every_way(sport):
    rows = TABLES[sport]
    assert len({r[0] for r in rows}) == len(rows) == EXPECTED_COUNTS[sport]
    tb = get_teambook(sport)
    for abbr, city, nick, aliases, extras in rows:
        assert tb.resolve(f"{city} {nick}") == abbr
        assert tb.resolve(abbr) == abbr
        assert tb.resolve(short_name(abbr, sport)) == abbr, short_name(abbr, sport)      # how Kalshi labels it
        assert full_name(abbr, sport) == f"{city} {nick}"
        for a in aliases:
            assert tb.abbr(a) == abbr


def test_ambiguity_is_refused_not_guessed():
    assert resolve_team("New York", "mlb") is None and resolve_team("Chicago", "mlb") is None
    assert resolve_team("Los Angeles", "nba") is None and resolve_team("New York", "nhl") is None
    assert resolve_team("Los Angeles", "nhl") == "LAK"               # only one LA team in the NHL: unambiguous
    assert resolve_team("Washington", "nba") == "WAS" and resolve_team("Tampa Bay", "nhl") == "TBL" and resolve_team("Tampa Bay", "mlb") == "TB"


def test_tricky_names():
    assert resolve_team("Montréal Canadiens", "nhl") == "MTL"        # accent folding (The Odds API spells it this way)
    assert resolve_team("Chicago White Sox", "mlb") == "CWS" and resolve_team("Chicago Cubs", "mlb") == "CHC"
    assert resolve_team("Boston Red Sox", "mlb") == "BOS" and resolve_team("Chicago WS", "mlb") == "CWS"
    assert resolve_team("St. Louis Blues", "nhl") == "STL" and resolve_team("Portland Trail Blazers", "nba") == "POR"
    assert resolve_team("LA Clippers", "nba") == "LAC" and resolve_team("Los Angeles Angels", "mlb") == "LAA"
    assert resolve_team("Athletics", "mlb") == "ATH" and resolve_team("Oakland Athletics", "mlb") == "ATH"
    assert resolve_team("Philadelphia 76ers", "nba") == "PHI"
    assert resolve_team("Cleveland", "nfl") == "CLE" and resolve_team("Cleveland", "mlb") == "CLE"


def test_split_codes_per_sport():
    assert split_matchup_code("PHIATL", "mlb") == ("PHI", "ATL") and split_matchup_code("SASNYK", "nba") == ("SAS", "NYK")
    assert split_matchup_code("NYATL", "wnba") == ("NYL", "ATL") and split_matchup_code("TBNYR", "nhl") == ("TBL", "NYR")
    assert split_matchup_code("CLETB") == ("CLE", "TB") and split_matchup_code("ZZZZ", "nba") is None


# ── tickers ──
def test_event_ticker_with_game_time():
    d, teams, minutes = parse_event_ticker("KXMLBGAME-26SEP301400PHIATL", "mlb")
    assert (d.month, d.day) == (9, 30) and teams == ("PHI", "ATL") and minutes == 14 * 60
    d, teams, minutes = parse_event_ticker("KXMLBGAME-26SEP292000BOSNYY", "mlb")
    assert teams == ("BOS", "NYY") and minutes == 20 * 60
    d, teams, minutes = parse_event_ticker("KXNBAGAME-26JUN08SASNYK", "nba")
    assert (d.month, d.day) == (6, 8) and teams == ("SAS", "NYK") and minutes is None
    assert parse_event_ticker("KXNBAGAME", "nba") == (None, None, None)


def mlb_market(team, sub, event="KXMLBGAME-26SEP301400PHIATL", bid="0.48", ask="0.50"):
    return {"ticker": f"{event}-{team}", "event_ticker": event, "title": "Philadelphia at Atlanta Winner?", "yes_sub_title": sub, "status": "active",
            "yes_bid_dollars": bid, "yes_ask_dollars": ask, "yes_bid_size_fp": "50.00", "yes_ask_size_fp": "50.00"}


def test_market_for_team_outside_matchup_is_refused():
    assert parse_kalshi_market(mlb_market("ATL", "Atlanta"), "KXMLBGAME", "mlb").team == "ATL"
    assert parse_kalshi_market(mlb_market("BOS", "Boston"), "KXMLBGAME", "mlb") is None       # Boston is not playing in PHI @ ATL
    assert parse_kalshi_market(mlb_market("NYY", "New York"), "KXMLBGAME", "mlb") is None     # ambiguous AND not in the matchup


# ── matching: doubleheaders and isolation ──
def mlb_event(gid, home, away, start):
    lu = NOW.isoformat()
    return {"id": gid, "commence_time": start.isoformat().replace("+00:00", "Z"), "home_team": home, "away_team": away, "bookmakers": [
        {"key": b, "last_update": lu, "markets": [{"key": "h2h", "last_update": lu, "outcomes": [{"name": home, "price": 1.9}, {"name": away, "price": 2.0}]}]}
        for b in ("pinnacle", "fanduel")]}


def test_doubleheader_matches_by_start_time():
    et = timezone(timedelta(hours=-4))
    g1 = datetime(2026, 9, 30, 13, 10, tzinfo=et).astimezone(timezone.utc)
    g2 = datetime(2026, 9, 30, 19, 10, tzinfo=et).astimezone(timezone.utc)
    games = build_book_games([mlb_event("early", "Atlanta Braves", "Philadelphia Phillies", g1),
                              mlb_event("late", "Atlanta Braves", "Philadelphia Phillies", g2)], Settings(), "mlb")
    early = parse_kalshi_market(mlb_market("ATL", "Atlanta", "KXMLBGAME-26SEP301300PHIATL"), "KXMLBGAME", "mlb")
    late = parse_kalshi_market(mlb_market("ATL", "Atlanta", "KXMLBGAME-26SEP301900PHIATL"), "KXMLBGAME", "mlb")
    mapping, unmatched = match_games([early, late], games)
    assert mapping[early.ticker].game_id == "early" and mapping[late.ticker].game_id == "late" and not unmatched


def test_contract_never_matches_another_sports_game():
    nhl = build_book_games([mlb_event("n1", "Philadelphia Flyers", "Boston Bruins", NOW + timedelta(hours=3))], Settings(), "nhl")
    nba_c = parse_kalshi_market({"ticker": "KXNBAGAME-26OCT07BOSPHI-PHI", "event_ticker": "KXNBAGAME-26OCT07BOSPHI", "title": "x",
                                 "yes_sub_title": "Philadelphia", "status": "active", "yes_bid_dollars": "0.4", "yes_ask_dollars": "0.42"}, "KXNBAGAME", "nba")
    mapping, unmatched = match_games([nba_c], nhl)                    # same city codes (PHI/BOS) in both leagues
    assert mapping[nba_c.ticker] is None and unmatched[0]["sport"] == "nba"


# ── per-sport approximation rules ──
def line_game(sport, home, away, line_home, p_home=0.5):
    fp = lambda p, L: FairPoint(p, L, 3, 0.005, NOW.timestamp(), ["pinnacle", "fanduel", "dk"])
    return BookGame("g", sport, home, away, NOW + timedelta(hours=4),
                    {f"SP:{home}": fp(p_home, line_home), f"SP:{away}": fp(1 - p_home, -line_home)})


def sp_contract(sport, series, team, other, strike, event_code):
    ev = f"{series}-26OCT07{event_code}"
    return parse_kalshi_market({"ticker": f"{ev}-{team}{int(strike)}", "event_ticker": ev, "title": f"{team} wins by over {strike}",
                                "yes_sub_title": f"{team} wins by over {strike} points", "floor_strike": strike, "status": "active",
                                "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42", "yes_bid_size_fp": "50", "yes_ask_size_fp": "50"}, series, sport)


def test_nba_may_bridge_nearby_lines_but_nhl_and_mlb_may_not():
    st = Settings()
    nba_g = line_game("nba", "NYK", "BOS", 4.5)
    near = sp_contract("nba", "KXNBASPREAD", "NYK", "BOS", 5.5, "BOSNYK")
    r = evaluate(near, nba_g, st, NOW.timestamp())
    assert r["approx"] and r["fair_yes"] is not None and r["fair_yes"] < 0.5
    too_far = sp_contract("nba", "KXNBASPREAD", "NYK", "BOS", 2.5, "BOSNYK")            # 2 points away: beyond the NBA's 1.5 reach
    assert evaluate(too_far, nba_g, st, NOW.timestamp())["fair_yes"] is None
    nba_g3 = line_game("nba", "NYK", "BOS", 3.5)
    across_three = sp_contract("nba", "KXNBASPREAD", "NYK", "BOS", 2.5, "BOSNYK")        # crosses 3: fine in the NBA (no key numbers)...
    assert evaluate(across_three, nba_g3, st, NOW.timestamp())["fair_yes"] is not None
    assert evaluate(sp_contract("nfl", "KXNFLSPREAD", "TB", "CLE", 2.5, "CLETB"), line_game("nfl", "TB", "CLE", 3.5), st, NOW.timestamp())["fair_yes"] is None  # ...never in the NFL
    nfl_g = line_game("nfl", "TB", "CLE", 3.5)
    over3 = sp_contract("nfl", "KXNFLSPREAD", "TB", "CLE", 2.5, "CLETB")
    assert evaluate(over3, nfl_g, st, NOW.timestamp())["fair_yes"] is None             # NFL: 3 IS a key number

    nhl_g = line_game("nhl", "NYR", "TBL", 1.5)
    nhl_c = sp_contract("nhl", "KXNHLSPREAD", "NYR", "TBL", 2.5, "TBLNYR")
    r = evaluate(nhl_c, nhl_g, st, NOW.timestamp())
    assert r["fair_yes"] is None and "exact-match only" in r["note"]
    exact = sp_contract("nhl", "KXNHLSPREAD", "NYR", "TBL", 1.5, "TBLNYR")
    assert evaluate(exact, nhl_g, st, NOW.timestamp())["fair_yes"] == pytest.approx(0.5)
    mlb_g = line_game("mlb", "ATL", "PHI", 1.5)
    assert evaluate(sp_contract("mlb", "KXMLBSPREAD", "ATL", "PHI", 2.5, "PHIATL"), mlb_g, st, NOW.timestamp())["fair_yes"] is None


def test_maker_edge_includes_fee_on_spreads_only():
    g = line_game("nba", "NYK", "BOS", 4.5, p_home=0.55)
    c = sp_contract("nba", "KXNBASPREAD", "NYK", "BOS", 4.5, "BOSNYK")
    r = evaluate(c, g, Settings(), NOW.timestamp())
    m = r["maker_limit"]
    assert r["maker_edge"] == pytest.approx(r["fair_side"] - m - 0.0175 * m * (1 - m))


# ── scheduler and credit budget ──
class FakeOdds:
    def __init__(self, cost=1):
        self.calls, self.cost, self.remaining = [], cost, 500

    async def fetch(self, odds_key, markets=("h2h",)):
        self.calls.append(odds_key)
        self.remaining -= self.cost
        return [], {"remaining": self.remaining, "used": 500 - self.remaining, "last_cost": self.cost}


class FakeKalshi:
    skipped = {}

    async def fetch_contracts(self, sport, kinds=("ML",)):
        return []


def clocked_engine(sports=("nfl", "nhl"), **settings):
    clock = [1_800_000_000.0]
    e = Engine(Settings(**settings), FakeOdds(), FakeKalshi(), Store(":memory:"), [SPORTS[s] for s in sports], now_fn=lambda: clock[0])
    return e, clock


def add_game(e, sport, clock, hours_ahead, gid="g"):
    start = datetime.fromtimestamp(clock[0] + hours_ahead * 3600, tz=timezone.utc)
    e.states[sport].games[gid] = BookGame(gid, sport, "AAA", "BBB", start)


def test_unloaded_sports_load_once_then_idle_recheck_not_every_two_minutes():
    e, clock = clocked_engine()
    assert e.book_target() in ("nfl", "nhl")
    run(e.refresh_book("nfl")); run(e.refresh_book("nhl"))                 # both return zero games (off-season)
    assert e.states["nfl"].book_loaded and e.book_target() is None
    clock[0] += BACKOFF_SECS + 60
    assert e.book_target() is None                                          # NOT re-polled after the 2-minute back-off
    clock[0] += IDLE_RECHECK_SECS
    assert e.book_target() in ("nfl", "nhl")                                # but re-checked after 6h to notice a new season


def test_polls_only_when_kickoff_is_near_and_most_urgent_first():
    e, clock = clocked_engine()
    run(e.refresh_book("nfl")); run(e.refresh_book("nhl"))
    add_game(e, "nfl", clock, 30, "far")                                    # 30h away: outside the 12h window
    add_game(e, "nhl", clock, 10, "warm")                                   # 10h: warm
    clock[0] += 4000
    assert e.book_target() is None                                          # warm needs 90 min (5400s) since last poll
    clock[0] += 2000
    assert e.book_target() == "nhl"                                         # nfl is far out -> never polled
    add_game(e, "nfl", clock, 2, "hot")                                     # an NFL game within the hot window now exists
    assert e.book_target() == "nfl"                                         # most urgent (soonest kickoff) wins


def test_hot_games_poll_more_often_than_warm():
    e, clock = clocked_engine(sports=("nfl",))
    run(e.refresh_book("nfl"))
    add_game(e, "nfl", clock, 2, "hot")
    clock[0] += 1700
    assert e.book_target() is None
    clock[0] += 200                                                         # 1900s > 1800s hot interval
    assert e.book_target() == "nfl"


def test_daily_cap_is_a_hard_stop_shared_across_sports_but_manual_refresh_overrides():
    e, clock = clocked_engine(daily_credit_cap=3)
    for _ in range(3):
        assert e.book_target() is not None
        run(e.refresh_book("nfl")); clock[0] += BACKOFF_SECS + 1; e.states["nfl"].book_loaded = False   # keep it perpetually "due"
    assert e.spent_24h() == 3
    assert e.book_target() is None                                          # cap reached: no automatic polling
    assert run(e.refresh_book("nhl")) is False and "daily credit cap" in e.errors[0]["msg"]
    assert run(e.refresh_book("nhl", force=True)) is True                    # a deliberate manual click is allowed
    clock[0] += 86400 + 10
    assert e.spent_24h() == 0 and e.credit_gate(1)[0]                       # rolling window frees credits


def test_reserve_is_never_broken_even_by_manual_refresh():
    e, clock = clocked_engine()
    e.credits = {"remaining": 26, "used": 474, "last_cost": 1}              # reserve is 25: one more call would leave exactly 25
    e.odds.remaining = 26
    assert run(e.refresh_book("nfl", force=True)) is True
    assert run(e.refresh_book("nfl", force=True)) is False and "reserve" in e.errors[0]["msg"]


def test_multi_market_costs_more_credits_and_is_counted():
    e, clock = clocked_engine(sports=("nfl",))
    e.kinds = ("ML", "SP", "TOT")
    e.odds.cost = 3
    assert e.odds_markets(SPORTS["nfl"]) == ("h2h", "spreads", "totals")
    run(e.refresh_book("nfl"))
    assert e.spent_24h() == 3 and e.credits["remaining"] == 497


def test_credit_ledger_is_persistent_and_rolling():
    s = Store(":memory:")
    s.record_spend(1000.0, 2); s.record_spend(2000.0, 1)
    assert s.spent_since(0) == 3 and s.spent_since(1500) == 1
    s.record_spend(1000.0 + 90000, 1)                                       # recording prunes entries older than 24h
    assert s.spent_since(0) == 1


# ── database upgrade (you already have a v1 database with real snapshots/trades) ──
def test_v1_database_upgrades_in_place_without_losing_data(tmp_path):
    p = str(tmp_path / "old.db")
    db = sqlite3.connect(p)
    db.executescript("""
      CREATE TABLE snapshots(ts REAL, ticker TEXT, kind TEXT, game TEXT, label TEXT, fair_yes REAL, yes_bid REAL, yes_ask REAL,
        best_side TEXT, price REAL, net_edge REAL, n_books INTEGER, approx INTEGER, tradable INTEGER);
      CREATE TABLE paper_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT, label TEXT, game TEXT, side TEXT, price REAL,
        qty INTEGER, fee REAL, fair_side REAL, net_edge REAL, status TEXT, result TEXT, pnl REAL, settled_ts REAL, note TEXT);
      CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);
      INSERT INTO snapshots VALUES(1.0,'KXNFLGAME-26OCT18CARPHI-CAR','ML','CAR @ PHI','CAR to win',0.457,0.48,0.50,'NO',0.52,0.005,3,0,1);
      INSERT INTO paper_trades(ts,ticker,label,game,side,price,qty,fee,status,note) VALUES(2.0,'KXNFLGAME-26OCT18CARPHI-CAR','CAR to win','CAR @ PHI','NO',0.52,10,0.18,'open','');
    """)
    db.commit(); db.close()
    for _ in range(2):                                                       # opening twice must be idempotent
        s = Store(p)
        t = s.trades()
        assert len(t) == 1 and t[0]["sport"] == "nfl" and t[0]["price"] == 0.52 and t[0]["status"] == "open"
        assert s.history("KXNFLGAME-26OCT18CARPHI-CAR")[0]["fair_yes"] == 0.457
    s.add_trade({"ticker": "KXNHLGAME-X-NYR", "label": "NYR to win", "game": "TBL @ NYR", "side": "YES", "price": 0.5, "qty": 5, "fee": 0.09, "sport": "nhl"})
    assert [x["sport"] for x in s.trades(sport="nhl")] == ["nhl"] and len(s.trades(sport="nfl")) == 1 and len(s.trades()) == 2


# ── full multi-sport app over HTTP ──
@pytest.fixture()
def multi_client(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO", "1")
    monkeypatch.setenv("SPORTS", "nfl,nba,nhl,mlb,wnba")
    monkeypatch.setenv("DATABASE", str(tmp_path / "m.db"))
    app = create_app(run_loop=False)
    with TestClient(app) as c:
        assert c.post("/api/refresh", json={"source": "both"}).status_code == 200
        yield c


def test_all_five_sports_flow_through_the_api(multi_client):
    c = multi_client
    s = c.get("/api/status").json()
    assert [x["key"] for x in s["sports"]] == ["nfl", "nba", "nhl", "mlb", "wnba"] and s["errors"] == []
    assert all(x["matched"] == x["contracts"] > 0 for x in s["sports"])
    d = c.get("/api/diagnostics").json()
    assert d["unmatched_kalshi"] == [] and d["unmatched_books"] == []
    for sport in ("nfl", "nba", "nhl", "mlb", "wnba"):
        rows = c.get("/api/board", params={"sport": sport}).json()["rows"]
        assert rows and {r["sport"] for r in rows} == {sport}
    allrows = c.get("/api/board").json()["rows"]
    assert {r["sport"] for r in allrows} == {"nfl", "nba", "nhl", "mlb", "wnba"}
    assert c.get("/api/board", params={"sport": "cricket"}).status_code == 404
    mlb = [r for r in allrows if r["sport"] == "mlb"][0]
    assert "KXMLBGAME-" in mlb["ticker"] and any(ch.isdigit() for ch in mlb["event_ticker"].split("-")[1][7:11])      # time-stamped ticker


def test_paper_trade_and_performance_are_tracked_per_sport(multi_client):
    c = multi_client
    assert c.get("/api/opportunities").json()["rows"]                          # demo plants gaps big enough to clear the threshold
    for sport in ("nhl", "mlb"):                                               # trade from the board: independent of price noise
        row = next(r for r in c.get("/api/board", params={"sport": sport}).json()["rows"] if r["best_side"])
        r = c.post("/api/paper-trades", json={"ticker": row["ticker"], "side": row["best_side"], "qty": 10})
        assert r.status_code == 200
    rows = c.get("/api/paper-trades").json()["rows"]
    assert len(rows) == 2 and all(t["kickoff"] for t in rows)                  # every trade remembers WHEN its game starts
    assert [t["sport"] for t in c.get("/api/paper-trades", params={"sport": "mlb"}).json()["rows"]] == ["mlb"]
    assert c.post("/api/demo/finish-games").json()["settled"] == 2
    assert c.get("/api/performance", params={"sport": "nhl"}).json()["settled"] == 1
    assert c.get("/api/performance").json()["settled"] == 2


def test_refresh_one_sport_only(multi_client):
    assert c_status(multi_client.post("/api/refresh", json={"source": "kalshi", "sport": "nhl"})) == 200
    assert multi_client.post("/api/refresh", json={"source": "kalshi", "sport": "bogus"}).status_code == 404


def c_status(r):
    return r.status_code


def test_saved_settings_survive_a_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO", "1")
    monkeypatch.setenv("DATABASE", str(tmp_path / "persist.db"))
    with TestClient(create_app(run_loop=False)) as c:
        assert c.post("/api/settings", json={"min_net_edge": 0.035, "daily_credit_cap": 9}).json()["daily_credit_cap"] == 9
    with TestClient(create_app(run_loop=False)) as c2:                       # brand-new app, same database file
        s = c2.get("/api/settings").json()
        assert s["min_net_edge"] == 0.035 and s["daily_credit_cap"] == 9 and s["min_books"] == 2    # untouched defaults stay defaults


def test_bad_sports_env_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO", "1")
    monkeypatch.setenv("DATABASE", str(tmp_path / "x.db"))
    monkeypatch.setenv("SPORTS", "nfl,curling")
    with pytest.raises(ValueError, match="unknown sport"):
        create_app(run_loop=False)


# ── same teams, several games: the DATE decides which game a Kalshi market belongs to ──
def series(sport, home_name, away_name, starts_et):
    et = timezone(timedelta(hours=-4))
    return build_book_games([mlb_event(f"g{i}", home_name, away_name, datetime(2026, 10, d, h, m, tzinfo=et).astimezone(timezone.utc))
                             for i, (d, h, m) in enumerate(starts_et)], Settings(), sport)


def test_three_game_series_without_times_in_tickers_matches_by_date():
    # NHL/NBA/NFL tickers carry a date but no time. Three games on consecutive days between the same teams.
    games = series("nhl", "New York Rangers", "Tampa Bay Lightning", [(9, 19, 0), (10, 19, 0), (11, 19, 0)])
    def contract(day):
        ev = f"KXNHLGAME-26OCT{day:02d}TBLNYR"
        return parse_kalshi_market({"ticker": f"{ev}-NYR", "event_ticker": ev, "title": "x", "yes_sub_title": "New York R", "status": "active",
                                    "yes_bid_dollars": "0.4", "yes_ask_dollars": "0.42"}, "KXNHLGAME", "nhl")
    cs = [contract(d) for d in (9, 10, 11)]
    mapping, unmatched = match_games(cs, games)
    assert [mapping[c.ticker].game_id for c in cs] == ["g0", "g1", "g2"] and not unmatched      # NOT all three -> the earliest game


def test_exact_date_beats_adjacent_date_even_if_adjacent_is_earlier():
    games = series("nhl", "New York Rangers", "Tampa Bay Lightning", [(10, 19, 0), (11, 19, 0)])
    ev = "KXNHLGAME-26OCT11TBLNYR"
    c = parse_kalshi_market({"ticker": f"{ev}-NYR", "event_ticker": ev, "title": "x", "yes_sub_title": "New York R", "status": "active",
                             "yes_bid_dollars": "0.4", "yes_ask_dollars": "0.42"}, "KXNHLGAME", "nhl")
    assert match_games([c], games)[0][c.ticker].game_id == "g1"


def test_mlb_series_uses_date_then_time_and_flags_a_start_time_mismatch():
    games = series("mlb", "Milwaukee Brewers", "Los Angeles Dodgers", [(9, 19, 10), (10, 19, 10), (10, 13, 10)])
    ev = "KXMLBGAME-26OCT101910LADMIL"                                                 # Oct 10, 7:10 pm
    c = parse_kalshi_market({"ticker": f"{ev}-MIL", "event_ticker": ev, "title": "x", "yes_sub_title": "Milwaukee", "status": "active",
                             "yes_bid_dollars": "0.48", "yes_ask_dollars": "0.50", "yes_bid_size_fp": "50", "yes_ask_size_fp": "50"}, "KXMLBGAME", "mlb")
    g = match_games([c], games)[0][c.ticker]
    assert g.game_id == "g1"                                                            # Oct 10 at 19:10, not Oct 9 and not the 1:10 pm game
    row = evaluate(c, g, Settings(), NOW.timestamp())
    assert row["start_gap_min"] == 0 and "start time differs" not in row["flags"]
    # if the ticker's clock disagrees with the book's by hours (e.g. a different timezone), refuse to trade it
    ev2 = "KXMLBGAME-26OCT102310LADMIL"
    c2 = parse_kalshi_market({"ticker": f"{ev2}-MIL", "event_ticker": ev2, "title": "x", "yes_sub_title": "Milwaukee", "status": "active",
                              "yes_bid_dollars": "0.48", "yes_ask_dollars": "0.50", "yes_bid_size_fp": "50", "yes_ask_size_fp": "50"}, "KXMLBGAME", "mlb")
    r2 = evaluate(c2, g, Settings(), NOW.timestamp())
    assert r2["start_gap_min"] == 240 and "start time differs" in r2["flags"] and not r2["tradable"]


def test_paper_trade_remembers_kickoff_and_old_trades_migrate(tmp_path):
    p = str(tmp_path / "old.db")
    db = sqlite3.connect(p)
    db.executescript("""CREATE TABLE paper_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ticker TEXT, label TEXT, game TEXT, side TEXT,
        price REAL, qty INTEGER, fee REAL, fair_side REAL, net_edge REAL, status TEXT, result TEXT, pnl REAL, settled_ts REAL, note TEXT, sport TEXT DEFAULT 'nfl');
        INSERT INTO paper_trades(ts,ticker,label,game,side,price,qty,fee,status) VALUES(1,'T','x','A @ B','YES',0.5,1,0.02,'open');""")
    db.commit(); db.close()
    s = Store(p)
    assert s.trades()[0]["kickoff"] is None                                             # old trades simply have no start time recorded
    s.add_trade({"ticker": "T2", "label": "y", "game": "C @ D", "side": "NO", "price": 0.4, "qty": 2, "fee": 0.04, "kickoff": "2026-10-11T23:00:00+00:00"})
    assert [t["kickoff"] for t in s.trades()][0] == "2026-10-11T23:00:00+00:00"


# ── regression: the same series shows two days' markets, but the books list only one of the games ──
def lad_game(day, hour=20):
    et = timezone(timedelta(hours=-4))
    return mlb_event(f"g{day}", "Los Angeles Dodgers", "Milwaukee Brewers", datetime(2026, 10, day, hour, 0, tzinfo=et).astimezone(timezone.utc))


def lad_contract(day, hhmm="2000", bid="0.57", ask="0.59"):
    ev = f"KXMLBGAME-26OCT{day}{hhmm}MILLAD"
    return parse_kalshi_market({"ticker": f"{ev}-LAD", "event_ticker": ev, "title": "x", "yes_sub_title": "Los Angeles D", "status": "active",
                                "yes_bid_dollars": bid, "yes_ask_dollars": ask, "yes_bid_size_fp": "50", "yes_ask_size_fp": "50"}, "KXMLBGAME", "mlb")


def test_mlb_market_for_another_day_is_never_priced_against_the_next_days_game():
    games = build_book_games([lad_game(14)], Settings(), "mlb")                 # books list only Wednesday's game
    tue, wed = lad_contract(13), lad_contract(14)
    mapping, unmatched = match_games([tue, wed], games)
    assert mapping[tue.ticker] is None and mapping[wed.ticker].game_id == "g14"          # Tuesday's market is NOT attached to Wednesday's odds
    assert len(unmatched) == 1 and unmatched[0]["ticker"] == tue.ticker and "no sportsbook game" in unmatched[0]["reason"]


def test_late_game_with_the_next_day_in_the_ticker_is_refused_for_mlb_not_guessed():
    games = build_book_games([lad_game(14)], Settings(), "mlb")
    assert match_games([lad_contract(15)], games)[0]["KXMLBGAME-26OCT152000MILLAD-LAD"] is None
    assert match_games([lad_contract(14)], games)[0]["KXMLBGAME-26OCT142000MILLAD-LAD"].game_id == "g14"


def nhl_pair(day):
    ev = f"KXNHLGAME-26OCT{day}TBLNYR"
    return parse_kalshi_market({"ticker": f"{ev}-NYR", "event_ticker": ev, "title": "x", "yes_sub_title": "New York R", "status": "active",
                                "yes_bid_dollars": "0.4", "yes_ask_dollars": "0.42"}, "KXNHLGAME", "nhl")


def test_two_events_claiming_one_game_only_the_best_fit_keeps_it_for_tickers_without_a_time():
    et = timezone(timedelta(hours=-4))
    games = build_book_games([mlb_event("n14", "New York Rangers", "Tampa Bay Lightning", datetime(2026, 10, 14, 19, 0, tzinfo=et).astimezone(timezone.utc))], Settings(), "nhl")
    a, b = nhl_pair(13), nhl_pair(14)                                           # both within the +-1 day tolerance of the Oct 14 game
    mapping, unmatched = match_games([a, b], games)
    assert mapping[b.ticker].game_id == "n14" and mapping[a.ticker] is None
    assert len(unmatched) == 1 and "fits the sportsbook's Oct 14 game better" in unmatched[0]["reason"] and "2026-10-13" in unmatched[0]["reason"]
    mapping2, _ = match_games([b, a], games)                                    # order of arrival must not matter
    assert mapping2[b.ticker].game_id == "n14" and mapping2[a.ticker] is None


def test_single_off_by_one_event_still_matches_but_says_so():
    et = timezone(timedelta(hours=-4))
    games = build_book_games([mlb_event("n14", "New York Rangers", "Tampa Bay Lightning", datetime(2026, 10, 14, 19, 0, tzinfo=et).astimezone(timezone.utc))], Settings(), "nhl")
    c = nhl_pair(13)                                                            # e.g. a Kalshi ticker dated in UTC for a late game
    g = match_games([c], games)[0][c.ticker]
    row = evaluate(c, g, Settings(), NOW.timestamp())
    assert g is not None and row["date_off_days"] == 1 and "date +/-1 day" in row["flags"] and row["event_date"] == "2026-10-13"
    assert "date +/-1 day" not in __import__("kalshi_edge.core", fromlist=["BLOCKING_FLAGS"]).BLOCKING_FLAGS      # informational, not blocking
    exact = evaluate(nhl_pair(14), g, Settings(), NOW.timestamp())
    assert exact["date_off_days"] == 0 and "date +/-1 day" not in exact["flags"]


def test_engine_never_shows_two_rows_for_the_same_bet(monkeypatch):
    from dataclasses import replace
    from kalshi_edge import service
    from kalshi_edge.demo import DemoKalshi, DemoOdds, DemoWorld
    sl = [SPORTS["nfl"]]
    w = DemoWorld(sl, seed=2)
    e = Engine(Settings(), DemoOdds(w), DemoKalshi(w), Store(":memory:"), sl, demo=True, demo_world=w)
    run(e.refresh_book("nfl")); run(e.refresh_kalshi("nfl"))
    assert len({(r["kickoff"], r["kind"], r["team"], r["line"]) for r in e.rows}) == len(e.rows)       # normal case: all unique

    # simulate a matcher bug that lets a second copy of one market through to the same game
    stt = e.states["nfl"]
    original = stt.contracts[0]
    stt.contracts = stt.contracts + [replace(original, ticker=original.ticker + "-X", event_ticker=original.event_ticker + "X")]
    real = service.match_games

    def leaky(contracts, games):
        mapping, unmatched = real(contracts, games)
        mapping[original.ticker + "-X"] = mapping[original.ticker]
        return mapping, unmatched
    monkeypatch.setattr(service, "match_games", leaky)
    e.scan()
    dups = [r for r in e.rows if "duplicate market" in r["flags"]]
    assert {r["ticker"] for r in dups} == {original.ticker, original.ticker + "-X"}
    assert all(not r["tradable"] and not r["opportunity"] for r in dups)                               # blocked in what the API serves...
    assert all(not r["tradable"] for r in stt.rows if "duplicate market" in r["flags"])               # ...and in the stored rows
    assert all("duplicate market" not in r["flags"] for r in e.opportunities())
