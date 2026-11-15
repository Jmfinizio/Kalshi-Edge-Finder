"""Order-book math: what an order REALLY costs (depth + fees), and the new all-in column."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from kalshi_edge.clients import KalshiClient
from kalshi_edge.core import Settings, asks_from_bids, kalshi_fee, walk_book
from kalshi_edge.demo import DemoKalshi, DemoOdds, DemoWorld
from kalshi_edge.server import create_app
from kalshi_edge.service import Engine
from kalshi_edge.sports import SPORTS
from kalshi_edge.store import Store

# the example order book printed in Kalshi's own documentation
DOC_YES = [(0.01, 200), (0.15, 100), (0.20, 50), (0.25, 20), (0.30, 11), (0.31, 10), (0.32, 10), (0.33, 11), (0.34, 9), (0.35, 11), (0.41, 10), (0.42, 13)]
DOC_NO = [(0.01, 100), (0.16, 3), (0.25, 50), (0.28, 19), (0.36, 5), (0.37, 50), (0.38, 300), (0.44, 29), (0.45, 20), (0.56, 17)]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_asks_are_the_complement_of_the_opposite_bids():
    yes_asks = asks_from_bids(DOC_NO)                                   # buying YES hits NO bids
    assert yes_asks[0] == (0.44, 17) and yes_asks[1] == (0.55, 20)      # docs: best YES ask = 1 - 0.56 = 0.44
    no_asks = asks_from_bids(DOC_YES)                                   # buying NO hits YES bids
    assert no_asks[0] == (0.58, 13)                                     # docs: best NO ask = 1 - 0.42 = 0.58
    assert [p for p, _ in yes_asks] == sorted(p for p, _ in yes_asks)   # cheapest first


def test_small_order_fills_at_the_best_ask():
    f = walk_book(asks_from_bids(DOC_NO), 10)
    assert f["complete"] and f["avg_price"] == pytest.approx(0.44) and f["worst"] == 0.44
    assert f["fee"] == pytest.approx(kalshi_fee(0.44, 10)) and f["all_in_per"] == pytest.approx(0.44 + kalshi_fee(0.44, 10) / 10)


def test_big_order_walks_up_the_book_and_costs_more_than_the_board_price():
    f = walk_book(asks_from_bids(DOC_NO), 30)                           # 17 @ 44c, then 13 @ 55c
    assert [(x["price"], x["qty"]) for x in f["fills"]] == [(0.44, 17), (0.55, 13)]
    assert f["cost"] == pytest.approx(17 * 0.44 + 13 * 0.55) and f["avg_price"] == pytest.approx(14.63 / 30)
    assert f["avg_price"] > f["best"] and f["worst"] == 0.55
    assert f["fee"] == pytest.approx(kalshi_fee(0.44, 17) + kalshi_fee(0.55, 13))
    assert f["all_in_total"] == pytest.approx(f["cost"] + f["fee"]) and f["all_in_per"] == pytest.approx(f["all_in_total"] / 30)


def test_order_bigger_than_the_book_reports_partial_fill():
    f = walk_book([(0.44, 17), (0.55, 20)], 100)
    assert not f["complete"] and f["filled"] == 37 and f["requested"] == 100
    empty = walk_book([], 5)
    assert not empty["complete"] and empty["filled"] == 0 and empty["avg_price"] is None


# ── client parses both response shapes ──
def kalshi_with(payload):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as h:
            return await KalshiClient(h).fetch_orderbook("KXNFLGAME-X-TB")
    return run(go())


def test_orderbook_dollar_format():
    ob = kalshi_with({"orderbook_fp": {"yes_dollars": [["0.4100", "10.00"], ["0.4200", "13.00"]], "no_dollars": [["0.5600", "17.00"]]}})
    assert ob["yes_bids"] == [(0.41, 10.0), (0.42, 13.0)] and ob["no_bids"] == [(0.56, 17.0)]


def test_orderbook_legacy_cents_format_and_empty_sides():
    ob = kalshi_with({"orderbook": {"yes": [[41, 10], [42, 13]], "no": None}})
    assert ob["yes_bids"] == [(0.41, 10.0), (0.42, 13.0)] and ob["no_bids"] == []
    assert kalshi_with({"orderbook_fp": {}}) == {"yes_bids": [], "no_bids": []}


# ── engine + API on the demo market ──
@pytest.fixture()
def client():
    sl = [SPORTS["nfl"]]
    w = DemoWorld(sl, seed=3)
    e = Engine(Settings(), DemoOdds(w), DemoKalshi(w), Store(":memory:"), sl, demo=True, demo_world=w)
    with TestClient(create_app(engine=e, run_loop=False)) as c:
        c.post("/api/refresh", json={"source": "both"})
        yield c


def test_every_row_shows_all_in_above_the_quoted_price(client):
    rows = [r for r in client.get("/api/board").json()["rows"] if r["price"]]
    assert rows
    for r in rows:
        assert r["all_in"] == pytest.approx(r["price"] + r["fee_per"]) and r["all_in"] > r["price"]
        assert r["net_edge"] == pytest.approx(r["fair_side"] - r["all_in"])           # the net edge ALWAYS included the fee


def test_quote_at_top_of_book_matches_board_and_beyond_it_slips(client):
    top = client.get("/api/opportunities").json()["rows"][0]
    small = client.get("/api/quote", params={"ticker": top["ticker"], "side": top["best_side"], "qty": 1}).json()
    assert small["complete"] and small["avg_price"] == pytest.approx(top["price"], abs=1e-4) and small["slippage"] == pytest.approx(0, abs=1e-4)
    assert small["edge_all_in"] == pytest.approx(small["fair_side"] - small["all_in_per"])
    big = client.get("/api/quote", params={"ticker": top["ticker"], "side": top["best_side"], "qty": 400}).json()
    assert big["avg_price"] > small["avg_price"] and big["slippage"] > 0 and big["edge_all_in"] < small["edge_all_in"]
    assert len(big["levels"]) >= 2 and big["levels"][0]["price"] <= big["levels"][1]["price"]


def test_quote_validation(client):
    t = client.get("/api/board").json()["rows"][0]["ticker"]
    assert client.get("/api/quote", params={"ticker": "NOPE", "side": "YES", "qty": 5}).status_code == 404
    assert client.get("/api/quote", params={"ticker": t, "side": "MAYBE", "qty": 5}).status_code == 400
    assert client.get("/api/quote", params={"ticker": t, "side": "YES", "qty": 0}).status_code == 400
