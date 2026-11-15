"""Live (in-game) mode: freshness rules, budgets, separate bookkeeping, and the polling schedule."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from kalshi_edge.core import Settings, build_book_games, evaluate, live_settings, match_games, parse_kalshi_market
from kalshi_edge.server import create_app
from kalshi_edge.service import Engine
from kalshi_edge.sports import SPORTS
from kalshi_edge.store import Store

T0 = datetime(2026, 10, 10, 23, 0, tzinfo=timezone.utc).timestamp()          # 7:00 pm Eastern


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def event(clock, minutes_ago=30, ph=0.55, quote_age=5, books=("pinnacle", "fanduel")):
    """An NHL game that started `minutes_ago`, quoted by `books` whose quotes are `quote_age` seconds old."""
    start = datetime.fromtimestamp(clock - minutes_ago * 60, tz=timezone.utc)
    lu = datetime.fromtimestamp(clock - quote_age, tz=timezone.utc).isoformat()
    return {"id": "live1", "commence_time": start.isoformat().replace("+00:00", "Z"), "home_team": "New York Rangers", "away_team": "Tampa Bay Lightning",
            "bookmakers": [{"key": b, "last_update": lu, "markets": [{"key": "h2h", "last_update": lu, "outcomes": [
                {"name": "New York Rangers", "price": round(1 / (ph * 1.03), 4)}, {"name": "Tampa Bay Lightning", "price": round(1 / ((1 - ph) * 1.03), 4)}]}]} for b in books]}


def contract(bid, ask, code="NYR"):
    ev = "KXNHLGAME-26OCT10TBLNYR"
    sub = {"NYR": "New York R", "TBL": "Tampa Bay"}[code]
    return parse_kalshi_market({"ticker": f"{ev}-{code}", "event_ticker": ev, "title": "x", "yes_sub_title": sub, "status": "active",
                                "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}", "yes_bid_size_fp": "80", "yes_ask_size_fp": "80"}, "KXNHLGAME", "nhl")


# ── settings and thresholds ──
def test_live_settings_are_clamped_and_stricter():
    st = Settings()
    st.update({"live_book_secs": 2, "live_kalshi_secs": 0, "live_budget": -5})
    assert st.live_book_secs == 10 and st.live_kalshi_secs == 1 and st.live_budget == 0        # never hammer the API
    ls = live_settings(Settings())
    assert ls.min_net_edge == 0.03 > Settings().min_net_edge and ls.max_spread == 0.06 < Settings().max_spread
    assert ls.book_max_age_secs == 90 + 30                                                      # freshness window + one poll interval


def test_old_quotes_are_ignored_when_building_in_game_prices():
    st = Settings()
    ev = event(T0, quote_age=5)
    ev["bookmakers"][1]["markets"][0]["last_update"] = datetime.fromtimestamp(T0 - 300, tz=timezone.utc).isoformat()    # fanduel went quiet 5 min ago
    fresh = build_book_games([ev], st, "nhl", now=T0, max_quote_age=90)["live1"]
    assert fresh.fair["ML:NYR"].n_books == 1 and fresh.fair["ML:NYR"].books == ["pinnacle"]
    assert build_book_games([ev], st, "nhl")["live1"].fair["ML:NYR"].n_books == 2               # pregame path unchanged
    ev["bookmakers"][0]["markets"][0]["last_update"] = datetime.fromtimestamp(T0 - 300, tz=timezone.utc).isoformat()
    assert build_book_games([ev], st, "nhl", now=T0, max_quote_age=90) == {}                    # every book stale: no fair price at all


# ── evaluation ──
def test_live_evaluation_ignores_started_but_enforces_freshness():
    st = Settings()
    g = build_book_games([event(T0)], st, "nhl")["live1"]
    c = contract(0.40, 0.42)
    pre = evaluate(c, g, st, T0)
    assert "started" in pre["flags"] and not pre["tradable"] and not pre["live"]               # pregame logic still blocks a started game
    ls = live_settings(st)
    live = evaluate(c, g, ls, T0 + 5, live=True, kalshi_age=2)
    assert "started" not in live["flags"] and live["live"] is True and live["tradable"]
    assert "stale quote" in evaluate(c, g, ls, T0, live=True, kalshi_age=40)["flags"]            # Kalshi data too old
    assert not evaluate(c, g, ls, T0, live=True, kalshi_age=40)["tradable"]
    late = evaluate(c, g, ls, T0 + 6 * 3600, live=True, kalshi_age=1)                           # 6.5 h after the start: a hockey game is over
    assert "past expected length" in late["flags"] and not late["tradable"]
    assert "stale book" in evaluate(c, g, ls, T0 + 400, live=True, kalshi_age=1)["flags"]       # book quote now 405 s old


def test_live_demands_a_bigger_edge_than_pregame():
    st = Settings()
    g = build_book_games([event(T0, ph=0.55)], st, "nhl")["live1"]                              # fair ~55%
    c = contract(0.50, 0.51)                                                                    # ~2.2c of net edge after fees
    assert evaluate(c, g, st, T0 - 7200)["net_edge"] > 0.02                                     # clears the pregame bar (2c)
    ls = live_settings(st)
    row = evaluate(c, g, ls, T0, live=True, kalshi_age=1)
    assert 0.02 < row["net_edge"] < 0.03 and row["tradable"] and not row["opportunity"]        # a pregame-grade edge is NOT enough in-game


# ── engine ──
class LiveOdds:
    def __init__(self, clock):
        self.clock, self.calls, self.cost, self.remaining, self.quote_age = clock, [], 1, 500, 5

    async def fetch(self, odds_key, markets=("h2h",)):
        self.calls.append(self.clock[0])
        self.remaining -= self.cost
        return [event(self.clock[0], quote_age=self.quote_age)], {"remaining": self.remaining, "used": 500 - self.remaining, "last_cost": self.cost}


class LiveKalshi:
    skipped = {}

    def __init__(self):
        self.calls, self.bid, self.ask = 0, 0.50, 0.52

    async def fetch_contracts(self, sport, kinds=("ML",)):
        self.calls += 1
        return [contract(self.bid, self.ask, "NYR"), contract(1 - self.ask, 1 - self.bid, "TBL")]

    async def fetch_market(self, ticker):
        return {"status": "active"}


def live_engine(**kw):
    clock = [T0]
    e = Engine(Settings(**kw), LiveOdds(clock), LiveKalshi(), Store(":memory:"), [SPORTS["nhl"]], now_fn=lambda: clock[0])
    return e, clock


def test_live_mode_starts_off_and_costs_nothing_until_enabled():
    e, clock = live_engine()
    run(e.refresh_book("nhl")); run(e.refresh_kalshi("nhl"))                                    # a normal pregame-style refresh of an in-play game
    assert not e.live.enabled and e.live_rows() == [] and e.live_status()["enabled"] is False
    assert e.live.spent == 0 and e.status()["live_on"] is False


def test_live_polling_schedule_and_budget():
    e, clock = live_engine(live_budget=3)
    run(e.refresh_book("nhl")); run(e.refresh_kalshi("nhl"))
    e.set_live(True)
    base_book, base_k = len(e.odds.calls), e.kalshi.calls
    clock[0] += 4                                                                               # (Kalshi was fetched this very second: wait out its 3 s interval)
    run(e.tick())                                                                               # first live tick polls both
    assert len(e.odds.calls) == base_book + 1 and e.kalshi.calls == base_k + 1 and e.live.spent == 1
    run(e.tick())                                                                               # immediately again: nothing is due
    assert len(e.odds.calls) == base_book + 1 and e.kalshi.calls == base_k + 1
    clock[0] += 4; run(e.tick())                                                                # 4 s later: Kalshi only (free), not the books
    assert e.kalshi.calls == base_k + 2 and len(e.odds.calls) == base_book + 1
    clock[0] += 31; run(e.tick())                                                               # 31 s: books again
    assert len(e.odds.calls) == base_book + 2 and e.live.spent == 2
    clock[0] += 31; run(e.tick())
    assert e.live.spent == 3
    clock[0] += 31; run(e.tick())                                                               # budget of 3 is gone: books pause, Kalshi keeps going
    assert len(e.odds.calls) == base_book + 3 and "budget used" in e.live.paused_reason
    k = e.kalshi.calls
    clock[0] += 31; run(e.tick()); clock[0] += 31; run(e.tick())
    assert len(e.odds.calls) == base_book + 3 and e.kalshi.calls > k                            # no more credits spent; Kalshi still refreshing
    assert e.live_status()["paused_reason"]
    e.set_live(True, budget=10)                                                                 # topping up the budget resumes sportsbook polling
    clock[0] += 31; run(e.tick())
    assert len(e.odds.calls) == base_book + 4


def test_live_polling_spends_the_shared_ledger_but_respects_the_reserve():
    e, clock = live_engine(live_budget=50, credit_reserve=25)
    run(e.refresh_book("nhl")); run(e.refresh_kalshi("nhl")); e.set_live(True)
    e.credits["remaining"] = 26; e.odds.remaining = 26
    run(e.tick())
    assert e.spent_24h() >= 1                                                                   # live calls count toward the day's total
    clock[0] += 31; run(e.tick())
    assert any("reserve" in x["msg"] for x in e.errors) and len(e.odds.calls) == 2              # the reserve is never broken, live or not


def test_pregame_scheduler_leaves_live_sports_alone():
    e, clock = live_engine()
    run(e.refresh_book("nhl")); run(e.refresh_kalshi("nhl")); clock[0] += 99999
    e.set_live(True)
    assert e.live_sports() == [] or e.book_target() is None                                     # a live sport is never polled by BOTH schedulers
    clock[0] = T0 + 100
    e.states["nhl"].book_ts = clock[0] - 99999
    assert e.live_sports() == ["nhl"] and e.book_target() is None


def test_live_rows_track_movement_and_tag_trades_and_snapshots():
    e, clock = live_engine()
    run(e.refresh_book("nhl")); e.set_live(True)
    run(e.refresh_kalshi("nhl"))
    row = next(r for r in e.live_rows() if r["team"] == "NYR")
    assert row["live"] and row["move_30s"] is None and row["kalshi_age"] == 0                    # no history yet
    clock[0] += 31; e.kalshi.bid, e.kalshi.ask = 0.55, 0.57; e.odds.quote_age = 5; run(e.refresh_book("nhl", live=True)); run(e.refresh_kalshi("nhl"))
    row = next(r for r in e.live_rows() if r["team"] == "NYR")
    assert row["move_30s"] == pytest.approx(0.05) and row["move_120s"] is None                  # the mid jumped 51c -> 56c
    t = e.paper_trade(row["ticker"], "YES", 10)
    trade = e.store.trades()[0]
    assert trade["live"] == 1 and e.store.trades(live=True) and not e.store.trades(live=False)
    perf_live, perf_pre = e.performance(live=True), e.performance(live=False)
    assert perf_live["open"] == 1 and perf_pre["open"] == 0
    snaps = e.store.db.execute("select count(*) c from snapshots where live=1").fetchone()["c"]
    assert snaps >= 1


def test_convergence_never_mixes_pregame_and_live_prices():
    s = Store(":memory:")
    base = dict(kind="ML", game="A @ B", label="B to win", fair_yes=0.60, yes_bid=0.54, yes_ask=0.56, best_side="YES", price=0.56,
                net_edge=0.05, n_books=3, approx=False, tradable=True, ticker="T1")
    s.add_snapshots([dict(base, live=True)], 1000.0)
    s.add_snapshots([dict(base, live=True, yes_bid=0.60, yes_ask=0.62)], 1100.0)
    assert s.convergence(0.02, 600)["signals"] == 0                                              # pregame view sees nothing
    live = s.convergence(0.02, 60, live=True)
    assert live["signals"] == 1 and live["avg_move_cents"] == pytest.approx(6.0)


# ── API ──
@pytest.fixture()
def demo_client(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO", "1"); monkeypatch.setenv("SPORTS", "nhl,mlb"); monkeypatch.setenv("DATABASE", str(tmp_path / "l.db"))
    with TestClient(create_app(run_loop=False)) as c:
        assert c.post("/api/refresh", json={"source": "both"}).status_code == 200
        yield c


def test_live_api_off_then_on_then_off(demo_client):
    c = demo_client
    s = c.get("/api/live").json()
    assert s["enabled"] is False and set(s["live_sports"]) == {"nhl", "mlb"} and s["est_credits_per_hour"] == pytest.approx(240.0)   # 120/h per sport
    assert c.get("/api/live/board").json()["rows"] == []                                          # OFF means no live rows are shown at all
    on = c.post("/api/live", json={"enabled": True, "budget": 40}).json()
    assert on["enabled"] and on["budget"] == 40 and on["spent"] == 0
    board = c.get("/api/live/board").json()
    rows = board["rows"]
    assert rows and all(r["live"] for r in rows) and {r["sport"] for r in rows} == {"nhl", "mlb"}
    assert all("move_30s" in r and "kalshi_age" in r and "book_age_secs" in r for r in rows)
    assert c.get("/api/live/board", params={"sport": "mlb"}).json()["rows"] and {r["sport"] for r in c.get("/api/live/board", params={"sport": "mlb"}).json()["rows"]} == {"mlb"}
    assert c.get("/api/status").json()["live_on"] is True
    off = c.post("/api/live", json={"enabled": False}).json()
    assert off["enabled"] is False and c.get("/api/live/board").json()["rows"] == []


def test_live_trade_over_http_is_tagged_and_filterable(demo_client):
    c = demo_client
    c.post("/api/live", json={"enabled": True, "budget": 10})
    row = next(r for r in c.get("/api/live/board").json()["rows"] if r["best_side"])
    assert c.post("/api/paper-trades", json={"ticker": row["ticker"], "side": row["best_side"], "qty": 5}).status_code == 200
    assert [t["live"] for t in c.get("/api/paper-trades").json()["rows"]] == [1]
    assert len(c.get("/api/paper-trades", params={"live": "true"}).json()["rows"]) == 1 and c.get("/api/paper-trades", params={"live": "false"}).json()["rows"] == []
    assert c.get("/api/performance", params={"live": "true"}).json()["open"] == 1 and c.get("/api/performance", params={"live": "false"}).json()["open"] == 0
    q = c.get("/api/quote", params={"ticker": row["ticker"], "side": row["best_side"], "qty": 3}).json()    # live rows work with the order-cost calculator
    assert q["complete"]


def test_live_mode_needs_an_api_key_when_live(monkeypatch, tmp_path):
    from kalshi_edge.demo import DemoKalshi, DemoWorld
    e = Engine(Settings(), None, DemoKalshi(DemoWorld([SPORTS["nhl"]])), Store(":memory:"), [SPORTS["nhl"]])
    with TestClient(create_app(engine=e, run_loop=False)) as c:
        assert c.post("/api/live", json={"enabled": True}).status_code == 400


# ── freshness is judged when rows are READ, so a stalled collector can never leave rows looking fresh ──
def test_a_stalled_collector_turns_pregame_rows_stale_and_untradable():
    e, clock = live_engine()
    class PreOdds(LiveOdds):
        async def fetch(self, odds_key, markets=("h2h",)):
            self.calls.append(self.clock[0]); self.remaining -= 1
            return [event(self.clock[0], minutes_ago=-120, quote_age=5)], {"remaining": self.remaining, "used": 1, "last_cost": 1}
    e.odds = PreOdds(clock)
    e.kalshi.bid, e.kalshi.ask = 0.46, 0.47                                                     # ~8c below the books' 55%: a clear opportunity
    run(e.refresh_book("nhl")); run(e.refresh_kalshi("nhl"))
    fresh = [r for r in e.rows if r["team"] == "NYR"][0]
    assert fresh["opportunity"] and fresh["kalshi_age"] == 0 and e.opportunities()
    clock[0] += 120                                                                             # Kalshi polling silently stops for two minutes
    stale = [r for r in e.rows if r["team"] == "NYR"][0]
    assert stale["kalshi_age"] == pytest.approx(120) and "stale quote" in stale["flags"]
    assert not stale["tradable"] and not stale["opportunity"] and e.opportunities() == [] and e.status()["opportunities"] == 0
    assert fresh["opportunity"]                                                                 # restamp is pure: the stored row is untouched
    clock[0] += 3000                                                                            # ...and the sportsbook data ages past its own limit too
    older = [r for r in e.rows if r["team"] == "NYR"][0]
    assert "stale book" in older["flags"] and older["book_age_secs"] >= 3000
    run(e.refresh_kalshi("nhl")); run(e.refresh_book("nhl"))                                    # collector recovers
    back = [r for r in e.rows if r["team"] == "NYR"][0]
    assert back["kalshi_age"] == 0 and "stale quote" not in back["flags"] and "stale book" not in back["flags"] and back["opportunity"]


def test_live_rows_age_on_screen_and_go_stale_when_kalshi_stops():
    e, clock = live_engine()
    run(e.refresh_book("nhl")); e.set_live(True); run(e.refresh_kalshi("nhl"))
    e.kalshi.bid, e.kalshi.ask = 0.44, 0.45
    clock[0] += 31; run(e.refresh_book("nhl", live=True)); run(e.refresh_kalshi("nhl"))
    r0 = [r for r in e.live_rows() if r["team"] == "NYR"][0]
    assert r0["kalshi_age"] == 0 and r0["opportunity"] and "stale quote" not in r0["flags"]
    clock[0] += 5
    r1 = [r for r in e.live_rows() if r["team"] == "NYR"][0]
    assert r1["kalshi_age"] == pytest.approx(5) and r1["book_age_secs"] == r0["book_age_secs"] + 5       # numbers keep counting between refreshes
    clock[0] += 12                                                                              # 17 s without a Kalshi update: past the 15 s live limit
    r2 = [r for r in e.live_rows() if r["team"] == "NYR"][0]
    assert "stale quote" in r2["flags"] and not r2["tradable"] and not r2["opportunity"]
    clock[0] += 200                                                                             # sportsbook polling also stalled: book quote far too old
    r3 = [r for r in e.live_rows() if r["team"] == "NYR"][0]
    assert "stale book" in r3["flags"] and not r3["tradable"]


def test_api_reports_age_that_grows_between_refreshes(demo_client):
    c = demo_client
    c.post("/api/live", json={"enabled": True, "budget": 10})
    eng = c.app.state.engine
    shift = [0.0]
    real_now = eng.now
    eng.now = lambda: real_now() + shift[0]                                                     # fast-forward the engine's clock only
    a = c.get("/api/live/board").json()["rows"][0]
    shift[0] = 40
    b = c.get("/api/live/board").json()["rows"][0]
    assert b["kalshi_age"] >= a["kalshi_age"] + 39 and "stale quote" in b["flags"] and not b["tradable"]
